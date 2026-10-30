#!/usr/bin/env bash
# One-shot active TCP destination-rewrite run.
#
#   sudo env TEST_DEST=<ip> ENABLE_REDIRECT=1 backend/run-tcp-redirect.sh
#   sudo env TEST_DEST=any  ENABLE_REDIRECT=1 backend/run-tcp-redirect.sh [seconds]
#
# This is deliberately separate from run-passive.sh. It applies only complete
# IPv4 address+port mutations returned by ExitLag's live callback. Every error,
# timeout, missing shim, unchanged callback or unsupported reply accepts the
# original packet unchanged.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
duration="${1:-}"
queue="${QUEUE_NUM:-0}"
port="${EXL_PORT:-51900}"
log="${NETD_LOG:-$repo_root/runtime/logs/netd-active-$(date +%Y%m%dT%H%M%S).log}"
table_installed=0
source "$repo_root/backend/runtime-guard.sh"
mapfile -t identity_args < <(runtime_identity_args)

if [[ $EUID -ne 0 ]]; then echo "run with sudo (NFQUEUE and nftables need CAP_NET_ADMIN)" >&2; exit 1; fi
: "${TEST_DEST:?set TEST_DEST=<ip> to scope the rule, or TEST_DEST=any}"
if [[ "${ENABLE_REDIRECT:-}" != 1 ]]; then
    echo "refusing active mode without ENABLE_REDIRECT=1" >&2
    exit 2
fi

runtime_guard_acquire "$repo_root"

mkdir -p "$(dirname "$log")"
cleanup() {
    set +e
    [[ -n "${exitlag_timer_pid:-}" ]] && kill "$exitlag_timer_pid" 2>/dev/null
    [[ -n "${netd_pid:-}" ]] && kill "$netd_pid" 2>/dev/null
    [[ -n "${netd_pid:-}" ]] && wait "$netd_pid" 2>/dev/null
    if (( table_installed )); then
        "$repo_root/backend/nft-teardown.sh" >/dev/null 2>&1
    fi
    [[ -n "${netd_pid:-}" ]] && runtime_guard_clear_helper "$netd_pid"
    if (( table_installed )); then
        echo "teardown complete: $(nft list table inet exitlag >/dev/null 2>&1 && echo 'TABLE STILL PRESENT' || echo 'no exitlag table')"
    else
        echo "teardown complete: no table was installed by this run"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Bind the fail-open queue first; only expose traffic to it once the helper is
# alive. The empty DNAT map installed below has no effect by itself.
runtime_guard_spawn_helper "$repo_root/backend/exitlag-netd" \
    --queue "$queue" --port "$port" --apply-tcp "${identity_args[@]}" --log "$log"
netd_pid=$exitlag_spawned_pid
runtime_guard_record_helper "$netd_pid"
for _ in {1..50}; do
    grep -q "bound NFQUEUE $queue" "$log" 2>/dev/null && break
    kill -0 "$netd_pid" 2>/dev/null || { echo "exitlag-netd exited during startup; see $log" >&2; exit 3; }
    sleep 0.02
done
grep -q "bound NFQUEUE $queue" "$log" 2>/dev/null || { echo "exitlag-netd did not become ready; see $log" >&2; exit 3; }

QUEUE_NUM="$queue" TEST_DEST="$TEST_DEST" "$repo_root/backend/nft-setup.sh" >/dev/null
table_installed=1
echo "active TCP redirect ready (scope: $TEST_DEST, queue $queue)"
echo "exitlag-netd pid=$netd_pid  log=$log"
[[ -n "$duration" ]] && echo "running for ${duration}s; callback errors and unsupported mutations fail open" \
                     || echo "running until Ctrl-C; callback errors and unsupported mutations fail open"
runtime_guard_wait "$netd_pid" "$duration"
