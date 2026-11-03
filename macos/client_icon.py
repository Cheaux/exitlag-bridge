"""Convert the user's installed client icon locally; no vendor image is shipped.
SPDX-License-Identifier: GPL-3.0-only
"""
from pathlib import Path
import struct
import subprocess
import tempfile

from bridge_setup.pe import PE


def client_ico(executable):
    pe = PE(executable)
    nt = struct.unpack_from('<I', pe.data, 60)[0]
    directory = nt + 24 + (112 if pe.width == 8 else 96)
    rva, length = struct.unpack_from('<II', pe.data, directory + 16)
    if not rva or not length:
        raise ValueError('No client icon resources')

    def offset(relative, size):
        if relative < 0 or relative + size > length:
            raise ValueError('Resource outside its directory')
        return pe.offset(rva + relative, size)

    def entries(relative):
        position = offset(relative, 16)
        named, numeric = struct.unpack_from('<HH', pe.data, position + 12)
        if named + numeric > 4096:
            raise ValueError('Too many resource entries')
        return [struct.unpack_from('<II', pe.data, offset(relative + 16 + i * 8, 8))
                for i in range(named + numeric)]

    def resource(kind, identity=None):
        branch = 0
        for key in (kind, identity, None):
            candidates = [(name, value) for name, value in entries(branch)
                          if not name & 0x80000000 and (key is None or name == key)]
            if not candidates:
                raise ValueError('Missing client icon resource')
            _, value = min(candidates)
            if value & 0x80000000:
                branch = value & 0x7fffffff
            else:
                position = offset(value, 16)
                address, size = struct.unpack_from('<II', pe.data, position)
                if size > 16 * 1024 * 1024:
                    raise ValueError('Oversized client icon')
                start = pe.offset(address, size)
                return pe.data[start:start + size]
        raise ValueError('Incomplete client icon resource')

    group = resource(14)
    reserved, kind, count = struct.unpack_from('<HHH', group)
    if reserved or kind != 1 or not 0 < count <= 128 or len(group) != 6 + 14 * count:
        raise ValueError('Invalid client icon group')
    images, records = [], []
    position = 6 + 16 * count
    for index in range(count):
        width, height, colors, reserved, planes, bits, _, identity = struct.unpack_from(
            '<BBBBHHIH', group, 6 + 14 * index)
        data = resource(3, identity)
        records.append(struct.pack('<BBBBHHII', width, height, colors, reserved,
                                   planes, bits, len(data), position))
        images.append(data)
        position += len(data)
        if position > 16 * 1024 * 1024:
            raise ValueError('Oversized client icon group')
    return struct.pack('<HHH', 0, 1, count) + b''.join(records + images)


def client_icns(executable):
    with tempfile.TemporaryDirectory(prefix='exitlag-icon-') as folder:
        source, target = Path(folder) / 'client.ico', Path(folder) / 'ClientIcon.icns'
        source.write_bytes(client_ico(executable))
        # Windows icons may use arbitrary dimensions (the current one is 75px).
        # ICNS requires a supported size even when ImageIO can decode the ICO.
        subprocess.run(['/usr/bin/sips', '-z', '256', '256', '-s', 'format', 'icns', str(source), '--out', str(target)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        return target.read_bytes()
