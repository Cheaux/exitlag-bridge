#!/usr/bin/env bash
# Run ExitLag on the PRODUCTION path: no research instrumentation at all.
#
#   ./tools/run-exitlag-production.sh
#
# Deliberately sets none of EXITLAG_NFAPI_SUCCEED / EXITLAG_NFAPI_EXTVER /
# EXITLAG_NFAPI_INJECT / EXITLAG_NFAPI_TRACE. The shim answers from its built-in supported-export
# table and its built-in extensions version, so a production run cannot depend
# on a hand-maintained debug list.
#
# The research launcher (tools/run-exitlag-gameprefix.sh) is unchanged and still
# available for experiments that need to narrow or widen the surface.
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Honour the managed-install locations; tools/exitlag-setup.sh and
# tools/exitlag-doctor.sh use the same variables, and hardcoding these
# meant a managed install silently launched the repo prefix instead.
prefix="${EXITLAG_PREFIX:-$repo_root/runtime/prefix-game}"
app_dir="${EXITLAG_APP_DIR:-$repo_root/runtime/app-shim}"
local_wine="$repo_root/wine-wbemprox/runtime-staging/x86_64-unix/wine"
stamp="$(date +%Y%m%dT%H%M%S)"
session_dir="$repo_root/runtime/logs/nfextlag/prodsession-$stamp"
mkdir -p "$session_dir"

# ExitLag auto-updates, and the shim reproduces a private ABI. Known build: run.
# Unknown build: do not guess -- re-derive the ABI this build actually needs. If
# it still matches, say so loudly and continue; if it does not, stop, because
# the failure mode is a crash or silently unsteered traffic.
if ! "$repo_root/tools/check-exitlag-version.sh" >/dev/null 2>&1; then
    echo "ExitLag build is not the recorded known-good one; verifying the ABI..." >&2
    if "$repo_root/tools/check-exitlag-abi.sh"; then
        echo >&2
        echo "WARNING: running against an unrecorded ExitLag build." >&2
        echo "  The ABI checks out, so this is expected to work, but it has not been" >&2
        echo "  tested end to end. If it behaves, record it as known-good with:" >&2
        echo "    tools/check-exitlag-version.sh --record" >&2
        echo >&2
    else
        echo >&2
        echo "REFUSING to start: this ExitLag build is not ABI-compatible with the shim." >&2
        echo "  See the report above. docs/exitlag-updates.md explains what to do." >&2
        exit 4
    fi
fi

if [[ -z "${KEEP_PREFIX:-}" ]]; then
    env WINEPREFIX="$prefix" /usr/bin/wineserver -k 2>/dev/null || true
    sleep 1
fi

# EXITLAG_NFAPI_LOG is kept: it is the runtime's own log, not a behaviour hook.
run_env=(
    "WINEPREFIX=$prefix" "WINELOADER=$local_wine"
    "WINEDEBUG=${WINEDEBUG:--all}"
    "QSG_RHI_BACKEND=opengl"
    "EXITLAG_NFAPI_LOG=Z:${session_dir//\//\\}\\nfapi.log"
    "EXITLAG_NETD=${EXITLAG_NETD:-127.0.0.1:51900}"
    # The backend publishes a per-session token readable only by this user; the
    # shim must present it. Z: is Wine's view of the Linux root.
    "EXITLAG_NETD_TOKEN=${EXITLAG_NETD_TOKEN:-Z:\\run\\exitlag-linux\\ipc.token}"
)
{ printf 'prefix=%s\nmode=production (no succeed list, no extver, no inject)\n' "$prefix"
  printf 'environment='; printf '%q ' "${run_env[@]}"; printf '\n'; } >"$session_dir/metadata.txt"
echo "session: $session_dir"
cd "$app_dir"
exec env -u EXITLAG_NFAPI_SUCCEED -u EXITLAG_NFAPI_EXTVER -u EXITLAG_NFAPI_INJECT \
     -u EXITLAG_NFAPI_TRACE \
     "${run_env[@]}" "$local_wine" ./ExitLag.exe >"$session_dir/wine.log" 2>&1
