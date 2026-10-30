#!/usr/bin/env python3
"""Exercise the installed Linux first-run UI with no vendor or account."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import time
from unittest import mock

root = Path(sys.argv[1]).resolve(strict=True)
spec = importlib.util.spec_from_file_location('desktop_app', root / 'installers/linux/app.py')
app = importlib.util.module_from_spec(spec); spec.loader.exec_module(app)
with tempfile.TemporaryDirectory(prefix='desktop setup ') as temporary:
    state = Path(temporary) / 'state'
    invalid = Path(temporary) / 'invalid.exe'; invalid.write_text('not an executable')
    with mock.patch.object(app, 'default_state', return_value=state):
        desktop = app.Desktop()
        desktop.window.update()
        assert not state.exists(), 'Opening first-run setup should not create a prefix'
        with mock.patch.object(app.filedialog, 'askopenfilename', return_value=str(invalid)):
            desktop.install()
        deadline = time.monotonic() + 8
        while desktop.busy and time.monotonic() < deadline:
            desktop.window.update()
            time.sleep(.05)
        assert not desktop.busy
        assert desktop.message.get().startswith('Setup stopped:'), desktop.message.get()
        assert not (state / 'runtime').exists()
        assert desktop.helper is None and desktop.client is None
        desktop.close()
print('Installed desktop first-run and invalid-installer rejection passed; no vendor, root routing or account used.')
