#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run sudo bash ./uninstall.sh after quitting ExitLag Bridge.' >&2; exit 1; }
root=/usr/local/lib/exitlag-bridge
[[ ! -L "$root" && -f "$root/.exitlag-bridge-program" ]] || { echo 'No managed portable installation found.' >&2; exit 1; }
if pgrep -f '/exitlag-bridge/installers/linux/(app|run_root).py' >/dev/null; then echo 'Quit ExitLag Bridge before removing it.' >&2; exit 1; fi
rm -rf -- "$root"
rm -f /usr/share/applications/exitlag-bridge.desktop
echo 'Program removed. Your private Wine prefix, settings, and account data were preserved.'
