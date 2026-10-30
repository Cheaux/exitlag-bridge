#!/usr/bin/env python3
"""Audit committed source and reachable commit metadata before sharing it.
SPDX-License-Identifier: GPL-3.0-only
This detects specific privacy hazards; it is not a legal or security clearance.
"""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import subprocess

HOME_PATH = re.compile(r'/(?:Users|home)/([^/\s\x60\"\'<>]+)')
PLACEHOLDERS = {'USER', 'user', 'Shared', 'example', '…'}
IPV4 = re.compile(r'(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])')
DOCUMENTED_NETWORK_BASES = {'10.0.0.0', '172.16.0.0', '192.168.0.0'}
PRIVATE_NETWORKS = tuple(ipaddress.ip_network(x) for x in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))
SECRET_PATTERNS = (
    re.compile(rb'gh[pousr]_[A-Za-z0-9]{20,}'),
    re.compile(rb'github_pat_[A-Za-z0-9_]{20,}'),
    re.compile(rb'AKIA[0-9A-Z]{16}'),
    re.compile(rb'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'),
)
BINARY_MAGIC = (b'MZ', b'\x7fELF', b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe')
PRIVATE_SUFFIXES = {'.log', '.dmp', '.reg', '.db', '.token', '.pem', '.key', '.pyc', '.pkg', '.deb', '.exe', '.dll'}


def git(root, *arguments):
    return subprocess.check_output(['git', '-C', str(root), *arguments])


def inspect_file(name, data):
    findings = []
    path = Path(name)
    if path.suffix.lower() in PRIVATE_SUFFIXES or path.name == '.env' or path.name.startswith('.env.'):
        findings.append('runtime, credential or generated-artifact filename')
    if any(data.startswith(magic) for magic in BINARY_MAGIC):
        findings.append('compiled binary in source tree')
    if any(pattern.search(data) for pattern in SECRET_PATTERNS):
        findings.append('credential or private-key pattern')
    text = data.decode('utf-8', errors='replace')
    if not name.startswith('LICENSES/') and name != 'LICENSE':
        if any(match.group(1) not in PLACEHOLDERS for match in HOME_PATH.finditer(text)):
            findings.append('non-placeholder home-directory path')
        for raw in IPV4.findall(text):
            try:
                address = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if raw not in DOCUMENTED_NETWORK_BASES and any(address in network for network in PRIVATE_NETWORKS):
                findings.append('private network address')
                break
    return findings


def audit(root, revision='HEAD'):
    issues = []
    names = git(root, 'ls-tree', '-r', '--name-only', '-z', revision).decode().split('\0')
    names = [name for name in names if name]
    for name in names:
        data = git(root, 'show', revision + ':' + name)
        for finding in inspect_file(name, data):
            issues.append({'file': name, 'finding': finding})
    commits = git(root, 'rev-list', '--all').decode().splitlines()
    for commit in commits:
        emails = git(root, 'show', '-s', '--format=%ae%n%ce', commit).decode().splitlines()
        if any(not (email.endswith('@users.noreply.github.com') or email == 'noreply@github.com') for email in emails):
            issues.append({'commit': commit[:12], 'finding': 'non-noreply author or committer email'})
    return {'files_checked': len(names), 'commits_checked': len(commits), 'findings': issues,
            'passed': not issues, 'scope': 'committed files and reachable email metadata; manual review still required'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--revision', default='HEAD')
    args = parser.parse_args()
    result = audit(args.repo, args.revision)
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['passed'] else 1)


if __name__ == '__main__':
    main()
