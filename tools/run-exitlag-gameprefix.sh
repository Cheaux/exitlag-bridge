#!/usr/bin/env bash
# Run the shim copy of ExitLag against the GAME prefix (runtime/prefix-game),
# which is the clone that may have a real game installed. Identical settings to
# run-exitlag-gate-session.sh; the pristine runtime/prefix is left untouched.
#
#   ./tools/run-exitlag-gameprefix.sh [SUCCEED_LIST]
set -euo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
succeed="${1:-nf_init,nf_getExtensionsVersion,nf_addFlowCtl,nf_setFlowCtlQueueParams,nf_setRulesEx,nf_getProcessNameFromKernel}"
# Honour the managed-install locations; tools/exitlag-setup.sh and
# tools/exitlag-doctor.sh use the same variables, and hardcoding these
# meant a managed install silently launched the repo prefix instead.
prefix="${EXITLAG_PREFIX:-$repo_root/runtime/prefix-game}"
app_dir="${EXITLAG_APP_DIR:-$repo_root/runtime/app-shim}"
local_wine="$repo_root/wine-wbemprox/runtime-staging/x86_64-unix/wine"
stamp="$(date +%Y%m%dT%H%M%S)"
session_dir="$repo_root/runtime/logs/nfextlag/gamesession-$stamp"
mkdir -p "$session_dir"

# This kills every process in the prefix, including Battle.net/WoW. Set
# KEEP_PREFIX=1 when other applications are already running in it.
if [[ -z "${KEEP_PREFIX:-}" ]]; then
    env WINEPREFIX="$prefix" /usr/bin/wineserver -k 2>/dev/null || true
    sleep 1
fi

run_env=(
    "WINEPREFIX=$prefix" "WINELOADER=$local_wine"
    "WINEDEBUG=${GATE_WINEDEBUG:--all,+timestamp,+pid,+tid,+debugstr}"
    "QSG_RHI_BACKEND=opengl"
    "EXITLAG_NFAPI_LOG=Z:${session_dir//\//\\}\\nfapi.log"
    "EXITLAG_NFAPI_SUCCEED=$succeed"
    "EXITLAG_NFAPI_EXTVER=8"
    "EXITLAG_NFAPI_INJECT=Z:${session_dir//\//\\}\\inject.cmd"
    "EXITLAG_NFAPI_TRACE=1"
    "EXITLAG_NETD=${EXITLAG_NETD:-127.0.0.1:51900}"
    # The backend publishes a per-session token readable only by this user; the
    # shim must present it. Z: is Wine's view of the Linux root.
    "EXITLAG_NETD_TOKEN=${EXITLAG_NETD_TOKEN:-Z:\\run\\exitlag-linux\\ipc.token}"
)
{ printf 'prefix=%s\nsucceed=%s\n' "$prefix" "$succeed"
  printf 'environment='; printf '%q ' "${run_env[@]}"; printf '\n'; } >"$session_dir/metadata.txt"
echo "session: $session_dir"
cd "$app_dir"
exec env "${run_env[@]}" "$local_wine" ./ExitLag.exe >"$session_dir/wine.log" 2>&1
