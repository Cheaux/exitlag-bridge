#!/usr/bin/env bash
# Stage 4 discovery runner: proven TCP redirect plus UDP observation only.
#
#   sudo env TEST_DEST=203.0.113.1 UDP_TEST_DEST=any UDP_TEST_PORT=29523 \
#       UDP_TEST_EXE=WoWClassic.exe ENABLE_REDIRECT=1 ENABLE_UDP_OBSERVE=1 \
#       backend/run-udp-observe.sh [seconds]
#
# UDP callbacks and every nested nf_udp* operation are recorded, but every
# queued datagram is accepted byte-for-byte unchanged. There is deliberately no
# UDP steering switch in this runner.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$repo_root/backend/runtime-guard.sh"
mapfile -t identity_args < <(runtime_identity_args)
duration="${1:-}"
queue="${QUEUE_NUM:-0}"
udp_queue="${UDP_QUEUE_NUM:-1}"
port="${EXL_PORT:-51900}"
log="${NETD_LOG:-$repo_root/runtime/logs/netd-udp-observe-$(date +%Y%m%dT%H%M%S).log}"
table_installed=0

if [[ $EUID -ne 0 ]]; then echo "run with sudo (NFQUEUE and nftables need CAP_NET_ADMIN)" >&2; exit 1; fi
: "${TEST_DEST:?set TEST_DEST=<ip|any> for the proven TCP path}"
: "${UDP_TEST_DEST:?set UDP_TEST_DEST=<ip|any> for UDP observation scope}"
: "${UDP_TEST_PORT:?set a UDP destination port, or an inclusive range LOW-HIGH}"
: "${UDP_TEST_EXE:?set the required UDP owner-path substring}"
if [[ "${ENABLE_REDIRECT:-}" != 1 || "${ENABLE_UDP_OBSERVE:-}" != 1 ]]; then
    echo "refusing without ENABLE_REDIRECT=1 and ENABLE_UDP_OBSERVE=1" >&2
    exit 2
fi
if [[ "$queue" == "$udp_queue" ]]; then echo "QUEUE_NUM and UDP_QUEUE_NUM must differ" >&2; exit 2; fi

mkdir -p "$(dirname "$log")"
cleanup() {
    set +e
    if (( table_installed )); then "$repo_root/backend/nft-teardown.sh" >/dev/null 2>&1; fi
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

netd_args=(--queue "$queue" --udp-queue "$udp_queue" --port "$port"
           --apply-tcp --observe-udp --udp-exe "$UDP_TEST_EXE" --log "$log")
"$repo_root/backend/exitlag-netd" "${netd_args[@]}" "${identity_args[@]}" &
netd_pid=$!
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
UDP_TEST_DEST="$UDP_TEST_DEST" ENABLE_UDP_OBSERVE=1 \
UDP_TEST_PORT="${UDP_TEST_PORT:-}" \
    "$repo_root/backend/nft-setup.sh" >/dev/null
table_installed=1
echo "Stage 4 observer ready: TCP active scope=$TEST_DEST; UDP unchanged scope=$UDP_TEST_DEST${UDP_TEST_PORT:+:$UDP_TEST_PORT} queue=$udp_queue"
echo "exitlag-netd pid=$netd_pid  log=$log"
if [[ -n "$duration" ]]; then
    echo "running for ${duration}s; every UDP verdict is ACCEPT unchanged"
    sleep "$duration"
else
    echo "running until Ctrl-C; every UDP verdict is ACCEPT unchanged"
    wait "$netd_pid"
fi
