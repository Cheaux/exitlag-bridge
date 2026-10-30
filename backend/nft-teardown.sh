#!/usr/bin/env bash
# Remove every rule this project installed. Safe to run repeatedly.
set -euo pipefail
if [[ $EUID -ne 0 ]]; then
    echo "must run as root; re-run with sudo" >&2
    exit 1
fi
if nft list table inet exitlag >/dev/null 2>&1; then
    nft delete table inet exitlag
    echo "removed table inet exitlag"
else
    echo "table inet exitlag not present; nothing to do"
fi
