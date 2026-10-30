"""Adapter protocol and root-facing configuration tests, using local TCP only."""
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import tempfile
import threading
import unittest
import bridge as b
import test_bridge
from test_bridge import Shim
from wine_socket import WineSocketAdapter


class AdapterServer:
    def __init__(self, pid, token, port=0):
        self.pid, self.token, self.stopping = pid, token, False
        self.listener = b.Bridge.listen(port)
        self.port = self.listener.getsockname()[1]
        self.bound = []
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        while not self.stopping:
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self.handle, args=(client,), daemon=True).start()

    def handle(self, client):
        try:
            with client, socket.socket() as reserved:
                client.settimeout(2)
                request = b.receive_exact(client, 40)
                if request[:36] != b"EWS1" + self.token:
                    return
                reserved.bind((socket.inet_ntoa(request[36:40]), 0))
                self.bound.append(reserved.getsockname())
                client.sendall(struct.pack("<I", self.pid) + b.address_bytes(reserved.getsockname())[:16])
                target = b.parse_address(b.receive_exact(client, 16) + bytes(12))
                reserved.connect(target)
                client.sendall(struct.pack("<I", 1))
                data = client.recv(256)
                reserved.sendall(data)
                client.sendall(reserved.recv(256))
        except (OSError, EOFError):
            pass

    def close(self):
        self.stopping = True
        self.listener.close()


class AdapterFlowTests(unittest.TestCase):
    setUp = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown
    exchange = test_bridge.BridgeTests.exchange
    def test_adapter_reserves_wine_tuple_and_carries_real_bytes(self):
        token = secrets.token_bytes(32)
        adapter = AdapterServer(4321, token)
        self.service.socket_adapter = WineSocketAdapter(4321, self.identity, token, adapter.port)
        self.shim = Shim(self.service, self.routed.endpoint, nested=True)
        try:
            self.assertEqual(self.exchange(), b"exitlag:hello")
            self.assertEqual(self.shim.locals, adapter.bound)
            self.assertEqual(self.shim.resolved, [self.identity, ""])
            self.assertEqual(self.service.snapshot()["adapter_errors"], 0)
        finally:
            adapter.close()

    def test_wrong_companion_pid_falls_back_without_sending_game_data(self):
        token = secrets.token_bytes(32)
        adapter = AdapterServer(9999, token)
        self.service.socket_adapter = WineSocketAdapter(4321, self.identity, token, adapter.port)
        self.shim = Shim(self.service, self.routed.endpoint)
        try:
            self.assertEqual(self.exchange(), b"origin:hello")
            self.assertEqual(self.service.snapshot()["adapter_errors"], 1)
            self.assertEqual(self.routed.peers, [])
        finally:
            adapter.close()


class ConfigurationTests(unittest.TestCase):
    def test_private_owned_configuration_loads_and_public_file_is_refused(self):
        identity = r"C:\Games\WowClassic.exe"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(dict(version=1, pid=4321, port=51902,
                                           identity=identity, token=secrets.token_hex(32))))
            path.chmod(0o600)
            self.assertEqual(WineSocketAdapter.load(path, os.getuid(), [identity]).pid, 4321)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                WineSocketAdapter.load(path, os.getuid(), [identity])

    def test_symlink_configuration_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config"
            path.write_text("{}"); path.chmod(0o600)
            link = Path(folder) / "link"; link.symlink_to(path)
            with self.assertRaises(OSError):
                WineSocketAdapter.load(link, os.getuid(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
