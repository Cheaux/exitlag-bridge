"""Authenticated local Winsock adapter; no administrator or game credentials."""
import json
import os
import socket
import stat
import struct
import time
from bridge import address_bytes, parse_address, receive_exact


class WineSocketAdapter:
    def __init__(self, pid, identity, token, port=51902):
        self.pid, self.identity, self.token, self.port = pid, identity, token, port

    @classmethod
    def load(cls, path, uid, identities, expected_port=51902):
        if uid is None or uid < 500:
            raise ValueError("A companion must belong to the game user")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as source:
            info = os.fstat(source.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                    or info.st_mode & 0o077 or info.st_size > 4096):
                raise ValueError("Companion configuration must be private and owned by the game user")
            config = json.loads(source.read(4097))
        token = bytes.fromhex(config["token"])
        if (expected_port not in (51902, 51903) or config.get("version") != 1 or config.get("port") != expected_port
                or type(config.get("pid")) is not int or not 0 < config["pid"] < 2**32
                or config.get("identity") not in identities or len(token) != 32):
            raise ValueError("Invalid companion configuration")
        return cls(config["pid"], config["identity"], token, expected_port)

    def connect(self, bridge, native_pid, identity, source_ip, original):
        if not native_pid or identity != self.identity:
            return socket.create_connection(original, timeout=5), False
        channel = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        try:
            channel.sendall(b"EWS1" + self.token + socket.inet_aton(source_ip))
            response = receive_exact(channel, 20)
            if struct.unpack("<I", response[:4])[0] != self.pid:
                raise ValueError("Companion process changed")
            local = parse_address(response[4:] + bytes(12))
            if local[0] != source_ip or not local[1]:
                raise ValueError("Companion returned an invalid source endpoint")
            target = bridge.select_route(self.pid, identity, local, original)
            if target == original:
                channel.close()
                return socket.create_connection(original, timeout=5), False
            if target[0].split(".")[0] != "127" or not target[1]:
                raise ValueError("Companion redirect must be loopback")
            channel.sendall(address_bytes(target)[:16])
            if struct.unpack("<I", receive_exact(channel, 4))[0] != 1:
                raise OSError("Companion could not open the ExitLag relay")
            return channel, True
        except BaseException:
            channel.close()
            raise


class ReloadingWineSocketAdapter:
    """Load the current game companion per flow, including after WoW restarts.

    The bridge can be ready before the native game launches. A short bounded
    wait lets its user-owned companion finish starting for the first flow.
    """
    def __init__(self, path, uid, identities, wait=4):
        self.path, self.uid, self.identities, self.wait = path, uid, tuple(identities), wait

    def connect(self, bridge, native_pid, identity, source_ip, original):
        if not native_pid or identity not in self.identities:
            return socket.create_connection(original, timeout=5), False
        path, port = self.path[identity] if isinstance(self.path, dict) else (self.path, 51902)
        deadline = time.monotonic() + self.wait
        while True:
            try:
                adapter = WineSocketAdapter.load(path, self.uid, [identity], port)
                return adapter.connect(bridge, native_pid, identity, source_ip, original)
            except (OSError, ValueError, KeyError, TypeError, EOFError, struct.error):
                if time.monotonic() >= deadline or bridge.stop_event.wait(.1):
                    raise OSError("The game socket companion is unavailable")
