#!/usr/bin/env python3
"""Installed Linux helper lifecycle in a disposable network namespace.
No vendor client, login or external traffic is used.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

root = Path(sys.argv[1]).resolve(strict=True)
uid = os.getuid()
assert uid >= 1000 and os.geteuid() != 0


def nft(*args):
    return subprocess.run(['sudo', '-n', 'nft', *args], capture_output=True, text=True)


def wait_ready(child):
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        assert child.poll() is None, 'Installed routing supervisor exited before readiness'
        result = nft('-j', 'list', 'table', 'inet', 'exitlag')
        if result.returncode == 0:
            rules = json.loads(result.stdout)['nftables']
            queues = [x['rule'] for x in rules if 'rule' in x and x['rule'].get('chain') == 'output_queue']
            assert any(any(e.get('match', {}).get('left', {}).get('meta', {}).get('key') == 'skuid'
                               and e['match']['right'] == uid for e in r['expr']) for r in queues), 'Queue is not scoped to the authorized UID'
            return
        time.sleep(.1)
    raise AssertionError('Installed routing helper did not become ready')


assert nft('list', 'table', 'inet', 'exitlag').returncode != 0, 'Test requires an empty disposable namespace'
with tempfile.TemporaryDirectory(prefix='installed helper ') as temporary:
    base = Path(temporary)
    mapping = base / 'games.map'; mapping.write_text('/usr/bin/sleep\tC:\\Games\\sleep.exe\n'); mapping.chmod(0o600)
    lease = base / 'session.lease'
    for reason in ('lease_removed', 'lease_expired', 'parent_exited'):
        parent = subprocess.Popen(['/usr/bin/sleep', '60'])
        lease.write_text('run\n'); lease.chmod(0o600)
        command = ['sudo', '-n', 'env', 'SUDO_UID=' + str(uid), '/usr/bin/python3', '-I', str(root / 'installers/linux/run_root.py'), '--uid', str(uid), '--parent', str(parent.pid), '--map', str(mapping), '--lease', str(lease)]
        diagnostics = tempfile.TemporaryFile(mode='w+')
        child = subprocess.Popen(command, stdout=diagnostics, stderr=diagnostics)
        try:
            wait_ready(child)
            if reason == 'lease_removed': lease.unlink()
            elif reason == 'lease_expired': os.utime(lease, (time.time() - 20, time.time() - 20))
            else: parent.terminate(); parent.wait(timeout=5)
            assert child.wait(timeout=15) == 0, 'Installed supervisor did not stop cleanly: ' + reason
            assert nft('list', 'table', 'inet', 'exitlag').returncode != 0, 'Owned firewall table was left behind: ' + reason
            print('PASS installed routing cleanup: ' + reason)
        except BaseException:
            diagnostics.flush(); diagnostics.seek(0)
            print('Installed supervisor diagnostics:\n' + diagnostics.read()[-12000:])
            log = subprocess.run(['sudo', '-n', 'tail', '-40', '/run/exitlag-bridge-' + str(uid) + '/backend.log'], capture_output=True, text=True)
            print('Account-free backend diagnostics:\n' + log.stdout + log.stderr)
            raise
        finally:
            lease.unlink(missing_ok=True)
            if parent.poll() is None: parent.terminate(); parent.wait(timeout=5)
            if child.poll() is None: child.wait(timeout=15)
            diagnostics.close()
print('Installed helper start, UID scope, lease expiry and parent-exit cleanup verified in an isolated network namespace.')
