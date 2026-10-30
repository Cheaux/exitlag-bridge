#!/usr/bin/env python3
"""Sign the staged Mac program before pkgbuild. No key material is stored here.
SPDX-License-Identifier: GPL-3.0-only
"""
import argparse
from pathlib import Path
import subprocess

MAGIC = {b'\xce\xfa\xed\xfe', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xce', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca'}


def binaries(root):
    root = Path(root).resolve(strict=True)
    found = []
    for path in root.rglob('*'):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open('rb') as stream:
            if stream.read(4) in MAGIC:
                found.append(path)
    return sorted(found, key=lambda p: len(p.parts), reverse=True)


def sign(root, identity, allow_adhoc=False):
    if identity == '-' and not allow_adhoc:
        raise ValueError('A production build needs a Developer ID Application identity')
    root = Path(root).resolve(strict=True)
    app = root / 'Applications/ExitLag Bridge.app'
    if not (app / 'Contents/Info.plist').is_file():
        raise ValueError('Expected the staged ExitLag Bridge application')
    paths = binaries(root)
    if not paths:
        raise ValueError('No staged Mach-O components were found')
    options = ['--force', '--options', 'runtime', '--sign', identity]
    if identity != '-':
        options.append('--timestamp')
    for path in paths:
        subprocess.run(['/usr/bin/codesign', *options, str(path)], check=True)
    subprocess.run(['/usr/bin/codesign', *options, str(app)], check=True)
    for path in paths:
        subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(path)], check=True)
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', '--deep', str(app)], check=True)
    return len(paths)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('payload', type=Path)
    parser.add_argument('--identity', required=True)
    parser.add_argument('--allow-adhoc-for-test', action='store_true')
    args = parser.parse_args()
    count = sign(args.payload, args.identity, args.allow_adhoc_for_test)
    print('Verified signatures for %d staged Mach-O components and the app bundle.' % count)


if __name__ == '__main__': main()
