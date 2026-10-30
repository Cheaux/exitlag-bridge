#!/usr/bin/env bash
# Install the presence-only nfextlag service/driver surface into the dedicated
# Wine prefix. Everything is prefix-local and reversed by remove-nfextlag-stub.sh.
#
#   START_MODE=demand (default) : ExitLag itself must StartServiceA it (mirrors
#                                 ensureNfsdkServiceRunning at 0x1412927f0).
#   START_MODE=auto             : services.exe starts it at prefix boot.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
prefix="${WINEPREFIX:-$repo_root/runtime/prefix}"
local_wine="$repo_root/wine-wbemprox/runtime-staging/x86_64-unix/wine"
stub="$repo_root/shim/nfextlag/build/nfextlag.sys"
target_dir="$prefix/drive_c/windows/system32/drivers"
target="$target_dir/nfextlag.sys"
start_mode="${START_MODE:-demand}"
manifest="$repo_root/runtime/logs/nfextlag/install-manifest.txt"

test -x "$local_wine"
test -f "$stub" || "$repo_root/shim/nfextlag/build.sh"
test -d "$target_dir"

if [[ -e "$target" ]]; then
    echo "refusing to overwrite existing $target" >&2
    echo "(remove it with tools/remove-nfextlag-stub.sh first)" >&2
    exit 1
fi

export WINEPREFIX="$prefix" WINELOADER="$local_wine" WINEDEBUG="${WINEDEBUG:--all}"

install -m 0644 "$stub" "$target"
"$local_wine" sc create nfextlag type= kernel start= "$start_mode" \
    binPath= 'C:\windows\system32\drivers\nfextlag.sys' \
    DisplayName= 'nfextlag (exitlag-linux presence stub)'

mkdir -p "$(dirname "$manifest")"
{
    printf 'installed_utc=%s\n' "$(date --utc +%Y-%m-%dT%H:%M:%SZ)"
    printf 'prefix=%s\n' "$prefix"
    printf 'driver=%s\n' "$target"
    printf 'driver_sha256=%s\n' "$(sha256sum "$target" | cut -d' ' -f1)"
    printf 'service=nfextlag type=kernel start=%s\n' "$start_mode"
} >"$manifest"

"$local_wine" sc query nfextlag || true
cat "$manifest"
