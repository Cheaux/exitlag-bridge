#!/usr/bin/env bash
# Compatibility entry for the shared, verified per-user installer.
# SPDX-License-Identifier: GPL-3.0-only
set -euo pipefail
repo="$(cd -- "$(dirname -- "$0")/.." && pwd)"
action="${1:-status}"
[[ $# == 0 ]] || shift
args=("$action")
if [[ -n "${EXITLAG_STATE_DIR:-}" ]]; then args+=(--state "$EXITLAG_STATE_DIR"); fi
case "$action" in
  install)
    [[ -f "$repo/installers/payloads/manifest.json" ]] || { echo 'Build the verified payloads first: python3 installers/build_runtime.py payloads --output installers/payloads' >&2; exit 1; }
    args+=(--payloads "$repo/installers/payloads")
    if [[ $# -gt 0 ]]; then args+=(--installer "$1"); shift; fi
    ;;
  uninstall)
    if [[ "${1:-}" == --purge ]]; then
      read -r -p 'Delete this managed prefix, including saved login and settings? [y/N] ' answer
      [[ "$answer" == y || "$answer" == Y ]] || exit 0
      args+=(--purge --confirm-purge); shift
    fi
    ;;
  status) ;;
  *) echo 'Usage: tools/exitlag-setup.sh status|install [official-installer.exe]|uninstall [--purge]' >&2; exit 2 ;;
esac
[[ $# == 0 ]] || { echo 'Unexpected arguments.' >&2; exit 2; }
exec python3 "$repo/installers/setup.py" "${args[@]}"
