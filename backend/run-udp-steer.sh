#!/usr/bin/env bash
# Stage 4 narrow active proof. Only one selected IPv4 UDP endpoint owned
# by one process enters the take-and-reinject path. Every unsupported callback shape,
# timeout, attribution miss, raw-send failure, or queue loss fails open.
#
#   sudo env TEST_DEST=203.0.113.1 UDP_TEST_DEST=any \
#     UDP_TEST_PORT=29523 UDP_TEST_EXE=WoWClassic.exe \
#     UDP_ACTIVE_INDEX=1 ENABLE_REDIRECT=1 ENABLE_UDP_STEER=1 \
#     backend/run-udp-steer.sh [seconds]
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
duration="${1:-}"
queue="${QUEUE_NUM:-0}"
udp_queue="${UDP_QUEUE_NUM:-1}"
port="${EXL_PORT:-51900}"
log="${NETD_LOG:-$repo_root/runtime/logs/netd-udp-steer-$(date +%Y%m%dT%H%M%S).log}"
udp_active_index="${UDP_ACTIVE_INDEX:-1}"
table_installed=0
source "$repo_root/backend/runtime-guard.sh"
mapfile -t identity_args < <(runtime_identity_args)

if [[ $EUID -ne 0 ]]; then echo "run with sudo (NFQUEUE and raw UDP need root)" >&2; exit 1; fi
: "${TEST_DEST:?set TEST_DEST=<ip|any> for the unchanged TCP path}"
: "${UDP_TEST_DEST:?set one IPv4 UDP destination or any}"
: "${UDP_TEST_PORT:?set one exact UDP destination port}"
: "${UDP_TEST_EXE:?set the required UDP owner-path substring}"
if [[ "$UDP_TEST_DEST" == *:* ]]; then
    echo "active UDP supports IPv4 destinations only" >&2; exit 2
fi
if [[ ! "$udp_active_index" =~ ^[1-9][0-9]*$ ]]; then
    echo "UDP_ACTIVE_INDEX must be a positive integer" >&2; exit 2
fi
if [[ "${ENABLE_REDIRECT:-}" != 1 || "${ENABLE_UDP_STEER:-}" != 1 ]]; then
    echo "refusing without ENABLE_REDIRECT=1 and ENABLE_UDP_STEER=1" >&2; exit 2
fi
if [[ "$queue" == "$udp_queue" ]]; then echo "QUEUE_NUM and UDP_QUEUE_NUM must differ" >&2; exit 2; fi

runtime_guard_acquire "$repo_root"

mkdir -p "$(dirname "$log")"
cleanup() {
    set +e
    [[ -n "${exitlag_timer_pid:-}" ]] && kill "$exitlag_timer_pid" 2>/dev/null
    [[ -n "${netd_pid:-}" ]] && kill "$netd_pid" 2>/dev/null
    [[ -n "${netd_pid:-}" ]] && wait "$netd_pid" 2>/dev/null
    if (( table_installed )); then "$repo_root/backend/nft-teardown.sh" >/dev/null 2>&1; fi
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

runtime_guard_spawn_helper "$repo_root/backend/exitlag-netd" --queue "$queue" --udp-queue "$udp_queue" \
    --port "$port" --apply-tcp --apply-udp --udp-active-once \
    --udp-active-index "$udp_active_index" --udp-exe "$UDP_TEST_EXE" \
    "${identity_args[@]}" --log "$log"
netd_pid=$exitlag_spawned_pid
runtime_guard_record_helper "$netd_pid"
for _ in {1..100}; do
    if grep -q "bound NFQUEUE $queue" "$log" 2>/dev/null &&
       grep -q "bound UDP NFQUEUE $udp_queue" "$log" 2>/dev/null; then break; fi
    kill -0 "$netd_pid" 2>/dev/null || { echo "exitlag-netd exited during startup; see $log" >&2; exit 3; }
    sleep 0.02
done
if ! grep -q "bound NFQUEUE $queue" "$log" 2>/dev/null ||
   ! grep -q "bound UDP NFQUEUE $udp_queue" "$log" 2>/dev/null; then
    echo "exitlag-netd did not become ready; see $log" >&2; exit 3
fi

QUEUE_NUM="$queue" TEST_DEST="$TEST_DEST" UDP_QUEUE_NUM="$udp_queue" \
UDP_TEST_DEST="$UDP_TEST_DEST" UDP_TEST_PORT="$UDP_TEST_PORT" \
ENABLE_UDP_OBSERVE=1 ENABLE_UDP_STEER=1 "$repo_root/backend/nft-setup.sh" >/dev/null
table_installed=1
echo "Stage 4 narrow steering ready: TCP scope=$TEST_DEST; UDP endpoint index=$udp_active_index $UDP_TEST_EXE -> $UDP_TEST_DEST:$UDP_TEST_PORT"
echo "exitlag-netd pid=$netd_pid  log=$log"
[[ -n "$duration" ]] && echo "running for ${duration}s; every unsupported UDP case fails open" \
                     || echo "running until Ctrl-C; every unsupported UDP case fails open"
runtime_guard_wait "$netd_pid" "$duration"
