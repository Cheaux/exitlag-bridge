#!/usr/bin/env python3
"""Real loopback tests; no firewall, administrator, Wine, or account required."""
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import bridge as b

class Echo:
    def __init__(self, prefix):
        self.prefix, self.stopping = prefix, False
        self.peers = []
        self.sock = b.Bridge.listen(0)
        self.endpoint = self.sock.getsockname()
        threading.Thread(target=self.run, daemon=True).start()
    def run(self):
        while not self.stopping:
            try:
                client, address = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.peers.append(address)
            threading.Thread(target=self.echo, args=(client,), daemon=True).start()
    def echo(self, client):
        try:
            with client:
                while True:
                    data = client.recv(65536)
                    if not data:
                        return
                    client.sendall(self.prefix + data)
        except OSError:
            pass
    def close(self):
        self.stopping = True
        self.sock.close()

class Shim:
    def __init__(self, service, target=None, nested=False, delay=0, malformed=False):
        self.sock = socket.create_connection(("127.0.0.1", service.ipc_port))
        self.sock.settimeout(2)
        b.send_frame(self.sock, b.HELLO_MSG, b.HELLO.pack(b.VERSION, 123, 0, service.token))
        assert b.receive_frame(self.sock)[0] == b.ACK_MSG
        self.target, self.nested, self.delay, self.malformed = target, nested, delay, malformed
        self.resolved, self.errors, self.locals = [], [], []
        threading.Thread(target=self.run, daemon=True).start()
    def run(self):
        try:
            while True:
                kind, data = b.receive_frame(self.sock)
                if kind == b.PING:
                    b.send_frame(self.sock, b.PONG)
                    continue
                if kind != b.TCP_REQ:
                    raise AssertionError("Unexpected request")
                tag, endpoint, pid, flags, direction, reserved, family, local, remote = b.TCP_REQUEST.unpack(data)
                self.locals.append(b.parse_address(local))
                if self.nested:
                    for query in (pid, 0x7FFFFFFF):
                        b.send_frame(self.sock, b.RESOLVE_REQ, b.RESOLVE_REQUEST.pack(tag, query, 0))
                        reply_kind, reply = b.receive_frame(self.sock)
                        assert reply_kind == b.RESOLVE_RESP
                        self.resolved.append(reply[16:].decode())
                if self.delay:
                    time.sleep(self.delay)
                if self.malformed:
                    self.sock.sendall(b.HEADER.pack(b.MAGIC, b.TCP_RESP, 1, 0))
                    continue
                target = b.address_bytes(self.target) if self.target else remote
                b.send_frame(self.sock, b.TCP_RESP, b.TCP_REPLY.pack(
                    tag, int(bool(self.target)), flags, target, 0, int(bool(self.target)), 0, 0))
        except (OSError, EOFError):
            pass
        except Exception as error:
            self.errors.append(error)
    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.original, self.routed = Echo(b"origin:"), Echo(b"exitlag:")
        self.identity = r"C:\Games\World of Warcraft\WowClassic.exe"
        self.service = b.Bridge(secrets.token_bytes(32), ipc_port=0, proxy_port=0,
            test_destination=self.original.endpoint,
            owner_lookup=lambda endpoint: (9876, self.identity))
        self.service.start()
        self.shim = None
    def tearDown(self):
        if self.shim:
            self.shim.close()
        self.service.close()
        self.original.close()
        self.routed.close()
    def exchange(self):
        with socket.create_connection(("127.0.0.1", self.service.proxy_port)) as client:
            client.settimeout(3)
            client.sendall(b"hello")
            result = client.recv(256)
            client.shutdown(socket.SHUT_WR)
            return result
    def test_no_shim_uses_original(self):
        self.assertEqual(self.exchange(), b"origin:hello")
    def test_relay_metrics_count_real_payload(self):
        self.shim = Shim(self.service, self.routed.endpoint)
        self.assertEqual(self.exchange(), b"exitlag:hello")
        deadline = time.monotonic() + 1
        while self.service.snapshot()["bytes_received"] < 13 and time.monotonic() < deadline:
            time.sleep(.01)
        result = self.service.snapshot()
        self.assertEqual(result["redirected"], 1)
        self.assertEqual(result["bytes_sent"], 5)
        self.assertEqual(result["bytes_received"], 13)
        self.assertEqual(result["relay_bytes_sent"], 5)
        self.assertEqual(result["relay_bytes_received"], 13)
        self.assertEqual(result["flows"][0]["bytes_received"], 13)
        self.assertTrue(result["flows"][0]["relay"])
    def test_real_tcp_relay_and_nested_identity(self):
        self.shim = Shim(self.service, self.routed.endpoint, nested=True)
        self.assertEqual(self.exchange(), b"exitlag:hello")
        self.assertEqual(self.shim.resolved, [self.identity, ""])
        self.assertEqual(self.shim.errors, [])
        self.assertEqual(self.shim.locals, self.routed.peers)
    def test_unchanged_callback_uses_original(self):
        self.shim = Shim(self.service)
        self.assertEqual(self.exchange(), b"origin:hello")
    def test_non_loopback_redirect_rejected(self):
        self.shim = Shim(self.service, ("203.0.113.1", 80))
        self.assertEqual(self.exchange(), b"origin:hello")
        self.assertIsNone(self.service.peer)
    def test_slow_shim_falls_back_promptly(self):
        self.shim = Shim(self.service, self.routed.endpoint, delay=0.6)
        start = time.monotonic()
        self.assertEqual(self.exchange(), b"origin:hello")
        self.assertLess(time.monotonic() - start, 1.5)
    def test_malformed_response_uses_original(self):
        self.shim = Shim(self.service, malformed=True)
        self.assertEqual(self.exchange(), b"origin:hello")
    def test_unmapped_process_never_asks_shim(self):
        self.service.owner_lookup = lambda endpoint: (0, "")
        self.shim = Shim(self.service, self.routed.endpoint)
        self.assertEqual(self.exchange(), b"origin:hello")
    def test_bad_token_rejected(self):
        with socket.create_connection(("127.0.0.1", self.service.ipc_port)) as client:
            client.settimeout(1)
            b.send_frame(client, b.HELLO_MSG, b.HELLO.pack(b.VERSION, 1, 0, bytes(32)))
            self.assertEqual(client.recv(1), b"")
        self.assertIsNone(self.service.peer)
    def test_second_shim_cannot_replace_first(self):
        self.shim = Shim(self.service, self.routed.endpoint)
        with socket.create_connection(("127.0.0.1", self.service.ipc_port)) as client:
            client.settimeout(1)
            b.send_frame(client, b.HELLO_MSG, b.HELLO.pack(b.VERSION, 1, 0, self.service.token))
            self.assertEqual(client.recv(1), b"")
        self.assertEqual(self.exchange(), b"exitlag:hello")
    def test_token_permissions_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            b.write_token(path, self.service.token)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                b.write_token(path, secrets.token_bytes(32))
            self.assertEqual(path.read_text(), self.service.token.hex())
    @unittest.skipUnless(sys.platform == "darwin", "Requires macOS libproc")
    def test_native_process_attribution(self):
        code = "import socket,sys;s=socket.create_connection(('127.0.0.1',int(sys.argv[1])));print(s.getsockname()[1],flush=True);sys.stdin.read()"
        child = subprocess.Popen([sys.executable, "-c", code, str(self.original.endpoint[1])],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            port = int(child.stdout.readline())
            resolver = b.NativeIdentity({})
            # Apple's python3 launcher re-execs the Python.app binary.
            actual_executable = resolver.path(child.pid)
            self.assertTrue(Path(actual_executable).is_file())
            resolver.identities[actual_executable] = self.identity
            self.assertEqual(resolver.owner(("127.0.0.1", port)), (child.pid, self.identity))
        finally:
            child.stdin.close()
            child.wait(timeout=3)
            child.stdout.close()
    def test_wire_sizes_match_packed_c_protocol(self):
        code = '#include <stdio.h>\n#include "backend/protocol.h"\nint main(void){printf("%zu %zu %zu %zu",sizeof(struct exl_header),sizeof(struct exl_hello),sizeof(struct exl_tcp_connect_req),sizeof(struct exl_tcp_connect_reply));}\n'
        with tempfile.TemporaryDirectory() as directory:
            source, binary = Path(directory) / "sizes.c", Path(directory) / "sizes"
            source.write_text(code)
            subprocess.run(["/usr/bin/cc", "-I", str(Path(__file__).resolve().parents[1]),
                            str(source), "-o", str(binary)], check=True)
            actual = subprocess.check_output([str(binary)], text=True)
            self.assertEqual(actual, f"{b.HEADER.size} {b.HELLO.size} {b.TCP_REQUEST.size} {b.TCP_REPLY.size}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
