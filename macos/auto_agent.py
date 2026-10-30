#!/usr/bin/env python3
"""Ordinary-user launcher and actual-game companion lifecycle. Never runs as root."""
import argparse
import ctypes
import fcntl
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
import threading
from auto_common import atomic_private, publish, read_private, runtime_for, write_control
from bridge import NativeIdentity, load_identities
from companion import Companion

STATE = Path.home() / 'Library/Application Support/ExitLag Mac Bridge'
HERE = Path(__file__).resolve().parent
STOPPING = False


class Lease:
    def __init__(self, parent):
        self.parent = parent
        self.stop_event = threading.Event()
        self.enabled = False
        self.thread = threading.Thread(target=self.pulse, daemon=True)
        self.thread.start()

    def pulse(self):
        runtime = runtime_for(os.getuid())
        while not self.stop_event.is_set():
            try:
                status, _ = read_private(runtime / 'status.json', os.getuid())
                if time.time() - status['updated_at'] < 3:
                    write_control(runtime / 'control', os.getuid(), dict(version=1, epoch=status['epoch'],
                        run=self.enabled and not STOPPING and alive(self.parent) and not self.stop_event.is_set()))
            except (OSError, ValueError, KeyError):
                pass
            if self.stop_event.wait(1):
                break

    def close(self):
        self.enabled = False
        self.stop_event.set()
        self.thread.join(timeout=2)
        try:
            runtime = runtime_for(os.getuid())
            status, _ = read_private(runtime / 'status.json', os.getuid())
            write_control(runtime / 'control', os.getuid(), dict(version=1, epoch=status['epoch'], run=False))
        except (OSError, ValueError, KeyError):
            pass


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def client_pids(executable):
    listing = subprocess.check_output(['/bin/ps', '-ww', '-axo', 'pid=,uid=,args='], text=True)
    result = []
    for line in listing.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1] == str(os.getuid()):
            if parts[2] == str(executable) or parts[2].startswith(str(executable) + ' '):
                result.append(int(parts[0]))
    return result


def stop_clients(executable):
    pids = client_pids(executable)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + 5
    while any(alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(.1)
    if any(alive(pid) for pid in pids):
        env = os.environ.copy()
        env.update(WINEPREFIX=str(STATE / 'prefix'), WINEDEBUG='-all')
        subprocess.run([str(STATE / 'runtime/bin/wine'), 'taskkill', '/IM', 'ExitLag.exe', '/F'],
                       env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)


def start_client(executable, log):
    env = os.environ.copy()
    env.pop('QT_QUICK_BACKEND', None)
    env.pop('QT_OPENGL', None)
    env.update(WINEPREFIX=str(STATE / 'prefix'), WINEDEBUG='-all', MVK_CONFIG_LOG_LEVEL='0',
               QSG_RHI_BACKEND='vulkan', QSG_RENDER_LOOP='basic', EXITLAG_MACOS_SAFE_QT_TIMERS='0',
               EXITLAG_NETD='127.0.0.1:51900',
               EXITLAG_NETD_TOKEN='Z:' + str(runtime_for(os.getuid()) / 'session.token'))
    return subprocess.Popen([str(STATE / 'runtime/bin/wine'), str(executable)],
                            cwd=executable.parent, env=env, stdin=subprocess.DEVNULL,
                            stdout=log, stderr=log, start_new_session=True)


def native_pids(resolver):
    count = resolver.lib.proc_listallpids(None, 0)
    pids = (ctypes.c_int * min(max(count + 128, 128), 131072))()
    count = resolver.lib.proc_listallpids(pids, ctypes.sizeof(pids))
    return {resolver.path(pid): pid for pid in list(pids)[:max(0, count)] if pid > 0}


def legacy_active():
    try:
        state, _ = read_private(Path('/var/run') / f'exitlag-macos-{os.getuid()}' / 'state.json', os.getuid(), 4096)
        return alive(state['pid'])
    except (OSError, ValueError, KeyError):
        return False


def stop(signum, frame):
    global STOPPING
    STOPPING = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--parent', type=int, required=True)
    args = parser.parse_args()
    if os.geteuid() == 0 or os.getuid() < 500 or not alive(args.parent):
        raise SystemExit('Run ExitLag.app as the installed game user')
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(STATE / 'auto-agent.lock', os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    info = os.fstat(lock)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        raise SystemExit('Unexpected app lock')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('ExitLag launcher is already running')
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, stop)
    runtime = runtime_for(os.getuid())
    identities = load_identities(HERE / 'games.auto.map')
    resolver = NativeIdentity(identities)
    companions, failures = {}, {}
    client = None
    from bridge_setup.core import find_client
    executable = find_client(STATE / 'prefix')
    if executable is None:
        raise SystemExit('Complete the first-run setup before launching ExitLag')
    log_path = STATE / 'launcher-client.log'
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    os.fchmod(log_fd, 0o600)
    log = os.fdopen(log_fd, 'ab', buffering=0)
    launched_at = None
    lease = Lease(args.parent)
    try:
        while not STOPPING and alive(args.parent):
            service = None
            try:
                service, _ = read_private(runtime / 'status.json', os.getuid())
                if time.time() - service['updated_at'] > 3:
                    service = None
            except (OSError, ValueError, KeyError):
                pass
            stage, message = 'starting', 'Starting ExitLag'
            if not service:
                stage, message = 'setup', 'Install ExitLag Mac Setup.pkg to enable automatic routing'
            elif legacy_active():
                stage, message = 'legacy', 'The previous script session is still running. Waiting for it to finish.'
            else:
                if client is None:
                    # The previous command launcher used another token path.
                    # Restart only this prefix's exact official client once.
                    stop_clients(executable)
                    client = start_client(executable, log)
                    launched_at = time.monotonic()
                    lease.enabled = True
                pids = client_pids(executable)
                if not pids and time.monotonic() - launched_at > 20:
                    stage, message = 'closed', 'ExitLag is closed. Ordinary routing is active.'
                    publish(STATE / 'launcher-status.json', os.getuid(), dict(stage=stage, message=message, updated_at=time.time()))
                    break
                found = native_pids(resolver)
                for native in identities:
                    name = 'forever' if '/_forever_/' in native else 'era'
                    config_path = STATE / f'auto-companion-{name}.json'
                    old = companions.get(native)
                    if old and (found.get(native) != old.native_pid or old.child.poll() is not None):
                        config_path.unlink(missing_ok=True)
                        old.close()
                        del companions[native]
                    if native in found and native not in companions and time.monotonic() >= failures.get(native, 0):
                        candidate = None
                        try:
                            candidate = Companion(identities, native=native, port=51903 if name == 'forever' else 51902)
                            config = candidate.start()
                            atomic_private(config_path, json.loads(config.read_text()))
                            companions[native] = candidate
                        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                            print(name + ': ' + type(error).__name__ + ': ' + str(error), flush=True)
                            if candidate:
                                candidate.close()
                            failures[native] = time.monotonic() + 4
                stage, message = service['stage'], service['message']
                if service.get('relay_active'):
                    message = 'WoW traffic is flowing through ExitLag'
                elif service.get('stage') == 'ready' and companions:
                    message = 'Ready · reconnect your character to route a new connection'
            value = dict(stage=stage, message=message, updated_at=time.time(), games=[
                'Forever' if '/_forever_/' in native else 'Classic Era' for native in companions],
                routing=bool(service and service.get('routing')), relay_active=(service or {}).get('relay_active', 0),
                bytes_received=(service or {}).get('bytes_received', 0))
            publish(STATE / 'launcher-status.json', os.getuid(), value)
            time.sleep(.5)
    finally:
        lease.close()
        for native, companion in companions.items():
            name = 'forever' if '/_forever_/' in native else 'era'
            (STATE / f'auto-companion-{name}.json').unlink(missing_ok=True)
            companion.close()
        if client is not None:
            stop_clients(executable)
        log.close()
        os.close(lock)


if __name__ == '__main__':
    main()
