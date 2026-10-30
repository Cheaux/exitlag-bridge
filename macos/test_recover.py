import unittest
from recover import validate_state


class RecoveryTests(unittest.TestCase):
    def test_only_the_session_owners_exact_anchor_is_accepted(self):
        state=dict(pid=12345,uid=501,anchor="com.apple/cheaux.exitlag.501.12345")
        self.assertEqual(validate_state(state,501),state["anchor"])
        for changes in (dict(anchor="com.apple"),dict(anchor="com.apple/cheaux.exitlag.502.12345"),
                        dict(uid=502),dict(pid="12345"),dict(pid=1),dict(pid=True)):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    validate_state(dict(state,**changes),501)


if __name__=="__main__":
    unittest.main(verbosity=2)
