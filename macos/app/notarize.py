#!/usr/bin/env python3
"""Submit already Developer-ID-signed installer packages using a keychain profile.
SPDX-License-Identifier: GPL-3.0-only
"""
import argparse
import json
from pathlib import Path
import subprocess


def notarize(package, profile):
    package = Path(package).resolve(strict=True)
    subprocess.run(['/usr/sbin/pkgutil', '--check-signature', str(package)], check=True)
    result = subprocess.run(['/usr/bin/xcrun', 'notarytool', 'submit', str(package),
                             '--keychain-profile', profile, '--wait', '--output-format', 'json'],
                            check=True, capture_output=True, text=True)
    response = json.loads(result.stdout)
    if response.get('status') != 'Accepted':
        raise RuntimeError('Apple did not accept this submission; inspect its notary log before distributing it')
    subprocess.run(['/usr/bin/xcrun', 'stapler', 'staple', str(package)], check=True)
    subprocess.run(['/usr/bin/xcrun', 'stapler', 'validate', str(package)], check=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True)
    parser.add_argument('packages', type=Path, nargs='+')
    args = parser.parse_args()
    for package in args.packages: notarize(package, args.profile)
