#!/usr/bin/env bash
# One-shot passive observation run.
#
#   sudo TEST_DEST=<ip> backend/run-passive.sh [seconds]
#   sudo TEST_DEST=any  backend/run-passive.sh 60
#
# Installs the scoped `inet exitlag` table, runs exitlag-netd in observation-only
# mode, and ALWAYS removes the table again on exit (including Ctrl-C or crash).
# Packets are only ever ACCEPTed unchanged in this mode.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$repo_root/backend/runtime-guard.sh"
mapfile -t identity_args < <(runtime_identity_args)
duration="${1:-60}"
queue="${QUEUE_NUM:-0}"
port="${EXL_PORT:-51900}"
log="${NETD_LOG:-$repo_root/runtime/logs/netd-$(date +%Y%m%dT%H%M%S).log}"
table_installed=0

if [[ $EUID -ne 0 ]]; then echo "run with sudo (NFQUEUE needs CAP_NET_ADMIN)" >&2; exit 1; fi
: "${TEST_DEST:?set TEST_DEST=<ip> to scope the rule, or TEST_DEST=any}"

mkdir -p "$(dirname "$log")"
cleanup() {
    set +e
    if (( table_installed )); then
        "$repo_root/backend/nft-teardown.sh" >/dev/null 2>&1
    fi
    [[ -n "${netd_pid:-}" ]] && kill "$netd_pid" 2>/dev/null
    [[ -n "${netd_pid:-}" ]] && wait "$netd_pid" 2>/dev/null
    if (( table_installed )); then
        echo "teardown complete: $(nft list table inet exitlag >/dev/null 2>&1 && echo 'TABLE STILL PRESENT' || echo 'no exitlag table')"
    else
        echo "teardown complete: no table was installed by this run"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"$repo_root/backend/exitlag-netd" --queue "$queue" --port "$port" \
    "${identity_args[@]}" --log "$log" &
netd_pid=$!
for _ in {1..50}; do
    grep -q "bound NFQUEUE $queue" "$log" 2>/dev/null && break
    kill -0 "$netd_pid" 2>/dev/null || { echo "exitlag-netd exited during startup; see $log" >&2; exit 3; }
    sleep 0.02
done
grep -q "bound NFQUEUE $queue" "$log" 2>/dev/null || { echo "exitlag-netd did not become ready; see $log" >&2; exit 3; }

QUEUE_NUM="$queue" TEST_DEST="$TEST_DEST" "$repo_root/backend/nft-setup.sh" >/dev/null
table_installed=1
echo "rules installed (scope: $TEST_DEST, queue $queue)"
echo "exitlag-netd pid=$netd_pid  log=$log"
echo "observing for ${duration}s; every packet is ACCEPTed unchanged"
sleep "$duration"
