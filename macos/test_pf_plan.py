import subprocess
import tempfile
from pathlib import Path
import unittest
from pf_plan import make_rules

class PFPlanTests(unittest.TestCase):
    def test_parser_accepts_generated_anchor_without_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"anchor"
            path.write_text(make_rules(501,51901,["203.0.113.10:3724"]))
            result=subprocess.run(["/sbin/pfctl","-vnf",str(path)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
    def test_rejects_broad_or_loopback_targets(self):
        for target in ("0.0.0.0/0:80","127.0.0.1:51900","224.0.0.0/8:100","203.0.113.1:0"):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    make_rules(501,51901,[target])
    def test_requires_specific_user_and_destination(self):
        for uid,targets in ((0,["203.0.113.1:80"]),(501,[])):
            with self.assertRaises(ValueError):
                make_rules(uid,51901,targets)

if __name__=="__main__":
    unittest.main(verbosity=2)
