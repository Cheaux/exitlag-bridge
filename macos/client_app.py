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
import tempfile
from client_icon import client_icns


def write_owned(path, data, mode):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError('Unexpected generated application file')
        os.fchmod(fd, mode)
        if info.st_size == len(data) and os.read(fd, len(data) + 1) == data:
            return
        # The inactive Dock plug-in can still have this file mapped. Replace
        # changed files atomically instead of truncating code in that process.
        temporary_fd, temporary = tempfile.mkstemp(prefix='.client-', dir=path.parent)
        try:
            os.fchmod(temporary_fd, mode)
            with os.fdopen(temporary_fd, 'wb') as target:
                temporary_fd = -1
                target.write(data)
            os.replace(temporary, path)
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            Path(temporary).unlink(missing_ok=True)
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
                CFBundleIdentifier='local.cheaux.ExitLagClient', CFBundleVersion='5',
                # The visible client uses normal Dock launch behavior. Its
                # inactive plug-in and Finder icon already preserve alpha.
                LSUIElement=False, NSHighResolutionCapable=True)
    try:
        icon = client_icns(executable)
    except (OSError, ValueError, struct.error, subprocess.SubprocessError):
        # An icon extraction failure must not stop routing or client startup.
        fallback = Path(bridge_app) / 'Contents/Resources/BridgeIcon.icns'
        icon = fallback.read_bytes() if fallback.is_file() else None
    if icon:
        write_owned(app / 'Contents/Resources/ClientIcon.icns', icon, 0o600)
        info['CFBundleIconFile'] = 'ClientIcon'
    plugin_name = 'ExitLagIcon.docktileplugin'
    plugin_source = Path(__file__).with_name(plugin_name)
    if plugin_source.is_dir():
        for name in ('Contents/PlugIns', 'Contents/PlugIns/' + plugin_name,
                     'Contents/PlugIns/' + plugin_name + '/Contents',
                     'Contents/PlugIns/' + plugin_name + '/Contents/MacOS',
                     'Contents/PlugIns/' + plugin_name + '/Contents/_CodeSignature'):
            path = app / name
            if path.is_symlink():
                raise ValueError('Linked Dock plug-in directories are unsupported')
            path.mkdir(mode=0o700, exist_ok=True)
        target = app / 'Contents/PlugIns' / plugin_name
        for plugin_relative, mode in (('Contents/Info.plist', 0o600),
                                      ('Contents/MacOS/ExitLagDockTile', 0o700),
                                      ('Contents/_CodeSignature/CodeResources', 0o600)):
            write_owned(target / plugin_relative, (plugin_source / plugin_relative).read_bytes(), mode)
        info['NSDockTilePlugIn'] = plugin_name
    write_owned(app / 'Contents/Info.plist', plistlib.dumps(info), 0o600)
    config = dict(bridge_app=str(bridge_app), client_relative=str(relative))
    write_owned(app / 'Contents/Resources/client.json', (json.dumps(config) + '\n').encode(), 0o600)
    icon_helper = Path(__file__).with_name('set-client-icon')
    if icon_helper.is_file() and (app / 'Icon\r').exists():
        # Clear Finder's resource-fork decoration while signing, then restore
        # it afterward. The closed-app Dock tile has its own drawing hook.
        subprocess.run([str(icon_helper), 'clear', str(app)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(app)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    if icon and icon_helper.is_file():
        subprocess.run([str(icon_helper), 'apply', str(app)],
                       check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    # Bundle contents can change without changing the outer directory's mtime.
    # Notify LaunchServices so it can refresh the saved Dock icon.
    os.utime(app, None)
    return app
