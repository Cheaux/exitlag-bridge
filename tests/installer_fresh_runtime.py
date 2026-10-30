#!/usr/bin/env python3
"""Actual pinned-Wine bootstrap in a disposable state, without the vendor client."""
import json
from pathlib import Path
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'installers'))
from bridge_setup.core import Setup

payloads = Path(sys.argv[1]).resolve()
with tempfile.TemporaryDirectory(prefix='bridge fresh runtime ') as temporary:
    state = Path(temporary) / 'state'
    setup = Setup(state, payloads, emit=lambda stage, message: print(stage + ': ' + message, flush=True))
    setup.run(prepare_only=True)
    first = setup.state.load()
    assert first['wmi_verified'] and not first.get('complete')
    setup.run(prepare_only=True)
    assert setup.state.load()['wmi_verified']
    setup.state.remove_components()
    assert (state / 'prefix/system.reg').exists()
    setup.state.purge()
    assert not state.exists()
    print(json.dumps({'fresh_runtime_verified': True, 'wmi_verified': True, 'idempotent_prepare': True,
                      'uninstall_preserved_prefix': True, 'purge_scoped': True,
                      'vendor_install_or_login_tested': False}))
