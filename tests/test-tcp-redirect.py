#!/usr/bin/env python3
"""Root-only, self-contained integration test for the active TCP DNAT path.

The test uses RFC 5737 TEST-NET-2 as the apparent remote endpoint, a mock shim
that returns a loopback address+port mutation, and a real loopback TCP listener
as the proxy. Success proves that the original SYN reaches the callback, the
returned tuple is installed, payload crosses the redirected connection in both
directions, the application retains its original peer identity, and teardown
removes the dedicated nftables table.
"""

import os
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


MAGIC = 0x314C5845
PROTOCOL_VERSION = 2
ORIGINAL_IP = "198.51.100.1"
ORIGINAL_PORT = 65000
IPC_PORT = 51901  # deliberately not the live ExitLag shim's default 51900
ROOT = Path(__file__).resolve().parent.parent


def receive_exact(sock, length):
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise RuntimeError("unexpected IPC EOF")
        data += chunk
    return data


def receive_frame(sock):
    magic, message_type, _flags, length = struct.unpack("<IHHI", receive_exact(sock, 12))
    if magic != MAGIC:
        raise RuntimeError(f"bad IPC magic {magic:#x}")
    return message_type, receive_exact(sock, length)


def send_frame(sock, message_type, payload):
    sock.sendall(struct.pack("<IHHI", MAGIC, message_type, 0, len(payload)) + payload)


def table_exists():
    return subprocess.run(
        ["/usr/bin/nft", "list", "table", "inet", "exitlag"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def wait_for_log(log_path, text, process, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if log_path.exists() and text in log_path.read_text(errors="replace"):
            return
        if process.poll() is not None:
            raise RuntimeError(f"exitlag-netd exited during startup ({process.returncode})")
        time.sleep(0.02)
    raise RuntimeError(f"timed out waiting for {text!r}")


def main():
    if os.geteuid() != 0:
        print("run as root: sudo backend/test-tcp-redirect.py", file=sys.stderr)
        return 2
    if table_exists():
        print("refusing to touch pre-existing table inet exitlag", file=sys.stderr)
        return 3

    proxy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    proxy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    proxy.bind(("127.0.0.1", 0))
    proxy.listen(1)
    proxy_port = proxy.getsockname()[1]
    observed = {}
    worker_errors = []
    table_installed = False
    helper = None
    shim = None

    def proxy_worker():
        try:
            connection, peer = proxy.accept()
            with connection:
                connection.settimeout(5)
                observed["proxy_peer"] = peer
                observed["payload"] = receive_exact(connection, 4)
                connection.sendall(b"pong")
        except Exception as error:  # surfaced by the main thread below
            worker_errors.append(error)

    proxy_thread = threading.Thread(target=proxy_worker, daemon=True)
    proxy_thread.start()

    with tempfile.TemporaryDirectory(prefix="exitlag-tcp-test-") as temp_dir:
        log_path = Path(temp_dir) / "netd.log"
        try:
            helper = subprocess.Popen([
                str(ROOT / "backend/exitlag-netd"),
                "--queue", "0",
                "--port", str(IPC_PORT),
                "--timeout-ms", "1000",
                "--apply-tcp",
                "--log", str(log_path),
            ])
            wait_for_log(log_path, "bound NFQUEUE 0", helper)

            setup_env = os.environ.copy()
            setup_env["TEST_DEST"] = ORIGINAL_IP
            subprocess.run(
                [str(ROOT / "backend/nft-setup.sh")],
                env=setup_env,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            table_installed = True

            shim = socket.create_connection(("127.0.0.1", IPC_PORT), timeout=3)
            send_frame(shim, 0x0001, struct.pack("<IIQ", PROTOCOL_VERSION, 4242, 0))
            message_type, acknowledgement = receive_frame(shim)
            if message_type != 0x0002 or struct.unpack("<IIQ", acknowledgement)[0] != PROTOCOL_VERSION:
                raise RuntimeError("invalid helper HELLO acknowledgement")

            def shim_worker():
                try:
                    message_type, request = receive_frame(shim)
                    if message_type != 0x0010 or len(request) != 86:
                        raise RuntimeError(f"unexpected callback request {message_type:#x}/{len(request)}")
                    fields = struct.unpack("<QQIIB3sH28s28s", request)
                    tag, endpoint, pid, flag, _direction, _reserved, family, local, remote = fields
                    observed["request"] = {
                        "tag": tag,
                        "endpoint": endpoint,
                        "pid": pid,
                        "flag": flag,
                        "family": family,
                        "local": (socket.inet_ntoa(local[4:8]), struct.unpack("!H", local[2:4])[0]),
                        "remote": (socket.inet_ntoa(remote[4:8]), struct.unpack("!H", remote[2:4])[0]),
                    }
                    target = (
                        struct.pack("<H", 2)
                        + struct.pack("!H", proxy_port)
                        + socket.inet_aton("127.0.0.1")
                        + bytes(20)
                    )
                    reply = struct.pack("<QII28sBB2s", tag, 1, 0x210, target, 1, 1, bytes(2))
                    send_frame(shim, 0x0011, reply)
                except Exception as error:  # surfaced by the main thread below
                    worker_errors.append(error)

            shim_thread = threading.Thread(target=shim_worker, daemon=True)
            shim_thread.start()

            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.settimeout(5)
                client.connect((ORIGINAL_IP, ORIGINAL_PORT))
                observed["client_local"] = client.getsockname()
                observed["client_peer"] = client.getpeername()
                client.sendall(b"ping")
                observed["reply"] = receive_exact(client, 4)

            shim_thread.join(3)
            proxy_thread.join(3)
            if shim_thread.is_alive() or proxy_thread.is_alive():
                raise RuntimeError("integration-test worker did not finish")
            if worker_errors:
                raise worker_errors[0]

            ruleset = subprocess.run(
                ["/usr/bin/nft", "list", "table", "inet", "exitlag"],
                check=True,
                text=True,
                capture_output=True,
            ).stdout
            log_text = log_path.read_text(errors="replace")

            assert observed["request"]["remote"] == (ORIGINAL_IP, ORIGINAL_PORT)
            assert observed["client_peer"] == (ORIGINAL_IP, ORIGINAL_PORT)
            assert observed["payload"] == b"ping"
            assert observed["reply"] == b"pong"
            assert f"127.0.0.1 . {proxy_port}" in ruleset
            assert "DNAT armed; ACCEPT (active)" in log_text

            print(f"callback: {observed['request']['local']} -> {observed['request']['remote']}")
            print(f"mutation:  {ORIGINAL_IP}:{ORIGINAL_PORT} -> 127.0.0.1:{proxy_port}")
            print(f"client:    peer remains {observed['client_peer']}; received {observed['reply']!r}")
            print(f"proxy:     peer {observed['proxy_peer']}; received {observed['payload']!r}")
            print("active TCP redirect integration test: PASS")
        finally:
            if shim is not None:
                shim.close()
            proxy.close()
            # Only remove a table whose successful setup was recorded by this
            # process. Never tear down a table that appeared independently.
            if table_installed:
                subprocess.run(
                    [str(ROOT / "backend/nft-teardown.sh")],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            if helper is not None:
                helper.terminate()
                try:
                    helper.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    helper.kill()
                    helper.wait()

    if table_exists():
        print("teardown FAILED: table inet exitlag remains", file=sys.stderr)
        return 1
    print("teardown: PASS (no table inet exitlag)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
