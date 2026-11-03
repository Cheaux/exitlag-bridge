"""Generated client identity and ownership boundaries; no Wine or account."""
import json
import os
from pathlib import Path
import plistlib
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'installers'))
import client_app
import client_icon


class ClientApplication(unittest.TestCase):
    def fixture(self, folder):
        state = Path(folder) / 'state'
        exe = state / 'prefix/drive_c/ExitLag/ExitLag.exe'
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b'MZ fixture')
        return state, exe

    def test_generated_app_identity_and_private_configuration(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(client_app.subprocess, 'run'):
            state, exe = self.fixture(folder)
            app = client_app.prepare_client_app(state, exe, '/Applications/ExitLag Bridge.app')
            info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
            self.assertEqual(info['CFBundleDisplayName'], 'ExitLag')
            self.assertEqual(info['CFBundleIdentifier'], 'local.cheaux.ExitLagClient')
            self.assertFalse(info['LSUIElement'])
            config = json.loads((app / 'Contents/Resources/client.json').read_text())
            self.assertEqual(config['client_relative'], 'drive_c/ExitLag/ExitLag.exe')
            self.assertEqual((app / 'Contents/Resources/client.json').stat().st_mode & 0o777, 0o600)
            self.assertEqual((app / 'Contents/MacOS/ExitLag').stat().st_mode & 0o777, 0o700)
            client_app.prepare_client_app(state, exe, '/Applications/ExitLag Bridge.app')

    def test_unrelated_application_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            state, exe = self.fixture(folder)
            app = state / 'ExitLag.app'; app.mkdir()
            note = app / 'unrelated'; note.write_text('keep')
            with self.assertRaises(ValueError):
                client_app.prepare_client_app(state, exe, '/Applications/ExitLag Bridge.app')
            self.assertEqual(note.read_text(), 'keep')

    def test_closed_dock_plugin_preserves_the_actual_client_launch_path(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(client_app.subprocess, 'run'), \
                mock.patch.object(client_app, 'client_icns', return_value=None):
            state, exe = self.fixture(folder)
            source = Path(folder) / 'packaged-bridge'
            source.mkdir()
            (source / 'client_entry.py').write_text('# entry fixture\n')
            plugin = source / 'ExitLagIcon.docktileplugin'
            for name in ('Contents/Info.plist', 'Contents/MacOS/ExitLagDockTile',
                         'Contents/_CodeSignature/CodeResources'):
                target = plugin / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'packaged plug-in fixture')
            with mock.patch.object(client_app, '__file__', str(source / 'client_app.py')):
                app = client_app.prepare_client_app(state, exe, '/Applications/ExitLag Bridge.app')
            config = json.loads((app / 'Contents/Resources/client.json').read_text())
            self.assertEqual(config['client_relative'], 'drive_c/ExitLag/ExitLag.exe')
            self.assertEqual(plistlib.loads((app / 'Contents/Info.plist').read_bytes())['NSDockTilePlugIn'],
                             'ExitLagIcon.docktileplugin')

    def test_icon_is_attached_without_being_shipped_with_the_bridge(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(client_app.subprocess, 'run'), \
                mock.patch.object(client_app, 'client_icns', return_value=b'icns local fixture'):
            state, exe = self.fixture(folder)
            app = client_app.prepare_client_app(state, exe, '/Applications/ExitLag Bridge.app')
            self.assertEqual(plistlib.loads((app / 'Contents/Info.plist').read_bytes())['CFBundleIconFile'], 'ClientIcon')
            self.assertEqual((app / 'Contents/Resources/ClientIcon.icns').read_bytes(), b'icns local fixture')

    def test_linked_app_and_external_client_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            state, exe = self.fixture(folder)
            outside = Path(folder) / 'outside'; outside.mkdir()
            (state / 'ExitLag.app').symlink_to(outside)
            with self.assertRaises(ValueError):
                client_app.prepare_client_app(state, exe, '/Applications/ExitLag Bridge.app')
            self.assertEqual(list(outside.iterdir()), [])
            with self.assertRaises(ValueError):
                client_app.prepare_client_app(state, outside / 'ExitLag.exe', '/Applications/ExitLag Bridge.app')

    def test_updated_files_preserve_the_bytes_of_existing_readers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'mapped-dock-code'
            client_app.write_owned(path, b'old code', 0o600)
            with path.open('rb') as loaded:
                client_app.write_owned(path, b'new code', 0o600)
                self.assertEqual(loaded.read(), b'old code')
            self.assertEqual(path.read_bytes(), b'new code')

    def test_links_cannot_truncate_an_unrelated_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); original = root / 'original'; original.write_bytes(b'keep')
            for link in ('symbolic', 'hard'):
                path = root / link
                if link == 'symbolic': path.symlink_to(original)
                else: os.link(original, path)
                with self.assertRaises((OSError, ValueError)):
                    client_app.write_owned(path, b'overwrite', 0o600)
                self.assertEqual(original.read_bytes(), b'keep')


class ClientIconResources(unittest.TestCase):
    def fixture(self):
        # Resource directories: icon -> id -> language, group -> id -> language.
        data = bytearray(1024)
        struct.pack_into('<I', data, 60, 0x80)
        struct.pack_into('<II', data, 0x80 + 24 + 112 + 16, 0x1000, 188)
        for relative, children in ((0, [(3, 32), (14, 80)]), (32, [(7, 56)]),
                                   (56, [(1033, 128)]), (80, [(1000, 104)]), (104, [(1033, 144)])):
            struct.pack_into('<HH', data, 512 + relative + 12, 0, len(children))
            for i, (name, child) in enumerate(children):
                struct.pack_into('<II', data, 512 + relative + 16 + 8 * i, name,
                                 child | (0x80000000 if relative not in (56, 104) else 0))
        struct.pack_into('<II', data, 512 + 128, 0x1000 + 180, 8)
        struct.pack_into('<II', data, 512 + 144, 0x1000 + 160, 20)
        struct.pack_into('<HHHBBBBHHIH', data, 512 + 160, 0, 1, 1, 75, 75, 0, 0, 1, 32, 8, 7)
        data[512 + 180:512 + 188] = b'iconbits'
        def offset(rva, size=1):
            start = rva - 0x1000 + 512
            if start < 512 or start + size > len(data): raise ValueError('Unbacked resource')
            return start
        return SimpleNamespace(data=data, width=8, offset=offset)

    def test_icon_group_resolves_ids_and_builds_ico_offsets(self):
        with mock.patch.object(client_icon, 'PE', return_value=self.fixture()):
            ico = client_icon.client_ico('fixture.exe')
        self.assertEqual(struct.unpack_from('<HHH', ico), (0, 1, 1))
        self.assertEqual(struct.unpack_from('<II', ico, 14), (8, 22))
        self.assertEqual(ico[22:], b'iconbits')

    def test_directory_cannot_escape_its_declared_resource_range(self):
        pe = self.fixture()
        struct.pack_into('<II', pe.data, 0x80 + 24 + 112 + 16, 0x1000, 24)
        with mock.patch.object(client_icon, 'PE', return_value=pe), self.assertRaises(ValueError):
            client_icon.client_ico('fixture.exe')


if __name__ == '__main__':
    unittest.main(verbosity=2)
