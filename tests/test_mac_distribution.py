#!/usr/bin/env python3
"""Distribution safeguards without signing identities or Apple service access."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]

def module(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'macos/app'/f'{name}.py')
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result

signer=module('sign_payload');notary=module('notarize')


class Distribution(unittest.TestCase):
    def test_production_signing_refuses_ad_hoc_identity(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(signer.subprocess,'run') as run:
            with self.assertRaisesRegex(ValueError,'Developer ID'):
                signer.sign(Path(temporary),'-')
            run.assert_not_called()

    def test_signing_does_not_follow_external_links(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);payload=root/'payload';payload.mkdir()
            outside=root/'outside';outside.write_bytes(b'\xcf\xfa\xed\xfe' + b'fake')
            (payload/'external').symlink_to(outside)
            inside=payload/'inside';inside.write_bytes(b'\xcf\xfa\xed\xfe' + b'fake')
            self.assertEqual(signer.binaries(payload),[inside.resolve()])

    def test_notarization_rejection_cannot_staple_or_claim_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            package=Path(temporary)/'test.pkg';package.write_text('fixture')
            def command(args,**kwargs):
                return SimpleNamespace(stdout='{"status":"Invalid"}')
            with mock.patch.object(notary.subprocess,'run',side_effect=command) as run:
                with self.assertRaisesRegex(RuntimeError,'did not accept'):
                    notary.notarize(package,'example-profile')
                self.assertFalse(any('stapler' in call.args[0] for call in run.call_args_list))

    def test_accepted_notarization_must_staple_and_validate(self):
        with tempfile.TemporaryDirectory() as temporary:
            package=Path(temporary)/'test.pkg';package.write_text('fixture')
            with mock.patch.object(notary.subprocess,'run',return_value=SimpleNamespace(stdout='{"status":"Accepted"}')) as run:
                notary.notarize(package,'example-profile')
                args=[call.args[0] for call in run.call_args_list]
                self.assertEqual(args[-2][1:3],['stapler','staple'])
                self.assertEqual(args[-1][1:3],['stapler','validate'])


if __name__=='__main__':unittest.main(verbosity=2)
