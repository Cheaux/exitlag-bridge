"""Version-scoped, atomic runtime upgrades without Wine or a user account."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import window_driver as driver

sha = lambda data: hashlib.sha256(data).hexdigest()


class WindowDriverInstall(unittest.TestCase):
    def fixture(self, root):
        root = Path(root); state = root / 'state'; state.mkdir(mode=0o700)
        target = state / 'runtime' / driver.MODULE
        target.parent.mkdir(parents=True)
        original, replacement = b'stock Wine driver', b'packaged corrected driver'
        target.write_bytes(original)
        assets = root / 'assets'; assets.mkdir()
        (assets / 'winemac.so').write_bytes(replacement)
        manifest = dict(schema=1, project='exitlag-bridge', original_sha256=sha(original),
                        patched_sha256=sha(replacement), source_commit='a' * 40, patch_sha256='b' * 64)
        (assets / 'manifest.json').write_text(json.dumps(manifest))
        return state, target, assets, original, replacement, manifest

    def test_stock_install_backup_and_idempotent_reopen(self):
        with tempfile.TemporaryDirectory() as root:
            state, target, assets, original, replacement, _ = self.fixture(root)
            self.assertEqual(driver.install_window_driver(state, assets), 'installed')
            self.assertEqual(target.read_bytes(), replacement)
            backup = state / '.window-driver-backups' / (sha(original) + '.so')
            self.assertEqual(backup.read_bytes(), original)
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
            self.assertEqual(driver.install_window_driver(state, assets), 'current')

    def test_upgrade_from_recorded_local_fix(self):
        with tempfile.TemporaryDirectory() as root:
            state, target, assets, original, replacement, manifest = self.fixture(root)
            prior = b'previous local animation fix'; target.write_bytes(prior)
            record = dict(module=driver.MODULE, original_sha256=sha(original),
                          patched_sha256=sha(prior), source_commit=manifest['source_commit'])
            path = state / 'runtime' / driver.RECORD
            path.write_text(json.dumps(record)); path.chmod(0o600)
            self.assertEqual(driver.install_window_driver(state, assets), 'installed')
            self.assertEqual(target.read_bytes(), replacement)
            self.assertEqual((state / '.window-driver-backups' / (sha(prior) + '.so')).read_bytes(), prior)

    def test_unknown_runtime_is_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            state, target, assets, *_ = self.fixture(root)
            target.write_bytes(b'an unrelated Wine build')
            self.assertEqual(driver.install_window_driver(state, assets), 'preserved')
            self.assertEqual(target.read_bytes(), b'an unrelated Wine build')
            self.assertFalse((state / '.window-driver-backups').exists())

    def test_corrupt_asset_and_linked_target_do_not_change_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            state, target, assets, original, replacement, _ = self.fixture(root)
            (assets / 'winemac.so').write_bytes(b'corrupt')
            with self.assertRaises(ValueError): driver.install_window_driver(state, assets)
            self.assertEqual(target.read_bytes(), original)
            (assets / 'winemac.so').write_bytes(replacement)
            record = state / 'runtime' / driver.RECORD
            record.write_text('[]'); record.chmod(0o600)
            with self.assertRaises(ValueError): driver.install_window_driver(state, assets)
            self.assertEqual(target.read_bytes(), original)
        with tempfile.TemporaryDirectory() as root:
            state, target, assets, original, *_ = self.fixture(root)
            outside = Path(root) / 'outside'; outside.write_bytes(original)
            target.unlink(); target.symlink_to(outside)
            with self.assertRaises(OSError): driver.install_window_driver(state, assets)
            self.assertEqual(outside.read_bytes(), original)

    def test_interrupted_upgrade_resumes_from_previous_hash(self):
        with tempfile.TemporaryDirectory() as root:
            state, target, assets, original, replacement, manifest = self.fixture(root)
            previous = b'previous correction'; target.write_bytes(previous)
            record = dict(module=driver.MODULE, original_sha256=sha(original),
                          patched_sha256=manifest['patched_sha256'], source_commit=manifest['source_commit'],
                          pending_previous_sha256=sha(previous))
            path = state / 'runtime' / driver.RECORD
            path.write_text(json.dumps(record)); path.chmod(0o600)
            self.assertEqual(driver.install_window_driver(state, assets), 'installed')
            self.assertEqual(target.read_bytes(), replacement)
            self.assertNotIn('pending_previous_sha256', json.loads(path.read_text()))

    def test_failed_replacement_keeps_old_bytes_and_can_retry(self):
        with tempfile.TemporaryDirectory() as root:
            state, target, assets, original, replacement, _ = self.fixture(root)
            replace = driver.os.replace
            def fail_module(source, destination):
                if Path(destination) == target: raise OSError('simulated interruption')
                return replace(source, destination)
            with mock.patch.object(driver.os, 'replace', side_effect=fail_module):
                with self.assertRaises(OSError): driver.install_window_driver(state, assets)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(driver.install_window_driver(state, assets), 'installed')
            self.assertEqual(target.read_bytes(), replacement)


if __name__ == '__main__':
    unittest.main(verbosity=2)
