#!/usr/bin/env python3
"""Installer security/lifecycle tests; no account, Wine or privileges needed."""
import json
import os
from pathlib import Path
import struct
import sys
import tarfile
import tempfile
import time
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'installers'))
from bridge_setup import core, util


def pe(path):
    data = bytearray(2048); data[:2] = b'MZ'
    struct.pack_into('<I', data, 60, 128); data[128:132] = b'PE\0\0'
    struct.pack_into('<H', data, 132, 0x8664); path.write_bytes(data)


class Ownership(unittest.TestCase):
    def test_unmarked_data_is_preserved(self):
        with tempfile.TemporaryDirectory() as t:
            state = Path(t)/'state'; state.mkdir()
            note = state/'unrelated'; note.write_text('keep')
            with self.assertRaises(ValueError): util.State(state, create=True)
            self.assertEqual(note.read_text(), 'keep')

    def test_incomplete_prefix_is_backed_up(self):
        with tempfile.TemporaryDirectory() as t:
            state = util.State(Path(t)/'state', create=True)
            prefix = state.relative('prefix'); prefix.mkdir()
            (prefix/'private-file').write_text('keep'); state.quarantine_incomplete_prefix()
            self.assertFalse(prefix.exists())
            self.assertEqual(next((state.path/'backups').glob('*/private-file')).read_text(), 'keep')

    def test_symlinks_and_escape_are_refused(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t); other=base/'other'; other.mkdir()
            link=base/'link'; link.symlink_to(other)
            with self.assertRaises(ValueError): util.State(link, create=True)
            state=util.State(base/'state', create=True)
            for name in ('../other','/','/tmp/anything'):
                with self.assertRaises(ValueError): state.relative(name)
            (state.path/'runtime').symlink_to(other)
            with self.assertRaises(ValueError): state.remove_components()
            self.assertTrue(other.exists())

    def test_remove_preserves_accounts_and_purge_is_scoped(self):
        with tempfile.TemporaryDirectory() as t:
            base=Path(t); state=util.State(base/'state', create=True)
            for name in ('runtime','prefix','cache'): state.relative(name).mkdir()
            state.relative('prefix/account').write_text('keep')
            unrelated=base/'unrelated'; unrelated.write_text('keep')
            state.remove_components()
            self.assertEqual(state.relative('prefix/account').read_text(),'keep')
            self.assertFalse(state.relative('runtime').exists())
            state.purge(); self.assertFalse(state.path.exists())
            self.assertEqual(unrelated.read_text(),'keep')


class Archives(unittest.TestCase):
    def test_traversal_links_and_devices_are_refused(self):
        cases=[('../escape',None,tarfile.REGTYPE),('link','/etc/passwd',tarfile.SYMTYPE),
               ('link','../../outside',tarfile.LNKTYPE),('device',None,tarfile.CHRTYPE)]
        for name,link,kind in cases:
            with self.subTest(name=name,kind=kind), tempfile.TemporaryDirectory() as t:
                archive=Path(t)/'bad.tar'
                with tarfile.open(archive,'w') as out:
                    entry=tarfile.TarInfo(name); entry.type=kind
                    if link: entry.linkname=link
                    out.addfile(entry)
                with self.assertRaises(ValueError): util.extract(archive,Path(t)/'out')

    def test_wrong_download_bytes_are_not_used(self):
        with tempfile.TemporaryDirectory() as t:
            cache=Path(t)/'cache'
            def wrong(args,**kwargs): Path(args[args.index('--output')+1]).write_bytes(b'wrong')
            with mock.patch.object(util.subprocess,'run',side_effect=wrong):
                with self.assertRaises(ValueError): util.download({'url':'https://example.com/file','sha256':'0'*64},cache)
            self.assertEqual(list(cache.iterdir()),[])

    def test_plaintext_http_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ValueError): util.download({'url':'http://example.com/file','sha256':'0'*64},Path(t)/'cache')


class Lifecycle(unittest.TestCase):
    def setUp(self):
        patcher=mock.patch.object(core.platform,'machine',return_value='x86_64')
        patcher.start(); self.addCleanup(patcher.stop)

    def fixture(self,t):
        base=Path(t); payloads=base/'payloads'; payloads.mkdir()
        for name in ('nfapi.dll','nfextlag.sys','wbemprox.dll'): (payloads/name).write_bytes(('bridge-'+name).encode())
        (payloads/'manifest.json').write_text(json.dumps({'files':{p.name:util.digest(p) for p in payloads.iterdir()}}))
        runtime=base/'runtime-source'; (runtime/'bin').mkdir(parents=True)
        (runtime/'bin/wine').write_text('#!/bin/sh\n'); (runtime/'bin/wine').chmod(0o755)
        modules=runtime/'lib/wine/x86_64-windows'; modules.mkdir(parents=True)
        (modules/'wbemprox.dll').write_bytes(b'vendor-wmi')
        archive=base/'wine.tar'
        with tarfile.open(archive,'w') as out: out.add(runtime,arcname='wine-test')
        installer=base/'vendor.exe'; pe(installer)
        return base,payloads,archive,installer

    def test_fresh_install_repair_and_vendor_handoff(self):
        with tempfile.TemporaryDirectory(prefix='bridge setup ') as t:
            base,payloads,archive,installer=self.fixture(t); commands=[]
            def command(setup,args,**kwargs):
                args=[str(x) for x in args]; commands.append(args)
                if '--version' in args:return b'wine-11.17 (Staging)\n'
                if 'wineboot' in args:
                    (setup.prefix/'drive_c').mkdir(parents=True,exist_ok=True)
                    (setup.prefix/'system.reg').write_text('test')
                if 'wmic' in args:return b'GroupComponent PartComponent Win32_LogicalDisk'
                if str(installer.resolve()) in args:
                    folder=setup.prefix/'drive_c/ExitLag'; folder.mkdir()
                    pe(folder/'ExitLag.exe'); (folder/'nfapi.dll').write_bytes(b'vendor-dll')
                return b''
            with mock.patch.object(core,'download',return_value=archive), mock.patch.object(core.Setup,'command',command), mock.patch.object(core,'check_abi',return_value=True):
                setup=core.Setup(base/'state',payloads,host='linux'); client=setup.run(installer)
                self.assertTrue(setup.state.load()['complete'])
                self.assertEqual((client.parent/'nfapi.dll').read_bytes(),b'bridge-nfapi.dll')
                backup=setup.state.load()['vendor_nfapi_backup']
                self.assertEqual(setup.state.relative(backup).read_bytes(),b'vendor-dll')
                (setup.prefix/'saved-account').write_text('private fixture')
                core.main(['uninstall', '--state', str(setup.state.path)])
                self.assertEqual((client.parent/'nfapi.dll').read_bytes(), b'vendor-dll')
                self.assertTrue((setup.prefix/'saved-account').exists())
                self.assertFalse((setup.state.path/'runtime').exists())
                setup.run()
                self.assertEqual(sum(str(installer.resolve()) in args for args in commands),1)
                self.assertFalse(any(any('silent' in x.lower() or 'agree-to' in x.lower() for x in args) for args in commands))
                self.assertEqual(setup.state.relative(backup).read_bytes(),b'vendor-dll')

    def test_bad_installer_fails_before_network_and_wine(self):
        with tempfile.TemporaryDirectory() as t:
            base,payloads,archive,installer=self.fixture(t); installer.write_bytes(b'invalid')
            with mock.patch.object(core,'download') as network, mock.patch.object(core.Setup,'command') as wine:
                with self.assertRaises(ValueError):core.Setup(base/'state',payloads,host='linux').run(installer)
                network.assert_not_called(); wine.assert_not_called()

    def test_vendor_background_service_cannot_hold_setup_output_open(self):
        with tempfile.TemporaryDirectory() as t:
            base,payloads,archive,installer=self.fixture(t)
            setup=core.Setup(base/'state',payloads,host='linux')
            pidfile=base/'service.pid'
            code="import subprocess,sys; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); open(sys.argv[1],'w').write(str(p.pid)); print('wizard finished')"
            try:
                started=time.monotonic()
                output=setup.command([sys.executable,'-c',code,pidfile],timeout=3,capture=True)
                self.assertIn(b'wizard finished',output)
                self.assertLess(time.monotonic()-started,3)
            finally:
                if pidfile.exists():
                    try:os.kill(int(pidfile.read_text()),15)
                    except ProcessLookupError:pass

    def test_redirected_windows_console_encoding_is_normalized(self):
        text='GroupComponent PartComponent Win32_LogicalDisk'
        self.assertEqual(core.console_output(text.encode('utf-16')),text.encode())
        self.assertEqual(core.console_output(text.encode('utf-16le')),text.encode())
        self.assertEqual(core.console_output(b'wine-11.17\n'),b'wine-11.17\n')

    def test_missing_client_cannot_report_complete(self):
        with tempfile.TemporaryDirectory() as t:
            base,payloads,archive,installer=self.fixture(t); setup=core.Setup(base/'state',payloads,host='linux')
            with self.assertRaises(ValueError):setup.install_client()
            self.assertFalse(setup.state.load()['complete'])

    def test_modified_payload_is_refused(self):
        with tempfile.TemporaryDirectory() as t:
            base,payloads,archive,installer=self.fixture(t); (payloads/'nfapi.dll').write_bytes(b'tampered')
            with self.assertRaises(ValueError):core.Setup(base/'state',payloads,host='linux')

if __name__=='__main__':unittest.main(verbosity=2)
