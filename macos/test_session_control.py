import os
from pathlib import Path
import tempfile
import unittest
from supervisor import stop_requested


class SessionControlTests(unittest.TestCase):
    def test_only_run_keeps_the_session_active(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"control"
            p.write_text("run\n")
            self.assertFalse(stop_requested(p,os.getuid()))
            p.write_text("stop\n")
            self.assertTrue(stop_requested(p,os.getuid()))
            p.write_text("unexpected command")
            self.assertTrue(stop_requested(p,os.getuid()))

    def test_missing_wrong_owner_and_symlink_stop_the_session(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"control"
            self.assertTrue(stop_requested(p,os.getuid()))
            p.write_text("run\n")
            self.assertTrue(stop_requested(p,os.getuid()+1))
            link=Path(d)/"link"
            link.symlink_to(p)
            self.assertTrue(stop_requested(link,os.getuid()))


if __name__=="__main__":
    unittest.main()
