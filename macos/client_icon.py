"""Convert the user's installed client icon locally; no vendor image is shipped.
SPDX-License-Identifier: GPL-3.0-only
"""
from pathlib import Path
import struct
import subprocess
import tempfile
import xml.etree.ElementTree as ET

from bridge_setup.pe import PE


def client_logo_svg(executable):
    """Use the client's transparent dashboard mark instead of its old tile.

    The supported client embeds this small SVG without compression. Keep its
    paths local, accept only that self-contained asset, and leave the ICO as the
    fallback for builds with a different resource layout.
    """
    data = PE(executable).data
    marker = b'<svg width="40" height="40" viewBox="0 0 30 27"'
    start = data.find(marker)
    if start < 0:
        return None
    end = data.find(b'</svg>', start, start + 16384)
    if end < 0:
        return None
    try:
        root = ET.fromstring(data[start:end + 6])
    except ET.ParseError:
        return None
    paths = list(root)
    if (root.tag != '{http://www.w3.org/2000/svg}svg' or not 1 <= len(paths) <= 8
            or any(path.tag != '{http://www.w3.org/2000/svg}path'
                   or set(path.attrib) != {'d', 'fill'} or len(path)
                   or path.get('fill') not in ('white', '#A21916') for path in paths)):
        return None
    # The original UI viewport clips the lower edge. This square includes the
    # whole mark and gives it 12.5% transparent padding on each side in the Dock.
    root.set('width', '1024')
    root.set('height', '1024')
    root.set('viewBox', '0 0 32 32')
    return ET.tostring(root, encoding='utf-8')


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
        logo = client_logo_svg(executable)
        if logo is not None:
            source = Path(folder) / 'client.svg'
            source.write_bytes(logo)
            iconset = Path(folder) / 'ClientIcon.iconset'
            iconset.mkdir()
            for size in (16, 32, 128, 256, 512):
                for scale in (1, 2):
                    suffix = '@2x' if scale == 2 else ''
                    target = iconset / f'icon_{size}x{size}{suffix}.png'
                    pixels = str(size * scale)
                    subprocess.run(['/usr/bin/sips', '-z', pixels, pixels, '-s', 'format',
                                    'png', str(source), '--out', str(target)], check=True,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            target = Path(folder) / 'ClientIcon.icns'
            subprocess.run(['/usr/bin/iconutil', '-c', 'icns', str(iconset), '-o', str(target)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
            return target.read_bytes()
        source, target = Path(folder) / 'client.ico', Path(folder) / 'ClientIcon.icns'
        source.write_bytes(client_ico(executable))
        # Windows icons may use arbitrary dimensions (the current one is 75px).
        # ICNS requires a supported size even when ImageIO can decode the ICO.
        subprocess.run(['/usr/bin/sips', '-z', '256', '256', '-s', 'format', 'icns', str(source), '--out', str(target)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        return target.read_bytes()
