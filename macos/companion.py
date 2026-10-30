"""User-owned Wine socket companion tied to a real native Classic Era process."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from bridge import NativeIdentity, write_token


class Companion:
    def __init__(self, identities, native=None, port=51902):
        if os.geteuid() == 0:
            raise ValueError("Never run the Wine companion as root")
        candidates = [(source, windows) for source, windows in identities.items()
                      if (native == source if native else "/_classic_era_/" in source)
                      and windows.lower().endswith(("\\wowclassic.exe", "\\wow.exe"))]
        if len(candidates) != 1:
            raise ValueError("This companion requires one exact WoW game identity")
        self.native, self.identity = candidates[0]
        expected_port = 51903 if '/_forever_/' in self.native else 51902
        if port != expected_port:
            raise ValueError('Unexpected companion port for this game')
        self.port = port
        self.resolver = NativeIdentity(identities)
        count = self.resolver.lib.proc_listallpids(None, 0)
        pids = (ctypes.c_int * min(max(count + 128, 128), 131072))()
        count = self.resolver.lib.proc_listallpids(pids, ctypes.sizeof(pids))
        self.native_pid = next((pid for pid in list(pids)[:max(0, count)]
                                if pid > 0 and self.resolver.path(pid) == self.native), None)
        if not self.native_pid:
            raise ValueError("Open WoW Classic Era first; reconnect after the routing session starts")
        self.state = Path.home() / "Library/Application Support/ExitLag Mac Bridge"
        self.prefix = self.state / "prefix"
        self.wine = self.state / "runtime/bin/wine"
        if not self.wine.is_file():
            raise ValueError("The validated Wine runtime is not installed")
        self.folder = None
        self.child = None
        self.thread = None
        self.stop_event = threading.Event()

    def start(self):
        deadline = time.monotonic() + 8
        while True:
            probe = socket.socket()
            try:
                probe.bind(("127.0.0.1", self.port))
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("The previous Wine socket adapter has not released its port")
                time.sleep(.1)
            finally:
                probe.close()
        binary = Path(__file__).resolve().parent / "build/WowClassic.exe"
        parts = self.identity.replace("\\", "/").split("/")
        if parts[0].lower() != "c:" or any(part in ("", ".", "..") for part in parts[1:]):
            raise ValueError("Expected an explicit companion path on C:")
        destination = self.prefix / "drive_c" / Path(*parts[1:])
        if destination.exists():
            digest = hashlib.sha256(destination.read_bytes()).hexdigest()
            accepted = {hashlib.sha256(binary.read_bytes()).hexdigest(),
                        '1355d6034330cfa446d6981910558d3dd827b914aa93aaf19c1ad9915a17e12e',
                        'b1c9c044983ca492985c1cf9a275047830b91cc8b60b040c07832f7f13765bb4'}
            if digest not in accepted:
                raise ValueError("Refusing to replace an unrelated executable in the dedicated Wine prefix")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(binary, destination)
        sessions = self.state / "sessions"
        sessions.mkdir(mode=0o700, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix="wow-", dir=sessions))
        self.heartbeat = self.folder / "heartbeat"
        self.heartbeat.write_text("native-classic-era-alive\n")
        self.heartbeat.chmod(0o600)
        pid_file, token_file = self.folder / "wine.pid", self.folder / "socket.token"
        pid_file.write_text(""); pid_file.chmod(0o600)
        token = secrets.token_bytes(32)
        write_token(token_file, token)

        def pulse():
            try:
                while not self.stop_event.wait(.5):
                    if self.resolver.path(self.native_pid) != self.native:
                        break
                    self.heartbeat.touch()
            finally:
                self.heartbeat.unlink(missing_ok=True)
        self.thread = threading.Thread(target=pulse, daemon=True)
        self.thread.start()
        env = os.environ.copy()
        env.update(WINEPREFIX=str(self.prefix), WINEDEBUG="-all",
                   EXITLAG_COMPANION_PORT=str(self.port),
                   EXITLAG_COMPANION_PID_FILE="Z:" + str(pid_file),
                   EXITLAG_COMPANION_HEARTBEAT="Z:" + str(self.heartbeat),
                   EXITLAG_COMPANION_TOKEN_FILE="Z:" + str(token_file))
        self.child = subprocess.Popen([str(self.wine), str(destination)],
                    cwd=destination.parent, env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True)
        deadline = time.monotonic() + 10
        while not pid_file.read_text().strip() and time.monotonic() < deadline:
            if self.child.poll() is not None:
                raise RuntimeError("Wine socket companion stopped during startup")
            time.sleep(.1)
        pid = int(pid_file.read_text())
        config = self.folder / "config.json"
        fd = os.open(config, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as out:
            json.dump(dict(version=1, pid=pid, identity=self.identity,
                           port=self.port, token=token.hex()), out)
        return config

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        if self.folder:
            (self.folder / "heartbeat").unlink(missing_ok=True)
        if self.child and self.child.poll() is None:
            try:
                self.child.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.child.terminate()
                self.child.wait(timeout=3)
        if self.folder:
            for name in ("wine.pid", "socket.token", "config.json"):
                (self.folder / name).unlink(missing_ok=True)
            self.folder.rmdir()
