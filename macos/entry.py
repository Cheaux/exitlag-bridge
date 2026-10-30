"""Isolated Python entry point. The installed root copy selects only fixed code."""
import os
from pathlib import Path
import runpy
import sys

here = Path(__file__).resolve().parent
sys.path.insert(0, str(here))
if os.geteuid() == 0:
    if len(sys.argv) != 3 or sys.argv[1] != '--uid' or not sys.argv[2].isdecimal():
        raise SystemExit('The installed service requires its configured --uid')
    sys.argv = [str(here / 'auto_service.py'), '--uid', sys.argv[2]]
    runpy.run_path(sys.argv[0], run_name='__main__')
else:
    if len(sys.argv) > 1 and sys.argv[1] == '--setup':
        sys.argv.pop(1)
        sys.argv[0] = str(here / 'setup_worker.py')
    else:
        sys.argv[0] = str(here / 'auto_agent.py')
    runpy.run_path(sys.argv[0], run_name='__main__')
