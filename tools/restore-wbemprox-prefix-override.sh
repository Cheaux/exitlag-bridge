#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
prefix="${WINEPREFIX:-$repo_root/runtime/prefix}"
target="$prefix/drive_c/windows/system32/wbem/wbemprox.dll"
backup="$repo_root/runtime/prefix-overrides/wbemprox.dll.wine-original"

if [[ ! -f "$backup" ]]; then
    echo "missing saved prefix DLL: $backup" >&2
    exit 1
fi

cp -- "$backup" "$target"
cmp --silent -- "$backup" "$target"

echo "original prefix wbemprox restored"
sha256sum "$target"
