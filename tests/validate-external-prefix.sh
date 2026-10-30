#!/usr/bin/env bash
# Repeatable proof that same-prefix execution is no longer required:
# a Windows process in a DIFFERENT Wine prefix is routed by ExitLag.
#
#   1. start ExitLag (tools/run-exitlag-gameprefix.sh ... with a game route active)
#   2. sudo tests/validate-external-prefix.sh
#
# Creates two disposable Wine prefixes, maps only the first, and asserts that
# the mapped one is redirected and the unmapped one is not. Removes both
# prefixes on exit unless KEEP_PREFIXES=1.
#
# This is a validation harness, not part of the runtime path.
set -uo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
wine="$repo_root/wine-wbemprox/runtime-staging/x86_64-unix/wine"
dest="${PROBE_DEST:-66.40.178.239}"
port="${PROBE_PORT:-3724}"
target="${GAME_IDENTITY:-G:\\World of Warcraft\\_classic_era_\\WoWClassic.exe}"
stamp="$(date +%Y%m%dT%H%M%S)"
outdir="$repo_root/runtime/logs/extprefix-$stamp"
mapped="$repo_root/runtime/prefix-ext-mapped"
unmapped="$repo_root/runtime/prefix-ext-unmapped"
mapfile_path="$outdir/identity.map"
probe="$repo_root/tests/external-probe/external-tcp-probe.exe"
rc=0

[[ $EUID -eq 0 ]] || { echo "run with sudo (NFQUEUE/nftables need CAP_NET_ADMIN)" >&2; exit 1; }
[[ -x "$wine" ]]  || { echo "wine not found at $wine" >&2; exit 1; }
[[ -f "$probe" ]] || { echo "build the probe first: see tests/external-probe/" >&2; exit 1; }
pgrep -f '^\./ExitLag\.exe' >/dev/null || { echo "ExitLag is not running; start it with an active game route first" >&2; exit 1; }

# The prefixes must be owned by the invoking user, not root.
real_user="${SUDO_USER:-$USER}"
as_user() { sudo -u "$real_user" env "$@"; }

mkdir -p "$outdir"
cleanup() {
    set +e
    [[ -n "${runner_pid:-}" ]] && { kill "$runner_pid" 2>/dev/null; wait "$runner_pid" 2>/dev/null; }
    pkill -x exitlag-netd 2>/dev/null
    "$repo_root/backend/nft-teardown.sh" >/dev/null 2>&1
    if [[ "${KEEP_PREFIXES:-0}" != 1 ]]; then rm -rf -- "$mapped" "$unmapped"; fi
}
trap cleanup EXIT

make_prefix() {   # make_prefix <dir>
    rm -rf -- "$1"; mkdir -p "$1"
    chown "$real_user" "$1"
    as_user WINEPREFIX="$1" WINEDEBUG=-all timeout 240 "$wine" wineboot -i >/dev/null 2>&1
    mkdir -p "$1/drive_c/extprobe"
    cp "$probe" "$1/drive_c/extprobe/"
    chown -R "$real_user" "$1"
}

run_probe() {     # run_probe <prefix> <hold_ms>
    as_user WINEPREFIX="$1" WINEDEBUG=-all "$wine" \
        "$1/drive_c/extprobe/external-tcp-probe.exe" "$dest" "$port" "$2" 2>/dev/null |
        grep -vE 'MESA|pci id'
}

echo "building two disposable prefixes (this takes a minute) ..."
make_prefix "$mapped"
make_prefix "$unmapped"

printf 'wineprefix:%s\t%s\n' "$mapped" "$target" > "$mapfile_path"
echo "identity map:"; sed 's/\t/  ->  /' "$mapfile_path" | sed 's/^/  /'

EXITLAG_IDENTITY_MAP="$mapfile_path" TEST_DEST="$dest" ENABLE_REDIRECT=1 \
    NETD_LOG="$outdir/helper.log" "$repo_root/backend/run-tcp-redirect.sh" 300 \
    >"$outdir/runner.txt" 2>&1 &
runner_pid=$!
for _ in $(seq 1 200); do grep -q 'shim connected' "$outdir/helper.log" 2>/dev/null && break; sleep 0.1; done
grep -q 'shim connected' "$outdir/helper.log" || { echo "shim never connected; see $outdir/helper.log" >&2; exit 3; }
echo "helper ready; shim connected"

echo; echo "--- mapped prefix ---";   run_probe "$mapped" 4000
sleep 1
echo; echo "--- unmapped prefix ---"; run_probe "$unmapped" 4000
sleep 1

echo; echo "--- ExitLag's own Windows process table ---"
visible=$(as_user WINEPREFIX="$repo_root/runtime/prefix-game" WINEDEBUG=-all "$wine" tasklist.exe 2>/dev/null |
          grep -ci 'external-tcp-probe')
echo "external-tcp-probe entries visible to ExitLag: $visible"

echo; echo "=== results ==="
translated=$(grep -c 'identity=translated' "$outdir/helper.log")
redirected=$(grep -c 'DNAT armed' "$outdir/helper.log")
genuine=$(grep -c 'identity=genuine -> /usr/bin/wineserver' "$outdir/helper.log")
norelay=$(grep -c 'no redirect' "$outdir/helper.log")

check() { if [[ "$2" == "$3" ]]; then echo "  PASS  $1"; else echo "  FAIL  $1 (got $2, want $3)"; rc=1; fi; }
check "mapped prefix translated"            "$translated" 1
check "mapped prefix redirected"            "$redirected" 1
check "unmapped prefix kept genuine identity" "$genuine"   1
check "unmapped prefix not redirected"      "$norelay"    1
check "ExitLag cannot see the process"      "$visible"    0

echo; echo "log: $outdir/helper.log"
(( rc == 0 )) && echo "VALIDATION PASSED: same-prefix execution is not required" \
             || echo "VALIDATION FAILED"
exit $rc
