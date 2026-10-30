#!/usr/bin/env bash
# Reverse install-nfextlag-stub.sh: delete the service registration and the
# stub driver file. Only removes the file if it is the stub we installed.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
prefix="${WINEPREFIX:-$repo_root/runtime/prefix}"
local_wine="$repo_root/wine-wbemprox/runtime-staging/x86_64-unix/wine"
target="$prefix/drive_c/windows/system32/drivers/nfextlag.sys"
stub="$repo_root/shim/nfextlag/build/nfextlag.sys"

export WINEPREFIX="$prefix" WINELOADER="$local_wine" WINEDEBUG="${WINEDEBUG:--all}"

"$local_wine" sc stop nfextlag >/dev/null 2>&1 || true
"$local_wine" sc delete nfextlag || echo "service nfextlag was not registered"

if [[ -e "$target" ]]; then
    if [[ -f "$stub" && "$(sha256sum "$target" | cut -d' ' -f1)" == "$(sha256sum "$stub" | cut -d' ' -f1)" ]]; then
        rm -v "$target"
    else
        echo "not removing $target: it is not the built stub" >&2
        exit 1
    fi
fi

# Restart the prefix's service manager so a stale in-memory database is not kept.
/usr/bin/wineserver -k 2>/dev/null || true
echo "removed nfextlag stub surface"
