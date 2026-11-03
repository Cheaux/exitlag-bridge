#!/Library/PrivilegedHelperTools/local.cheaux.ExitLagRouting/python/bin/python3 -I
"""Registered Mac app entry; exec preserves its Dock identity for Wine.
SPDX-License-Identifier: GPL-3.0-only
"""
import json
import os
from pathlib import Path
import stat
import sys


def main():
    if os.geteuid() == 0:
        raise SystemExit('ExitLag must run as the game user')
    app = Path(__file__).resolve().parents[2]
    state = app.parent
    config = json.loads((app / 'Contents/Resources/client.json').read_text())
    if sys.argv[1:] != ['--bridge-launch']:
        os.execv('/usr/bin/open', ['open', '-a', config['bridge_app']])
    prefix = state / 'prefix'
    client = prefix / config['client_relative']
    client.resolve(strict=True).relative_to(prefix.resolve())
    if client.name != 'ExitLag.exe':
        raise ValueError('Unexpected client executable')
    environment = os.environ.copy()
    for key in ('QT_QUICK_BACKEND', 'QT_OPENGL', 'WINELOADER', 'WINEDLLPATH', 'WINELOADERNOEXEC'):
        environment.pop(key, None)
    environment.update(WINEPREFIX=str(prefix), WINEDEBUG='-all', MVK_CONFIG_LOG_LEVEL='0',
                       # The Vulkan surface can return black after Dock restore.
                       # Use Qt's bundled software OpenGL for this UI only.
                       QSG_RHI_BACKEND='opengl', QT_OPENGL='software', QSG_RENDER_LOOP='basic',
                       # Qt falls back to Windows message timers when its
                       # precision timer is unavailable under Wine.
                       EXITLAG_MACOS_SAFE_QT_TIMERS='1', EXITLAG_NETD='127.0.0.1:51900',
                       EXITLAG_MACOS_APP_ICON=str(app / 'Contents/Resources/ClientIcon.icns'),
                       EXITLAG_NETD_TOKEN='Z:/var/run/cheaux-exitlag-%s/session.token' % os.getuid())
    fd = os.open(state / 'launcher-client.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
        os.close(fd)
        raise ValueError('Unexpected client log')
    os.fchmod(fd, 0o600)
    os.dup2(fd, 1); os.dup2(fd, 2)
    if fd > 2:
        os.close(fd)
    os.chdir(client.parent)
    os.execve(str(state / 'runtime/bin/wine'), ['wine', str(client)], environment)


if __name__ == '__main__':
    main()
