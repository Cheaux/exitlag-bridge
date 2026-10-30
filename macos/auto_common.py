"""Bounded private local records shared by the ExitLag app and routing helper."""
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile


def runtime_for(uid):
    return Path('/var/run') / f'cheaux-exitlag-{uid}'


def read_private(path, uid, limit=16384):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != uid
                or info.st_mode & 0o077 or info.st_size > limit):
            raise ValueError('Unexpected private record')
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        value = json.loads(os.read(fd, limit + 1))
        if not isinstance(value, dict):
            raise ValueError('Expected an object')
        return value, info.st_mtime_ns
    finally:
        os.close(fd)


def write_control(path, uid, value):
    """The root-owned parent prevents a user replacing the control file."""
    data = (json.dumps(value, separators=(',', ':')) + '\n').encode()
    if len(data) > 512:
        raise ValueError('Control request too large')
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != uid
                or info.st_mode & 0o077):
            raise ValueError('Unexpected control file')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.pwrite(fd, data, 0) != len(data):
            raise OSError('Short control write')
        os.ftruncate(fd, len(data))
    finally:
        os.close(fd)


def publish(path, uid, value):
    data = (json.dumps(value, separators=(',', ':')) + '\n').encode()
    if len(data) > 16384:
        raise ValueError('Status too large')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid not in (uid, os.geteuid())):
            raise ValueError('Unexpected status file')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.fchmod(fd, 0o600)
        if os.geteuid() == 0:
            os.fchown(fd, uid, -1)
        if os.pwrite(fd, data, 0) != len(data):
            raise OSError('Short status write')
        os.ftruncate(fd, len(data))
    finally:
        os.close(fd)


def atomic_private(path, value):
    """Only for the ordinary user's own private application-support directory."""
    fd, temporary = tempfile.mkstemp(prefix='.exitlag-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            json.dump(value, out)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def valid_request(value, epoch):
    return (set(value) == {'version', 'epoch', 'run'} and value.get('version') == 1
            and value.get('epoch') == epoch and type(value.get('run')) is bool)
