"""Verified downloads and private, ownership-scoped installer state.
SPDX-License-Identifier: GPL-3.0-only
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time

PROJECT = 'exitlag-bridge'


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(data)
    return result.hexdigest()


def private_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.bridge-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, sort_keys=True)
            output.write('\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def read_private(path, limit=65536):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077 or info.st_size > limit):
            raise ValueError('Unexpected private setup record')
        return json.loads(os.read(fd, limit + 1))
    finally:
        os.close(fd)


def owned_directory(path, create=False):
    path = Path(path).expanduser()
    if not path.is_absolute() or path in (Path('/'), Path.home()) or path.is_symlink():
        raise ValueError('Choose a dedicated setup directory')
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('Setup directory must be owned by this user and not writable by others')
    return path


def download(asset, cache, emit=lambda *args: None):
    cache = owned_directory(cache, create=True)
    expected = asset['sha256']
    if len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected):
        raise ValueError('Invalid pinned asset digest')
    target = cache / expected
    if target.exists():
        if target.is_symlink() or not target.is_file() or digest(target) != expected:
            raise ValueError('Cached download failed verification')
        return target
    url = asset['url']
    if not url.startswith('https://'):
        raise ValueError('Downloads require HTTPS')
    emit('download', 'Downloading a verified setup component')
    fd, temporary = tempfile.mkstemp(prefix='.download-', dir=cache)
    os.close(fd)
    try:
        subprocess.run(['curl', '--fail', '--location', '--silent', '--show-error',
                        '--proto', '=https', '--proto-redir', '=https',
                        '--connect-timeout', '30', '--max-time', '1800',
                        '--output', temporary, url], check=True)
        if digest(temporary) != expected:
            raise ValueError('Download checksum mismatch; component was not used')
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return target


def extract(archive, destination):
    """Reject traversal, device nodes and links escaping the extraction tree."""
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError('Extraction destination must not be a symlink')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    base = destination.resolve()
    def inside(path):
        try:
            path.resolve().relative_to(base)
            return True
        except ValueError:
            return False
    with tarfile.open(archive, 'r:*') as source:
        members = source.getmembers()
        if len(members) > 100000 or sum(m.size for m in members) > 5 * 1024**3:
            raise ValueError('Archive exceeds setup bounds')
        for item in members:
            name = PurePosixPath(item.name)
            if name.is_absolute() or '..' in name.parts or not inside(base / item.name):
                raise ValueError('Unsafe archive path')
            if not (item.isdir() or item.isfile() or item.issym() or item.islnk()):
                raise ValueError('Unsupported archive member')
            if item.issym() or item.islnk():
                link = PurePosixPath(item.linkname)
                target = (base / item.name).parent / item.linkname if item.issym() else base / item.linkname
                if link.is_absolute() or not inside(target):
                    raise ValueError('Archive link leaves its extraction directory')
            # Check again after earlier members may have created a symlink.
            if not inside(base / item.name):
                raise ValueError('Archive parent escapes extraction directory')
            source.extract(item, destination, set_attrs=False)
            if item.isdir():
                os.chmod(base / item.name, 0o755)
            if item.isfile() and not (base / item.name).is_symlink():
                os.chmod(base / item.name, 0o755 if item.mode & 0o111 else 0o644)
                os.utime(base / item.name, (item.mtime, item.mtime))


def replace_tree(source, target):
    """Install only into a new target; never replace an unverified directory."""
    source, target = Path(source), Path(target)
    if target.exists() or target.is_symlink():
        raise ValueError('Setup target already exists')
    staging = Path(tempfile.mkdtemp(prefix='.runtime-', dir=target.parent))
    staging.rmdir()
    try:
        shutil.copytree(source, staging, symlinks=True)
        staging.rename(target)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


class State:
    def __init__(self, path, create=False):
        self.path = Path(path).expanduser().absolute()
        self.marker = self.path / '.bridge-owner.json'
        existed = self.path.exists()
        if not existed and not create:
            raise FileNotFoundError('No managed setup exists')
        owned_directory(self.path, create=create)
        if self.marker.exists():
            record = read_private(self.marker)
            if record != {'project': PROJECT, 'schema': 1, 'uid': os.getuid()}:
                raise ValueError('Setup ownership marker does not match')
        elif create:
            if existed and any(self.path.iterdir()):
                raise ValueError('Existing unmarked directory was preserved; choose a new setup location')
            private_json(self.marker, dict(project=PROJECT, schema=1, uid=os.getuid()))
        else:
            raise ValueError('Setup ownership marker is missing')

    def relative(self, name):
        part = PurePosixPath(name)
        if part.is_absolute() or '..' in part.parts or not part.parts:
            raise ValueError('Invalid managed path')
        result = self.path / name
        try:
            result.resolve().relative_to(self.path.resolve())
        except ValueError:
            raise ValueError('Managed path escapes setup directory')
        return result

    def save(self, value):
        private_json(self.relative('setup-manifest.json'), value)

    def load(self):
        try:
            return read_private(self.relative('setup-manifest.json'))
        except FileNotFoundError:
            return {'schema': 1, 'project': PROJECT, 'complete': False}

    def quarantine_incomplete_prefix(self):
        prefix = self.relative('prefix')
        if prefix.exists() and not (prefix / 'drive_c').is_dir():
            backups = self.relative('backups')
            backups.mkdir(mode=0o700, exist_ok=True)
            prefix.rename(backups / ('incomplete-prefix-' + str(time.time_ns())))

    def remove_components(self):
        for name in ('runtime', 'cache'):
            path = self.relative(name)
            if path.is_symlink():
                raise ValueError('Refusing a linked managed directory')
            if path.exists():
                shutil.rmtree(path)
        self.save(dict(schema=1, project=PROJECT, complete=False, removed=True))

    def purge(self):
        # Revalidate the marker immediately before removing the private state.
        State(self.path)
        shutil.rmtree(self.path)
