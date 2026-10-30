#!/usr/bin/env python3
"""Release-source privacy regression cases; all identities and secrets are fake."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('audit_source', Path(__file__).resolve().parents[1] / 'tools/audit-source.py')
audit_source = importlib.util.module_from_spec(spec); spec.loader.exec_module(audit_source)


class SourceAudit(unittest.TestCase):
    def test_secret_values_are_not_echoed_in_findings(self):
        fake = ('ghp_' + 'A' * 40).encode()
        self.assertEqual(audit_source.inspect_file('source.py', fake), ['credential or private-key pattern'])

    def test_personal_paths_and_lan_addresses_are_flagged(self):
        private = '/Users/' + 'private-person/project/file'
        address = '.'.join(('192', '168', '50', '12'))
        self.assertIn('non-placeholder home-directory path', audit_source.inspect_file('docs/report.md', private.encode()))
        self.assertIn('private network address', audit_source.inspect_file('docs/report.md', address.encode()))
        self.assertEqual(audit_source.inspect_file('docs/example.md', b'/home/USER/example /Users/Shared 192.0.2.10 127.0.0.1'), [])

    def test_generated_binaries_and_runtime_files_are_flagged(self):
        self.assertIn('compiled binary in source tree', audit_source.inspect_file('accidental', b'\x7fELFdata'))
        self.assertIn('runtime, credential or generated-artifact filename', audit_source.inspect_file('runtime/session.token', b'fake'))

    def test_committed_snapshot_and_metadata_are_scanned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args):
                return subprocess.run(['git', '-C', str(root), *args], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            git('init', '-b', 'main')
            git('config', 'user.name', 'Example')
            git('config', 'user.email', '123+example@users.noreply.github.com')
            (root / 'README.md').write_text('Public example\n')
            git('add', '.'); git('commit', '-m', 'Initial source')
            self.assertTrue(audit_source.audit(root)['passed'])
            # The source commit must be audited even if the working copy hides it.
            (root / 'oops.log').write_text('private fixture')
            git('add', '.'); git('commit', '-m', 'Accidental log')
            (root / 'oops.log').unlink()
            result = audit_source.audit(root)
            self.assertFalse(result['passed'])
            self.assertTrue(any(issue.get('file') == 'oops.log' for issue in result['findings']))
            git('config', 'user.email', 'noreply@github.com')
            git('commit', '--allow-empty', '-m', 'Public GitHub metadata')
            self.assertFalse(any('email' in issue['finding'] for issue in audit_source.audit(root)['findings']))
            git('config', 'user.email', 'private-person@example.invalid')
            git('commit', '--allow-empty', '-m', 'Private metadata')
            self.assertTrue(any('email' in issue['finding'] for issue in audit_source.audit(root)['findings']))


if __name__ == '__main__': unittest.main(verbosity=2)
