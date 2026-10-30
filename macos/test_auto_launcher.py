"""Automatic launcher boundaries and actual loopback companion reloading."""
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time
import unittest
from unittest import mock
import auto_common as c
import auto_service as service
import test_bridge
from test_wine_socket import AdapterServer
from wine_socket import ReloadingWineSocketAdapter
from bridge import load_identities

class Records(unittest.TestCase):
    def test_private_control_is_bounded_and_epoch_scoped(self):
        epoch = secrets.token_hex(16)
        value = dict(version=1, epoch=epoch, run=True)
        self.assertTrue(c.valid_request(value, epoch))
        self.assertFalse(c.valid_request(value, 'another epoch'))
        self.assertFalse(c.valid_request(dict(value, command='arbitrary'), epoch))
        self.assertFalse(c.valid_request(dict(value, run=1), epoch))
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'control'
            c.publish(path, os.getuid(), dict(value, run=False))
            c.write_control(path, os.getuid(), value)
            self.assertEqual(c.read_private(path, os.getuid(), 512)[0], value)
            with self.assertRaises(ValueError):
                c.write_control(path, os.getuid(), dict(payload='x'*600))

    def test_links_and_public_records_are_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'status'
            c.publish(path, os.getuid(), {'value': 1})
            link = Path(folder) / 'link'
            link.symlink_to(path)
            with self.assertRaises(OSError):
                c.read_private(link, os.getuid())
            link.unlink()
            os.link(path, link)
            with self.assertRaises(ValueError):
                c.read_private(path, os.getuid())
            link.unlink()
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                c.read_private(path, os.getuid())

class Lifecycle(unittest.TestCase):
    def test_standard_game_paths_may_be_installed_after_the_bridge(self):
        with tempfile.TemporaryDirectory() as folder:
            mapping = Path(folder) / 'games.map'
            missing = Path(folder) / 'not-yet-installed' / 'WowClassic.exe'
            mapping.write_text(str(missing) + '\t' + r'C:\Games\WowClassic.exe' + '\n')
            self.assertEqual(load_identities(mapping), {str(missing.resolve()): r'C:\Games\WowClassic.exe'})

    def test_installation_user_is_derived_and_system_accounts_are_refused(self):
        service.configure_user(os.getuid())
        self.assertEqual(service.UID, os.getuid())
        self.assertEqual(service.USER_HOME, Path.home())
        self.assertEqual(service.RUNTIME.name, f'cheaux-exitlag-{os.getuid()}')
        for uid in (0, 499, -1, True, 2**31):
            with self.subTest(uid=uid), self.assertRaises(ValueError):
                service.configure_user(uid)

    def test_pf_changes_are_limited_to_the_fixed_anchor_and_scope(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(service, 'RUNTIME', Path(folder)):
            session = service.Session()
            result = mock.Mock(stdout='Status: Enabled', stderr='')
            with mock.patch.object(service, 'command', return_value=result) as command, mock.patch.object(service, 'release_lease') as release:
                session.enable()
                rules = session.rules.read_text()
                self.assertIn(f'user {os.getuid()}', rules)
                self.assertIn('64.224.24.0/21 port 3724', rules)
                self.assertIn('24.105.0.0/18 port 3724', rules)
                self.assertNotIn('0.0.0.0/0', rules)
                session.disable()
                calls = [call.args for call in command.call_args_list]
                self.assertIn(('/sbin/pfctl', '-a', service.ANCHOR, '-F', 'all'), calls)
                self.assertFalse(any(args[:2] == ('/sbin/pfctl', '-f') for args in calls))
                release.assert_called_once()

    def test_both_game_configurations_are_kept_separate(self):
        identities = {'/games/_classic_era_/Game': r'C:\Game\WowClassic.exe', '/games/_forever_/Game': r'C:\Game\Wow.exe'}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(service, 'RUNTIME', Path(folder)), mock.patch.object(service, 'load_identities', return_value=identities), mock.patch.object(service, 'Bridge') as bridge, mock.patch.object(service, 'write_token'), mock.patch.object(service.os, 'chown'):
            session = service.Session()
            session.start()
            adapter = bridge.call_args.kwargs['socket_adapter']
            self.assertEqual(adapter.path[identities['/games/_classic_era_/Game']][1], 51902)
            self.assertEqual(adapter.path[identities['/games/_forever_/Game']][1], 51903)
            self.assertNotEqual(*[str(path[0]) for path in adapter.path.values()])

class ReloadingFlows(unittest.TestCase):
    setUp = test_bridge.BridgeTests.setUp
    tearDown = test_bridge.BridgeTests.tearDown
    exchange = test_bridge.BridgeTests.exchange

    def test_new_companion_is_loaded_after_game_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'companion.json'
            self.service.socket_adapter = ReloadingWineSocketAdapter(config, os.getuid(), [self.identity], wait=.5)
            self.shim = test_bridge.Shim(self.service, self.routed.endpoint)
            for pid in (4321, 5678):
                token = secrets.token_bytes(32)
                server = AdapterServer(pid, token, 51902)
                try:
                    c.atomic_private(config, dict(version=1, pid=pid, port=51902, identity=self.identity, token=token.hex()))
                    self.assertEqual(self.exchange(), b'exitlag:hello')
                finally:
                    server.close()
                time.sleep(.3)
            self.assertEqual(self.service.snapshot()['redirected'], 2)

    def test_forever_uses_its_own_port_and_identity(self):
        forever = r'C:\Program Files (x86)\World of Warcraft\_forever_\Wow.exe'
        self.identity = forever
        self.service.owner_lookup = lambda endpoint: (9876, forever)
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'forever.json'
            token = secrets.token_bytes(32)
            server = AdapterServer(9999, token, 51903)
            try:
                c.atomic_private(config, dict(version=1, pid=9999, port=51903, identity=forever, token=token.hex()))
                self.service.socket_adapter = ReloadingWineSocketAdapter({forever: (config, 51903)}, os.getuid(), [forever])
                self.shim = test_bridge.Shim(self.service, self.routed.endpoint, nested=True)
                self.assertEqual(self.exchange(), b'exitlag:hello')
                self.assertEqual(self.shim.resolved, [forever, ''])
            finally:
                server.close()

    def test_missing_companion_falls_back_to_the_original_destination(self):
        self.service.socket_adapter = ReloadingWineSocketAdapter(Path('/nonexistent/config'), os.getuid(), [self.identity], wait=.1)
        self.shim = test_bridge.Shim(self.service, self.routed.endpoint)
        self.assertEqual(self.exchange(), b'origin:hello')
        self.assertEqual(self.service.snapshot()['adapter_errors'], 1)
        self.assertEqual(self.routed.peers, [])

    def test_first_flow_can_wait_for_companion_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'companion.json'
            token = secrets.token_bytes(32)
            server = AdapterServer(4321, token, 51902)
            try:
                def later():
                    time.sleep(.15)
                    c.atomic_private(config, dict(version=1, pid=4321, port=51902, identity=self.identity, token=token.hex()))
                thread = threading.Thread(target=later)
                thread.start()
                self.service.socket_adapter = ReloadingWineSocketAdapter(config, os.getuid(), [self.identity], wait=.8)
                self.shim = test_bridge.Shim(self.service, self.routed.endpoint)
                self.assertEqual(self.exchange(), b'exitlag:hello')
                thread.join()
            finally:
                server.close()

if __name__ == '__main__':
    unittest.main(verbosity=2)
