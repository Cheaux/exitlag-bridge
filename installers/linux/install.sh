#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
set -euo pipefail
here="$(cd -- "$(dirname -- "$0")" && pwd)"
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || { echo 'Linux x86_64 is required.' >&2; exit 1; }
[[ $EUID == 0 ]] || { echo 'Run sudo bash ./install.sh to install the program. Setup and ExitLag run as your ordinary user.' >&2; exit 1; }
for command in python3 nft pkexec curl; do command -v "$command" >/dev/null || { echo "Install the missing dependency: $command" >&2; exit 1; }; done
python3 -c 'import sys, tkinter; assert sys.version_info >= (3,10)' || { echo 'Install Python 3.10+ and its Tk package.' >&2; exit 1; }
root=/usr/local/lib/exitlag-bridge
if pgrep -f '/usr/local/lib/exitlag-bridge/installers/linux/(app|run_root).py' >/dev/null; then
  echo 'Quit ExitLag Bridge before upgrading it.' >&2; exit 1
fi
if [[ -e "$root" ]]; then
  [[ ! -L "$root" && -f "$root/.exitlag-bridge-program" ]] || { echo 'An unrelated installation exists at the target path.' >&2; exit 1; }
fi
[[ -f "$here/program/.exitlag-bridge-program" ]] || { echo 'Installer payload is missing.' >&2; exit 1; }
if find "$here/program" -type l | read -r _; then echo 'Linked program payloads are not supported.' >&2; exit 1; fi
staging="$(mktemp -d /usr/local/lib/.exitlag-bridge.XXXXXXXX)"
trap 'rm -rf -- "$staging"' EXIT
cp -a -- "$here/program/." "$staging/"
chown -R root:root "$staging"
chmod -R go-w "$staging"
if [[ -e "$root.previous" ]]; then echo 'A previous upgrade backup exists. Review it before installing again.' >&2; exit 1; fi
if [[ -e "$root" ]]; then mv -- "$root" "$root.previous"; fi
mv -- "$staging" "$root"
install -m 644 "$here/exitlag-bridge.desktop" /usr/share/applications/exitlag-bridge.desktop
if [[ -d "$root.previous" ]]; then rm -rf -- "$root.previous"; fi
echo 'Installed. Open ExitLag Bridge from your applications menu as your ordinary user.'
echo 'Download the official ExitLag .exe yourself when setup opens.'
