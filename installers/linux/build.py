#!/usr/bin/env python3
"""Build an amd64 Debian package and a portable administrator installer.
SPDX-License-Identifier: GPL-3.0-only
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[2]
VERSION = '0.4.0'
FILES = ('backend/exitlag-netd', 'backend/run-tcp-redirect.sh', 'backend/runtime-guard.sh',
         'backend/nft-setup.sh', 'backend/nft-teardown.sh', 'installers/linux/app.py',
         'installers/linux/run_root.py', 'installers/setup.py', 'LICENSE', 'THIRD_PARTY.md')


def populate(target):
    target.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        destination = target / name; destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)
    for name in ('installers/bridge_setup', 'installers/payloads', 'LICENSES'):
        shutil.copytree(ROOT / name, target / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    (target / '.exitlag-bridge-program').write_text(VERSION + '\n')
    shutil.copy2(ROOT / 'macos/app/BridgeIcon.svg', target / 'icon.svg')
    for path in target.rglob('*'):
        if path.is_symlink(): raise ValueError('Do not package symlinks in the Linux program tree')
        path.chmod(0o755 if path.is_dir() or path.suffix == '.sh' or path.name == 'exitlag-netd' else 0o644)


def desktop(prefix):
    return f'''[Desktop Entry]
Type=Application
Name=ExitLag Bridge
Comment=Unofficial ExitLag compatibility launcher
Exec=/usr/bin/python3 -I {prefix}/installers/linux/app.py
Icon={prefix}/icon.svg
Terminal=false
Categories=Game;Network;
Actions=Games;Quit;

[Desktop Action Games]
Name=Configure games
Exec=/usr/bin/python3 -I {prefix}/installers/linux/app.py --manage-games

[Desktop Action Quit]
Name=Quit ExitLag Bridge
Exec=/usr/bin/python3 -I {prefix}/installers/linux/app.py --quit
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not sys.platform.startswith('linux'):
        parser.error('Build Linux packages on Linux')
    arch = subprocess.check_output(['dpkg', '--print-architecture'], text=True).strip()
    if arch != 'amd64': parser.error('The Linux beta supports amd64')
    output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    staging = ROOT / 'build/linux-package'
    shutil.rmtree(staging, ignore_errors=True); staging.mkdir(parents=True)
    deb = staging / 'deb'
    populate(deb / 'usr/lib/exitlag-bridge')
    applications = deb / 'usr/share/applications'; applications.mkdir(parents=True)
    (applications / 'exitlag-bridge.desktop').write_text(desktop('/usr/lib/exitlag-bridge'))
    control = deb / 'DEBIAN'; control.mkdir()
    (control / 'control').write_text(f'''Package: exitlag-bridge
Version: {VERSION}
Architecture: amd64
Maintainer: ExitLag Bridge contributors
Section: games
Priority: optional
Depends: python3 (>= 3.10), python3-tk, nftables, libmnl0, policykit-1, curl, ca-certificates, libfreetype6, libx11-6, libxext6, libxrender1, libxrandr2, libxcursor1, libxi6, libxinerama1, libxxf86vm1, libgl1, libvulkan1, fontconfig, libdbus-1-3, libudev1, libasound2 | libasound2t64, libgnutls30 | libgnutls30t64
Description: Unofficial ExitLag compatibility bridge
 Requires a separately downloaded official ExitLag installer and account.
 The proprietary client and account data are not included.
''')
    (control / 'prerm').write_text('''#!/bin/sh
set -eu
if pgrep -f '/usr/lib/exitlag-bridge/installers/linux/(app|run_root).py' >/dev/null; then
  echo 'Quit ExitLag Bridge before removing or upgrading it.' >&2
  exit 1
fi
exit 0
''')
    (control / 'prerm').chmod(0o755)
    subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(deb), str(output / f'exitlag-bridge_{VERSION}_amd64.deb')], check=True)
    portable = staging / f'exitlag-bridge-{VERSION}-linux-x86_64'; portable.mkdir()
    populate(portable / 'program')
    shutil.copy2(ROOT / 'installers/linux/install.sh', portable / 'install.sh')
    shutil.copy2(ROOT / 'installers/linux/uninstall.sh', portable / 'uninstall.sh')
    (portable / 'exitlag-bridge.desktop').write_text(desktop('/usr/local/lib/exitlag-bridge'))
    with tarfile.open(output / f'exitlag-bridge-{VERSION}-linux-x86_64.tar.gz', 'w:gz') as tar:
        def normalize(item):
            item.uid = item.gid = 0; item.uname = item.gname = 'root'; item.mtime = 0
            return item
        tar.add(portable, arcname=portable.name, filter=normalize)
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file() and p.suffix != '.json'}
    (output / 'SHA256SUMS.json').write_text(json.dumps(hashes, indent=2) + '\n')
    print(json.dumps(dict(packages_built=True, proprietary_client_included=False)))


if __name__ == '__main__': main()
