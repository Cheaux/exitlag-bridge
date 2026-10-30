#!/usr/bin/env bash
# The managed CLI now shares the cross-platform lifecycle implementation.
set -euo pipefail
repo="$(cd -- "$(dirname -- "$0")/.." && pwd)"
exec python3 "$repo/tests/test_installer.py"
