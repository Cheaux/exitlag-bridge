"""Automatic launcher boundaries and actual loopback companion reloading."""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'installers'))
import auto_common as c
import auto_agent as agent
import auto_service as service
import test_bridge
from test_wine_socket import AdapterServer
from wine_socket import ReloadingWineSocketAdapter
from bridge import load_identities

class Records(unittest.TestCase):
    def test_launcher_read_lock_does_not_stop_status_publication(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(agent, 'STATE', Path(folder)):
            path = Path(folder) / 'launcher-status.json'
            previous = {'stage': 'ready', 'updated_at': 1}
            updated = {'stage': 'ready', 'updated_at': 2}
            c.publish(path, os.getuid(), previous)
            with path.open('rb') as reader:
                fcntl.flock(reader.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                agent.publish_status(updated)
                self.assertEqual(c.read_private(path, os.getuid())[0], previous)
            agent.publish_status(updated)
            self.assertEqual(c.read_private(path, os.getuid())[0], updated)

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
    def test_launcher_reopens_a_crashed_client_without_restarting_the_lease(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            report = state / 'prefix/drive_c/users/example/AppData/Local/ExitLag/crashpad/reports/new.dmp'
            report.parent.mkdir(parents=True)
            executable = state / 'prefix/drive_c/ExitLag/ExitLag.exe'
            now = [0]
            startup = agent.ClientStartup(state, clock=lambda: now[0])
            scans = [0]
            def scan(_):
                scans[0] += 1
                if scans[0] == 1:
                    return [1234]
                if scans[0] == 2:
                    report.write_bytes(b'MDMP' + bytes(28))
                    return []
                agent.STOPPING = True
                return [5678]
            def advance(_):
                now[0] += 4
            service_status = dict(stage='waiting_client', message='Waiting', updated_at=time.time())
            with mock.patch.object(agent, 'STATE', state), \
                    mock.patch.object(agent, 'STOPPING', False), \
                    mock.patch.object(sys, 'argv', ['auto_agent', '--parent', '9999']), \
                    mock.patch.object(agent, 'alive', return_value=True), \
                    mock.patch.object(agent.signal, 'signal'), \
                    mock.patch.object(agent, 'load_identities', return_value={}), \
                    mock.patch.object(agent, 'NativeIdentity'), \
                    mock.patch.object(agent, 'native_pids', return_value={}), \
                    mock.patch.object(agent, 'legacy_active', return_value=False), \
                    mock.patch.object(agent, 'read_private', return_value=(service_status, None)), \
                    mock.patch.object(agent, 'Lease') as lease, \
                    mock.patch.object(agent, 'ClientStartup', return_value=startup), \
                    mock.patch.object(agent, 'client_pids', side_effect=scan), \
                    mock.patch.object(agent, 'stop_clients'), \
                    mock.patch.object(agent, 'start_client') as launch, \
                    mock.patch.object(agent.time, 'sleep', side_effect=advance), \
                    mock.patch('bridge_setup.core.find_client', return_value=executable):
                agent.main()
            self.assertEqual(launch.call_count, 2)
            lease.assert_called_once_with(9999)
            lease.return_value.close.assert_called_once()
            self.assertEqual(json.loads((state / 'launcher-status.json').read_text())['client_pid'], 5678)

    def test_confirmed_startup_crashes_retry_with_a_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            reports = state / 'prefix/drive_c/users/example/AppData/Local/ExitLag/crashpad/reports'
            reports.mkdir(parents=True)
            now = [0]
            startup = agent.ClientStartup(state, clock=lambda: now[0])
            for attempt in range(3):
                startup.launched()
                self.assertEqual(startup.observe([1234]), 'running')
                (reports / ('crash-%d.dmp' % attempt)).write_bytes(b'MDMP' + bytes(28))
                now[0] += 1
                self.assertEqual(startup.observe([]), 'starting')
                now[0] += 3
                self.assertEqual(startup.observe([]), 'retry' if attempt < 2 else 'failed')
            self.assertEqual(startup.retries, 2)

    def test_normal_quit_with_old_reports_does_not_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            report = state / 'prefix/drive_c/users/example/AppData/Local/ExitLag/crashpad/reports/old.dmp'
            report.parent.mkdir(parents=True)
            report.write_bytes(b'MDMP' + bytes(28))
            now = [0]
            startup = agent.ClientStartup(state, clock=lambda: now[0])
            startup.launched()
            startup.observe([1234])
            now[0] = 4
            self.assertEqual(startup.observe([]), 'closed')
            self.assertEqual(startup.retries, 0)

    def test_missing_dashboard_is_a_startup_failure_instead_of_a_quit(self):
        with tempfile.TemporaryDirectory() as folder:
            now = [0]
            startup = agent.ClientStartup(Path(folder), clock=lambda: now[0])
            startup.launched()
            now[0] = 19
            self.assertEqual(startup.observe([]), 'starting')
            now[0] = 21
            self.assertEqual(startup.observe([]), 'failed')

    def test_crash_recovery_is_limited_to_initial_startup(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            now = [0]
            startup = agent.ClientStartup(state, clock=lambda: now[0])
            startup.launched()
            startup.observe([1234])
            report = state / 'prefix/drive_c/users/example/AppData/Local/ExitLag/crashpad/reports/late.dmp'
            report.parent.mkdir(parents=True)
            report.write_bytes(b'MDMP' + bytes(28))
            now[0] = 121
            self.assertEqual(startup.observe([]), 'failed')

    def test_crash_report_links_and_non_dump_files_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            reports = state / 'prefix/drive_c/users/example/AppData/Local/ExitLag/crashpad/reports'
            reports.mkdir(parents=True)
            outside = state / 'unrelated'
            outside.write_bytes(b'MDMP' + bytes(28))
            (reports / 'linked.dmp').symlink_to(outside)
            (reports / 'text.dmp').write_bytes(b'not a crash report' + bytes(32))
            self.assertEqual(agent.client_crash_reports(state), set())

    def test_runtime_initialization_finishes_before_dashboard_launch(self):
        executable = Path('/private/test/prefix/drive_c/ExitLag/ExitLag.exe')
        order = []
        with mock.patch('client_app.prepare_client_app', return_value=Path('/private/test/ExitLag.app')), \
                mock.patch('window_driver.install_window_driver', return_value='current'), \
                mock.patch.object(agent.subprocess, 'run', side_effect=lambda *args, **kwargs: order.append(('boot', args, kwargs))), \
                mock.patch.object(agent.subprocess, 'Popen', side_effect=lambda *args, **kwargs: order.append(('dashboard', args, kwargs))):
            agent.start_client(executable, None)
        self.assertEqual([item[0] for item in order], ['boot', 'dashboard'])
        self.assertEqual(order[0][1][0][1:], ['cmd.exe', '/d', '/c', 'exit', '0'])
        self.assertEqual(order[0][2]['env']['WINEPREFIX'], str(agent.STATE / 'prefix'))
        self.assertEqual(order[0][2]['env']['EXITLAG_NETD'], '127.0.0.1:51900')
        self.assertEqual(order[0][2]['env']['EXITLAG_NETD_TOKEN'],
                         'Z:/var/run/cheaux-exitlag-%s/session.token' % os.getuid())
        self.assertTrue(order[0][2]['check'])

    def test_failed_runtime_initialization_does_not_open_a_broken_login(self):
        with mock.patch('client_app.prepare_client_app', return_value=Path('/private/test/ExitLag.app')), \
                mock.patch('window_driver.install_window_driver', return_value='current'), \
                mock.patch.object(agent.subprocess, 'run', side_effect=agent.subprocess.TimeoutExpired('cmd.exe', 45)), \
                mock.patch.object(agent.subprocess, 'Popen') as launch:
            with self.assertRaises(agent.subprocess.TimeoutExpired):
                agent.start_client(Path('/private/test/ExitLag.exe'), None)
            launch.assert_not_called()

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

    @contextlib.contextmanager
    def companion(self, pid, token, port=51902):
        # Keep the real configuration-port checks, but send test connections
        # to an ephemeral listener so a running game companion is untouched.
        server = AdapterServer(pid, token)
        connect = socket.create_connection
        def isolated(address, *args, **kwargs):
            if address == ('127.0.0.1', port):
                address = ('127.0.0.1', server.port)
            return connect(address, *args, **kwargs)
        try:
            with mock.patch('wine_socket.socket.create_connection', side_effect=isolated):
                yield server
        finally:
            server.close()

    def test_new_companion_is_loaded_after_game_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'companion.json'
            self.service.socket_adapter = ReloadingWineSocketAdapter(config, os.getuid(), [self.identity], wait=.5)
            self.shim = test_bridge.Shim(self.service, self.routed.endpoint)
            for pid in (4321, 5678):
                token = secrets.token_bytes(32)
                with self.companion(pid, token):
                    c.atomic_private(config, dict(version=1, pid=pid, port=51902, identity=self.identity, token=token.hex()))
                    self.assertEqual(self.exchange(), b'exitlag:hello')
                time.sleep(.3)
            self.assertEqual(self.service.snapshot()['redirected'], 2)

    def test_forever_uses_its_own_port_and_identity(self):
        forever = r'C:\Program Files (x86)\World of Warcraft\_forever_\Wow.exe'
        self.identity = forever
        self.service.owner_lookup = lambda endpoint: (9876, forever)
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'forever.json'
            token = secrets.token_bytes(32)
            with self.companion(9999, token, 51903):
                c.atomic_private(config, dict(version=1, pid=9999, port=51903, identity=forever, token=token.hex()))
                self.service.socket_adapter = ReloadingWineSocketAdapter({forever: (config, 51903)}, os.getuid(), [forever])
                self.shim = test_bridge.Shim(self.service, self.routed.endpoint, nested=True)
                self.assertEqual(self.exchange(), b'exitlag:hello')
                self.assertEqual(self.shim.resolved, [forever, ''])

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
            with self.companion(4321, token):
                def later():
                    time.sleep(.15)
                    c.atomic_private(config, dict(version=1, pid=4321, port=51902, identity=self.identity, token=token.hex()))
                thread = threading.Thread(target=later)
                thread.start()
                self.service.socket_adapter = ReloadingWineSocketAdapter(config, os.getuid(), [self.identity], wait=.8)
                self.shim = test_bridge.Shim(self.service, self.routed.endpoint)
                self.assertEqual(self.exchange(), b'exitlag:hello')
                thread.join()

if __name__ == '__main__':
    unittest.main(verbosity=2)
