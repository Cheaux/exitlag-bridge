#!/usr/bin/env python3
"""Account-free Linux supervisor and package-boundary checks."""
import importlib.util
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('supervisor', ROOT / 'installers/linux/run_root.py')
module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)


class Supervisor(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / 'private'
        self.path.write_text('/opt/games/WowClassic.exe\tC:\\Games\\WowClassic.exe\n')
        self.path.chmod(0o600)

    def tearDown(self): self.temporary.cleanup()

    def test_map_is_data_not_a_root_command(self):
        text = module.read_map(self.path, os.getuid())
        self.assertIn('WowClassic.exe', text)
        for unsafe in ('relative\tC:\\Game.exe\n', '/game\tcommand\n', '/game\tC:\\Game.exe\0\n', '/game\tC:\\Game.exe\textra\n', '# empty\n'):
            self.path.write_text(unsafe)
            with self.assertRaises(ValueError): module.read_map(self.path, os.getuid())

    def test_map_links_public_files_and_other_owners_are_refused(self):
        with self.assertRaises(ValueError): module.read_map(self.path, os.getuid() + 1)
        self.path.chmod(0o644)
        with self.assertRaises(ValueError): module.read_map(self.path, os.getuid())
        self.path.chmod(0o600)
        link = self.path.with_suffix('.link'); link.symlink_to(self.path)
        with self.assertRaises(OSError): module.read_map(link, os.getuid())

    def test_expired_or_removed_lease_stops_routing(self):
        self.path.write_text('run\n')
        self.assertTrue(module.lease_alive(self.path, os.getuid()))
        os.utime(self.path, (time.time() - 10, time.time() - 10))
        self.assertFalse(module.lease_alive(self.path, os.getuid()))
        self.path.touch(); self.path.write_text('arbitrary command\n')
        self.assertFalse(module.lease_alive(self.path, os.getuid()))
        self.path.unlink()
        self.assertFalse(module.lease_alive(self.path, os.getuid()))

    @unittest.skipUnless(sys.platform == 'linux', 'Linux /proc identity')
    def test_parent_identity_includes_owner_and_start_time(self):
        owner, started = module.process_identity(os.getpid())
        self.assertEqual(owner, os.getuid()); self.assertTrue(started.isdecimal())

    def test_uninstalled_privileged_entry_refuses_to_run(self):
        args = ['run_root', '--uid', str(os.getuid()), '--parent', str(os.getpid()), '--map', str(self.path), '--lease', str(self.path)]
        with mock.patch.object(sys, 'argv', args), mock.patch.object(os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(SystemExit, 'installed'):
                module.main()


if __name__ == '__main__': unittest.main(verbosity=2)
