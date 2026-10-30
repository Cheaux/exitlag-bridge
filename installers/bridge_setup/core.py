"""Per-user fresh setup using pinned Wine and a user-supplied vendor installer.
SPDX-License-Identifier: GPL-3.0-only
"""
import argparse
import contextlib
import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
from .util import State, digest, download, extract, private_json, replace_tree
from .pe import check as check_abi

CATALOG = json.loads(Path(__file__).with_name('catalog.json').read_text())
VERSION = '0.4.0'


def console_output(data):
    # Windows console tools switch to UTF-16 when redirected to a disk file.
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        return data.decode('utf-16', errors='replace').encode('utf-8')
    if len(data) >= 8 and data[1::2].count(0) > len(data[1::2]) * .7:
        return data.decode('utf-16le', errors='replace').encode('utf-8')
    return data


def default_state():
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/ExitLag Mac Bridge'
    return Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'exitlag-bridge'


def find_client(prefix):
    base = Path(prefix) / 'drive_c'
    if base.exists():
        base.resolve().relative_to(Path(prefix).resolve())
    for relative in ('ExitLag/ExitLag.exe', 'Program Files/ExitLag/ExitLag.exe', 'Program Files (x86)/ExitLag/ExitLag.exe'):
        candidate = base / relative
        if candidate.is_file() and not candidate.is_symlink():
            candidate.resolve().relative_to(Path(prefix).resolve())
            return candidate
    return None


def validate_installer(path):
    path = Path(path).expanduser().resolve(strict=True)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not 1024 <= info.st_size <= 1024**3:
        raise ValueError('Select the official Windows installer executable')
    with path.open('rb') as source:
        header = source.read(4096)
        if header[:2] != b'MZ' or len(header) < 64:
            raise ValueError('The selected file is not a Windows executable')
        offset = struct.unpack_from('<I', header, 60)[0]
        if offset > 16 * 1024**2:
            raise ValueError('Invalid installer executable header')
        source.seek(offset)
        signature = source.read(6)
        if signature[:4] != b'PE\0\0' or struct.unpack_from('<H', signature, 4)[0] not in (0x14c, 0x8664):
            raise ValueError('Unsupported Windows installer executable')
    return path


class Setup:
    def __init__(self, state, payloads, host=None, emit=None):
        if os.geteuid() == 0:
            raise ValueError('Run setup as the game user; Wine must never run as root')
        self.state = State(Path(state), create=True)
        self.payloads = Path(payloads)
        self.host = host or sys.platform
        if self.host not in ('darwin', 'linux'):
            raise ValueError('This installer supports macOS and Linux')
        if self.host == 'linux' and platform.machine() not in ('x86_64', 'AMD64'):
            raise ValueError('The Linux beta currently supports x86_64 only')
        self.external_emit = emit or (lambda *args: None)
        self.record = self.state.load()
        self.record.update(schema=1, project='exitlag-bridge', version=VERSION)
        self.wine = self.state.relative('runtime/bin/wine')
        self.prefix = self.state.relative('prefix')
        self.log = self.state.relative('setup.log')
        self.payload_manifest = json.loads((self.payloads / 'manifest.json').read_text())
        for name in ('nfapi.dll', 'nfextlag.sys', 'wbemprox.dll'):
            if self.payload_manifest.get('files', {}).get(name) != digest(self.payloads / name):
                raise ValueError('Packaged compatibility component failed verification')

    def emit(self, stage, message):
        value = dict(stage=stage, message=message, updated_at=time.time())
        private_json(self.state.relative('setup-status.json'), value)
        self.external_emit(stage, message)

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.state.relative('setup.lock'), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield
        finally:
            os.close(fd)

    def command(self, args, timeout=180, capture=False):
        env = os.environ.copy()
        env.update(WINEPREFIX=str(self.prefix), WINEDEBUG='-all', MVK_CONFIG_LOG_LEVEL='0',
                   WINEDLLOVERRIDES='mscoree,mshtml=')
        if self.host == 'darwin':
            env.update(QSG_RHI_BACKEND='vulkan', QSG_RENDER_LOOP='basic')
        else:
            env['QSG_RHI_BACKEND'] = 'opengl'
        env.pop('WINELOADER', None)
        env.pop('WINEDLLPATH', None)
        # Wine services can inherit the installer's pipe descriptors and stay
        # alive after the wizard exits. Regular temporary files let wait()
        # observe the real installer exit status without waiting for pipe EOF.
        with tempfile.TemporaryFile(dir=self.state.path) as output, tempfile.TemporaryFile(dir=self.state.path) as errors:
            result = subprocess.run([str(x) for x in args], env=env, stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=errors, timeout=timeout)
            streams = []
            for stream in (output, errors):
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - 1024**2))
                streams.append(stream.read(1024**2))
            stdout, stderr = streams
        # Diagnostics stay local. Never copy raw Wine output into release reports.
        fd = os.open(self.log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_size > 8 * 1024**2:
                os.ftruncate(fd, 0)
            os.write(fd, stdout + stderr)
        finally:
            os.close(fd)
        if result.returncode:
            raise RuntimeError('Setup command failed (code %s). Local details are in setup.log.' % result.returncode)
        return console_output(stdout) if capture else None

    def prepare_runtime(self):
        self.emit('runtime', 'Preparing the private Wine runtime')
        asset = CATALOG['wine-macos' if self.host == 'darwin' else 'wine-linux']
        runtime = self.state.relative('runtime')
        marker = runtime / '.bridge-runtime.json'
        if runtime.exists():
            if not marker.is_file() or marker.is_symlink():
                raise ValueError('An unmarked runtime was preserved; use a clean setup directory')
            metadata = json.loads(marker.read_text())
            if metadata.get('archive_sha256') != asset['sha256']:
                raise ValueError('The existing runtime has another version; repair requires an explicit runtime upgrade')
        else:
            cache = self.state.relative('cache')
            archive = download(asset, cache, self.emit)
            with tempfile.TemporaryDirectory(prefix='.unpack-', dir=self.state.path) as temporary:
                extract(archive, temporary)
                root = Path(temporary)
                if self.host == 'darwin':
                    candidates = list(root.glob('*/Contents/Resources/wine'))
                else:
                    candidates = [p for p in root.iterdir() if p.is_dir() and (p / 'bin/wine').exists()]
                if len(candidates) != 1:
                    raise ValueError('The verified runtime archive has an unexpected layout')
                replace_tree(candidates[0], runtime)
            private_json(marker, dict(project='exitlag-bridge', archive_sha256=asset['sha256']))
        modules = [p for p in runtime.rglob('wbemprox.dll') if p.parent.name == 'x86_64-windows']
        if len(modules) != 1:
            raise ValueError('The Wine WMI module layout is unsupported')
        target = modules[0]
        if target.is_symlink():
            resolved = target.resolve()
            resolved.relative_to(runtime.resolve())
            target = resolved
        backup = target.with_name('wbemprox.bridge-original.dll')
        if not backup.exists():
            shutil.copy2(target, backup)
        shutil.copy2(self.payloads / 'wbemprox.dll', target)
        version = self.command([self.wine, '--version'], capture=True).decode(errors='replace').strip()
        if not version.startswith('wine-11.17'):
            raise ValueError('The pinned Wine runtime did not report its expected version')
        self.record['wine_version'] = version
        self.record['runtime_archive_sha256'] = asset['sha256']
        self.record['wmi_module_sha256'] = digest(target)
        self.state.save(self.record)

    def prepare_prefix(self):
        self.emit('prefix', 'Preparing a dedicated Windows environment')
        self.state.quarantine_incomplete_prefix()
        if not (self.prefix / 'drive_c').is_dir() or not (self.prefix / 'system.reg').is_file():
            self.prefix.mkdir(mode=0o700, exist_ok=True)
            self.command([self.wine, 'wineboot', '-i'], timeout=300)
            # wineboot's launcher can exit while the server is still flushing
            # the initial registry. Wait only during fresh initialization;
            # existing prefixes may intentionally have applications running.
            self.command([self.wine.with_name('wineserver'), '-w'], timeout=120)
        if not (self.prefix / 'drive_c').is_dir() or not (self.prefix / 'system.reg').is_file():
            raise RuntimeError('Wine did not finish initializing its dedicated environment')
        self.emit('wmi', 'Checking Windows compatibility support')
        output = self.command([self.wine, 'wmic', 'path', 'Win32_LogicalDiskRootDirectory',
                               'get', 'GroupComponent,PartComponent'], timeout=60, capture=True)
        if b'GroupComponent' not in output or b'Win32_LogicalDisk' not in output:
            raise RuntimeError('The required WMI association did not pass its fresh-prefix check')
        self.record['wmi_verified'] = True
        self.state.save(self.record)

    def install_client(self, installer=None):
        client = find_client(self.prefix)
        if installer is not None:
            installer = validate_installer(installer)
            self.emit('vendor', 'Complete the official ExitLag installer and review its terms in the window that opens')
            # Deliberately interactive: no silent install and no agreement acceptance.
            self.command([self.wine, installer, '/DIR=C:\\ExitLag', '/NORESTART'], timeout=3600)
            client = find_client(self.prefix)
        if not client:
            raise ValueError('Choose the official ExitLag .exe downloaded from exitlag.com/download')
        # The wizard may launch the client. Stop only that client in this
        # dedicated prefix before replacing loaded compatibility components.
        tasks = self.command([self.wine, 'tasklist.exe', '/FI', 'IMAGENAME eq ExitLag.exe', '/NH'], capture=True)
        if b'exitlag.exe' in tasks.lower():
            self.command([self.wine, 'taskkill.exe', '/IM', 'ExitLag.exe', '/F'])
        self.emit('compatibility', 'Installing and checking the bridge components')
        with open(os.devnull, 'w') as quiet, contextlib.redirect_stdout(quiet):
            compatible = check_abi(self.payloads / 'nfapi.dll', client.parent)
        if not compatible:
            raise ValueError('This ExitLag client needs compatibility work; the vendor files were preserved')
        target = client.parent / 'nfapi.dll'
        expected = self.payload_manifest['files']['nfapi.dll']
        if target.exists() and digest(target) not in (expected, self.record.get('shim_sha256')):
            backups = self.state.relative('vendor-backups')
            backups.mkdir(mode=0o700, exist_ok=True)
            backup = backups / ('nfapi-' + digest(target) + '.dll')
            if not backup.exists():
                shutil.copy2(target, backup)
            self.record['vendor_nfapi_backup'] = str(backup.relative_to(self.state.path))
        shutil.copy2(self.payloads / 'nfapi.dll', target)
        drivers = self.state.relative('prefix/drive_c/windows/system32/drivers')
        drivers.mkdir(parents=True, exist_ok=True)
        driver = drivers / 'nfextlag.sys'
        driver_hash = self.payload_manifest['files']['nfextlag.sys']
        if driver.exists() and digest(driver) not in (driver_hash, self.record.get('driver_sha256')):
            backups = self.state.relative('vendor-backups')
            backups.mkdir(mode=0o700, exist_ok=True)
            backup = backups / ('nfextlag-' + digest(driver) + '.sys')
            if not backup.exists():
                shutil.copy2(driver, backup)
            self.record['vendor_driver_backup'] = str(backup.relative_to(self.state.path))
        shutil.copy2(self.payloads / 'nfextlag.sys', driver)
        self.command([self.wine, 'reg.exe', 'add', r'HKCU\Software\Wine\DllOverrides',
                      '/v', 'nfapi', '/t', 'REG_SZ', '/d', 'native,builtin', '/f'])
        # Wine may already have the presence-only service on a repair install.
        try:
            self.command([self.wine, 'sc.exe', 'query', 'nfextlag'])
        except RuntimeError:
            self.command([self.wine, 'sc.exe', 'create', 'nfextlag', 'type=', 'kernel',
                          'start=', 'demand', 'binPath=', r'C:\windows\system32\drivers\nfextlag.sys'])
        self.record.update(complete=True, client_relative=str(client.relative_to(self.prefix)),
                           client_sha256=digest(client), shim_sha256=expected, driver_sha256=driver_hash)
        self.state.save(self.record)
        self.emit('complete', 'ExitLag is ready. Sign in directly in the official client.')
        return client

    def run(self, installer=None, prepare_only=False):
        if installer is not None:
            validate_installer(installer)  # Fail before downloading or initializing Wine.
        with self.lock():
            try:
                self.prepare_runtime()
                self.prepare_prefix()
                if prepare_only:
                    self.emit('prepared', 'Wine and the required WMI support are ready; select the official installer to finish')
                    return
                return self.install_client(installer)
            except BaseException as error:
                self.record['complete'] = False
                self.state.save(self.record)
                self.emit('error', str(error))
                raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'install', 'status', 'uninstall'))
    parser.add_argument('--state', type=Path, default=default_state())
    parser.add_argument('--payloads', type=Path, required=False)
    parser.add_argument('--installer', type=Path)
    parser.add_argument('--purge', action='store_true')
    parser.add_argument('--confirm-purge', action='store_true')
    args = parser.parse_args(argv)
    if args.action == 'status':
        try:
            record = State(args.state).load()
        except FileNotFoundError:
            record = {'complete': False, 'installed': False}
        print(json.dumps(record))
        return
    if args.action == 'uninstall':
        state = State(args.state)
        if args.purge:
            if not args.confirm_purge:
                parser.error('Purging account data requires --confirm-purge')
            state.purge()
        else:
            record = state.load()
            if record.get('client_relative'):
                client = state.relative('prefix/' + record['client_relative'])
                replacements = [(client.parent / 'nfapi.dll', 'shim_sha256', 'vendor_nfapi_backup'),
                                (state.relative('prefix/drive_c/windows/system32/drivers/nfextlag.sys'),
                                 'driver_sha256', 'vendor_driver_backup')]
                for target, installed_hash, backup_key in replacements:
                    target.resolve().relative_to(state.path.resolve())
                    if target.exists() and digest(target) == record.get(installed_hash):
                        if record.get(backup_key):
                            shutil.copy2(state.relative(record[backup_key]), target)
                        else:
                            target.unlink()
            state.remove_components()
        return
    if not args.payloads:
        parser.error('--payloads must point to the packaged bridge components')
    def emit(stage, message):
        print(json.dumps(dict(stage=stage, message=message)), flush=True)
    Setup(args.state, args.payloads, emit=emit).run(args.installer, args.action == 'prepare')


if __name__ == '__main__':
    main()
