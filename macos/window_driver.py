"""Install the packaged, version-matched Wine window correction as the game user.
SPDX-License-Identifier: GPL-3.0-only
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

MODULE = 'lib/wine/x86_64-unix/winemac.so'
RECORD = 'exitlag-window-animation.json'


def read_file(path, private=False, asset=False, limit=16 * 1024 * 1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        owners = (0, os.getuid()) if asset else (os.getuid(),)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in owners or info.st_nlink != 1 or
                info.st_size > limit or (private and info.st_mode & 0o077)):
            raise ValueError('Unexpected window-correction file')
        data = os.read(fd, info.st_size + 1)
        if len(data) != info.st_size:
            raise ValueError('Window-correction file changed while reading')
        return data
    finally:
        os.close(fd)


def atomic_file(path, data, mode):
    fd, temporary = tempfile.mkstemp(prefix='.window-driver-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as output:
            fd = -1
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        Path(temporary).unlink(missing_ok=True)


def private_directory(path):
    if path.is_symlink():
        raise ValueError('Linked window-correction directory')
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Window-correction directory is not private')


def install_window_driver(state, assets):
    """Return installed/current/preserved; never overwrite an unknown Wine build."""
    if os.geteuid() == 0:
        raise ValueError('The window correction must run as the game user')
    state, assets = Path(state), Path(assets)
    private_directory(state)
    runtime = state / 'runtime'
    target = runtime / MODULE
    parent = state
    for component in ('runtime', 'lib', 'wine', 'x86_64-unix'):
        parent = parent / component
        if parent.is_symlink() or not parent.is_dir() or parent.stat().st_uid != os.getuid():
            raise ValueError('Unexpected private Wine directory')
    manifest = json.loads(read_file(assets / 'manifest.json', asset=True, limit=16384))
    if not isinstance(manifest, dict) or manifest.get('schema') != 1 or manifest.get('project') != 'exitlag-bridge':
        raise ValueError('Unsupported packaged window correction')
    for key in ('original_sha256', 'patched_sha256', 'source_commit', 'patch_sha256'):
        value = manifest.get(key)
        length = 40 if key == 'source_commit' else 64
        if not isinstance(value, str) or len(value) != length or any(c not in '0123456789abcdef' for c in value):
            raise ValueError('Invalid window-correction fingerprint')
    replacement = read_file(assets / 'winemac.so', asset=True)
    digest = lambda data: hashlib.sha256(data).hexdigest()
    if digest(replacement) != manifest['patched_sha256']:
        raise ValueError('Packaged window correction failed verification')
    current = read_file(target)
    current_hash = digest(current)
    record_path = runtime / RECORD
    record = {}
    if record_path.exists() or record_path.is_symlink():
        record = json.loads(read_file(record_path, private=True, limit=16384))
        if not isinstance(record, dict):
            raise ValueError('Invalid saved window-correction record')
    if current_hash == manifest['patched_sha256']:
        return 'current'
    known_previous = (record.get('module') == MODULE and
                      record.get('original_sha256') == manifest['original_sha256'] and
                      record.get('source_commit') == manifest['source_commit'] and
                      current_hash in (record.get('patched_sha256'), record.get('pending_previous_sha256')))
    if current_hash != manifest['original_sha256'] and not known_previous:
        return 'preserved'
    backups = state / '.window-driver-backups'
    private_directory(backups)
    backup = backups / (current_hash + '.so')
    if backup.exists() or backup.is_symlink():
        if digest(read_file(backup, private=True)) != current_hash:
            raise ValueError('The saved Wine window driver changed')
    else:
        atomic_file(backup, current, 0o600)
    record.update(schema=1, project='exitlag-bridge', module=MODULE,
                  original_sha256=manifest['original_sha256'], patched_sha256=manifest['patched_sha256'],
                  source_commit=manifest['source_commit'], patch_sha256=manifest['patch_sha256'],
                  window_driver_backup=str(backup.relative_to(state)), pending_previous_sha256=current_hash)
    # Publish provenance first: if interrupted, the still-original module remains
    # recognizable by its stock hash and a subsequent launch can finish safely.
    atomic_file(record_path, (json.dumps(record, sort_keys=True) + '\n').encode(), 0o600)
    atomic_file(target, replacement, 0o700)
    record.pop('pending_previous_sha256')
    atomic_file(record_path, (json.dumps(record, sort_keys=True) + '\n').encode(), 0o600)
    return 'installed'
