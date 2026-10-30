#!/usr/bin/env python3
"""Fixed-scope launchd service. No user commands, paths or executable code accepted."""
import argparse
import json
import os
import pwd
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import time
from auto_common import publish, read_private, runtime_for, valid_request
from bridge import Bridge, load_identities, write_token
from pf_plan import make_rules
from wine_socket import ReloadingWineSocketAdapter

UID = os.getuid()
USER_HOME = Path.home()
TARGETS = ['64.224.24.0/21:3724', '24.105.0.0/18:3724']
ANCHOR = f'com.apple/cheaux.exitlag.auto.{UID}'
HERE = Path(__file__).resolve().parent
RUNTIME = runtime_for(UID)
STOPPING = False


def configure_user(uid):
    """UID comes only from the administrator-owned launchd configuration."""
    global UID, USER_HOME, ANCHOR, RUNTIME
    if type(uid) is not int or not 500 <= uid < 2**31:
        raise ValueError('Expected the installation user UID')
    account = pwd.getpwuid(uid)
    home = Path(account.pw_dir)
    if not home.is_absolute() or str(home) in ('/', '/var/root', '/Users/Shared'):
        raise ValueError('Unexpected installation user home')
    info = home.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid:
        raise ValueError('The home must belong to the installation user')
    UID, USER_HOME = uid, home
    ANCHOR = f'com.apple/cheaux.exitlag.auto.{uid}'
    RUNTIME = runtime_for(uid)


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=8)


def private_root_file(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'r') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_size > 64:
            raise ValueError('Unexpected PF lease')
        text = stream.read(65).strip()
        if not re.fullmatch(r'\d{1,40}', text):
            raise ValueError('Invalid PF lease')
        return text


def release_lease():
    path = RUNTIME / 'pf-enable.token'
    try:
        token = private_root_file(path)
    except FileNotFoundError:
        return
    command('/sbin/pfctl', '-X', token)
    path.unlink()


class Session:
    def __init__(self):
        self.bridge = None
        self.active = False
        self.token = RUNTIME / 'session.token'
        self.rules = RUNTIME / 'anchor.conf'

    def start(self):
        identities = load_identities(HERE / 'games.auto.map')
        state = USER_HOME / 'Library/Application Support/ExitLag Mac Bridge'
        configurations = {identity: (state / ('auto-companion-forever.json' if '/_forever_/' in native else 'auto-companion-era.json'),
                                     51903 if '/_forever_/' in native else 51902)
                          for native, identity in identities.items()}
        adapter = ReloadingWineSocketAdapter(configurations, UID, identities.values())
        token = secrets.token_bytes(32)
        try:
            self.bridge = Bridge(token, identities, use_pf=True, socket_adapter=adapter)
            write_token(self.token, token)
            os.chown(self.token, UID, -1)
            self.bridge.start()
        except BaseException:
            self.close()
            raise

    def enable(self):
        if self.active:
            return
        previous = command('/sbin/pfctl', '-s', 'info')
        if not re.search(r'Status:\s*Enabled', previous.stdout):
            result = command('/sbin/pfctl', '-E')
            match = re.search(r'Token\s*:\s*(\d+)', result.stdout + result.stderr)
            if not match:
                raise RuntimeError('PF did not provide a reference')
            fd = os.open(RUNTIME / 'pf-enable.token', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as out:
                out.write(match[1] + '\n')
                out.flush()
                os.fsync(out.fileno())
        self.rules.write_text(make_rules(UID, 51901, TARGETS))
        self.rules.chmod(0o600)
        # Set first so even a partial pfctl failure invokes cleanup.
        self.active = True
        command('/sbin/pfctl', '-a', ANCHOR, '-f', str(self.rules))

    def disable(self):
        if self.active:
            command('/sbin/pfctl', '-a', ANCHOR, '-F', 'all')
            self.active = False
        release_lease()

    def close(self):
        self.disable()
        if self.bridge:
            self.bridge.close()
            self.bridge = None
        self.token.unlink(missing_ok=True)
        self.rules.unlink(missing_ok=True)


def stop(signum, frame):
    global STOPPING
    STOPPING = True


def main():
    if os.geteuid() != 0 or os.sys.platform != 'darwin':
        raise SystemExit('This service requires its installed macOS launch daemon')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--uid', type=int, required=True)
    args = parser.parse_args()
    configure_user(args.uid)
    RUNTIME.mkdir(mode=0o711, exist_ok=True)
    info = RUNTIME.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit('Unexpected runtime directory')
    # The bridge lives in this process. After a crash there can be no orphaned
    # child to kill. Clean only this service's fixed anchor and own PF reference.
    command('/sbin/pfctl', '-a', ANCHOR, '-F', 'all')
    release_lease()
    (RUNTIME / 'session.token').unlink(missing_ok=True)
    epoch = secrets.token_hex(16)
    publish(RUNTIME / 'control', UID, {'version': 1, 'epoch': epoch, 'run': False})
    session = Session()
    last_stamp = None
    last_pulse = 0
    last_run = False
    retry_at = 0
    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, stop)
    try:
        while not STOPPING:
            now = time.monotonic()
            try:
                request, stamp = read_private(RUNTIME / 'control', UID, 512)
                if valid_request(request, epoch):
                    if stamp != last_stamp:
                        last_stamp, last_pulse = stamp, now
                    last_run = request['run']
                else:
                    last_run = False
            except BlockingIOError:
                pass
            except (OSError, ValueError, TypeError):
                last_run = False
            requested = last_run and now - last_pulse < 6
            stage, message, snapshot = 'stopped', 'ExitLag is closed. Ordinary routing is active.', {}
            try:
                if requested:
                    if not session.bridge and now >= retry_at:
                        session.start()
                    if session.bridge:
                        snapshot = session.bridge.snapshot()
                        if snapshot['shim_connected']:
                            session.enable()
                            stage, message = 'ready', 'Ready for WoW Classic Era or Forever'
                        else:
                            session.disable()
                            stage, message = 'waiting_client', 'Waiting for the ExitLag dashboard'
                    else:
                        stage, message = 'starting', 'Starting the routing helper'
                elif session.bridge:
                    session.close()
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                print(type(error).__name__ + ': ' + str(error), flush=True)
                session.close()
                retry_at = now + 3
                stage, message = 'error', 'The routing helper is retrying. Ordinary routing is active.'
            value = dict(version=1, epoch=epoch, pid=os.getpid(), stage=stage, message=message,
                         routing=session.active, requested=requested, updated_at=time.time(),
                         relay_active=sum(bool(f.get('active') and f.get('relay')) for f in snapshot.get('flows', [])),
                         bytes_sent=snapshot.get('relay_bytes_sent', 0), bytes_received=snapshot.get('relay_bytes_received', 0),
                         errors=snapshot.get('errors', 0), adapter_errors=snapshot.get('adapter_errors', 0),
                         shim_connected=snapshot.get('shim_connected', False))
            try:
                publish(RUNTIME / 'status.json', UID, value)
            except BlockingIOError:
                pass
            time.sleep(.25)
    finally:
        session.close()
        publish(RUNTIME / 'status.json', UID, dict(version=1, epoch=epoch, stage='stopped',
                message='Routing helper stopped', routing=False, updated_at=time.time()))


if __name__ == '__main__':
    main()
