"""Create the per-user Mac app that gives the official client a Dock identity.
SPDX-License-Identifier: GPL-3.0-only
"""
import json
import os
from pathlib import Path
import plistlib
import stat
import struct
import subprocess
from client_icon import client_icns


def write_owned(path, data, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError('Unexpected generated application file')
        os.fchmod(fd, mode)
        os.ftruncate(fd, 0)
        with os.fdopen(fd, 'wb', closefd=False) as target:
            target.write(data)
    finally:
        os.close(fd)


def prepare_client_app(state, executable, bridge_app):
    state = Path(state)
    relative = Path(executable).resolve().relative_to((state / 'prefix').resolve())
    app = state / 'ExitLag.app'
    marker = app / 'Contents/Resources/.exitlag-bridge-client'
    if app.is_symlink():
        raise ValueError('Linked client applications are unsupported')
    if app.exists() and (not marker.is_file() or marker.is_symlink() or marker.read_text() != 'exitlag-bridge\n'):
        raise ValueError('An unrelated client application was preserved')
    app.mkdir(mode=0o700, exist_ok=True)
    for name in ('Contents', 'Contents/MacOS', 'Contents/Resources'):
        path = app / name
        if path.is_symlink():
            raise ValueError('Linked application directories are unsupported')
        path.mkdir(mode=0o700, exist_ok=True)
    write_owned(marker, b'exitlag-bridge\n', 0o600)
    source = Path(__file__).with_name('client_entry.py').read_bytes()
    write_owned(app / 'Contents/MacOS/ExitLag', source, 0o700)
    info = dict(CFBundleExecutable='ExitLag', CFBundlePackageType='APPL',
                CFBundleName='ExitLag', CFBundleDisplayName='ExitLag',
                CFBundleIdentifier='local.cheaux.ExitLagClient', CFBundleVersion='2',
                NSHighResolutionCapable=True)
    try:
        icon = client_icns(executable)
    except (OSError, ValueError, struct.error, subprocess.SubprocessError):
        # An icon extraction failure must not stop routing or client startup.
        fallback = Path(bridge_app) / 'Contents/Resources/BridgeIcon.icns'
        icon = fallback.read_bytes() if fallback.is_file() else None
    if icon:
        write_owned(app / 'Contents/Resources/ClientIcon.icns', icon, 0o600)
        info['CFBundleIconFile'] = 'ClientIcon'
    write_owned(app / 'Contents/Info.plist', plistlib.dumps(info), 0o600)
    config = dict(bridge_app=str(bridge_app), client_relative=str(relative))
    write_owned(app / 'Contents/Resources/client.json', (json.dumps(config) + '\n').encode(), 0o600)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(app)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    # Bundle contents can change without changing the outer directory's mtime.
    # Notify LaunchServices so it can refresh the saved Dock icon.
    os.utime(app, None)
    return app
