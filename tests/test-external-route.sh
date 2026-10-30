#!/usr/bin/env bash
# Research-only end-to-end proof for process identity translation. ExitLag must
# already be running with an enabled/configured game profile. The script uses
# the real ExitLag callback, a transient project-owned nftables table, one
# mapped native process, and one unmapped negative control.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
dest="${PROBE_DEST:-66.40.178.239}"
dest_port="${PROBE_PORT:-3724}"
windows_path="${WINDOWS_GAME_PATH:-G:\World of Warcraft\_classic_era_\WoWClassic.exe}"
stamp="$(date +%Y%m%dT%H%M%S)"
outdir="$repo_root/runtime/logs/external-route-$stamp"
work="$(mktemp -d)"
runner_pid=""

[[ $EUID -eq 0 ]] || { echo "run with sudo (NFQUEUE/nftables need CAP_NET_ADMIN)" >&2; exit 1; }
mkdir -p "$outdir"

cleanup() {
    set +e
    [[ -n "$runner_pid" ]] && kill -TERM "$runner_pid" 2>/dev/null
    [[ -n "$runner_pid" ]] && wait "$runner_pid" 2>/dev/null
    rm -r -- "$work"
}
trap cleanup EXIT

cp "$(readlink -f "$(command -v python3)")" "$work/native-game-probe"
chmod 0755 "$work/native-game-probe"
printf '%s\t%s\n' "$work/native-game-probe" "$windows_path" >"$work/identity.map"

TEST_DEST="$dest" ENABLE_REDIRECT=1 EXITLAG_IDENTITY_MAP="$work/identity.map" \
    NETD_LOG="$outdir/helper.log" \
    "$repo_root/backend/run-tcp-redirect.sh" 45 >"$outdir/runner.log" 2>&1 &
runner_pid=$!

for _ in $(seq 1 150); do
    grep -q 'shim connected' "$outdir/helper.log" 2>/dev/null && break
    kill -0 "$runner_pid" 2>/dev/null || { cat "$outdir/runner.log"; exit 1; }
    sleep 0.1
done
grep -q 'shim connected' "$outdir/helper.log" 2>/dev/null || {
    echo "ExitLag did not connect; enable its configured route and retry" >&2
    exit 1
}

"$work/native-game-probe" -c \
    'import socket,sys; s=socket.socket(); s.settimeout(8); s.connect((sys.argv[1],int(sys.argv[2]))); s.close()' \
    "$dest" "$dest_port" &
positive_pid=$!
wait "$positive_pid"

python3 -c \
    'import socket,sys; s=socket.socket(); s.settimeout(8); s.connect((sys.argv[1],int(sys.argv[2]))); s.close()' \
    "$dest" "$dest_port" &
negative_pid=$!
wait "$negative_pid"

grep -q "resolve pid=$positive_pid identity=translated" "$outdir/helper.log"
grep -q "pid=$positive_pid .*DNAT armed; ACCEPT (active)" "$outdir/helper.log"
grep -q "resolve pid=$negative_pid identity=genuine" "$outdir/helper.log"
grep -q "pid=$negative_pid .*ACCEPT unchanged (fail-open/no redirect)" "$outdir/helper.log"

echo "PASS: mapped native PID $positive_pid routed; unmapped PID $negative_pid remained unchanged"
echo "evidence: $outdir"
