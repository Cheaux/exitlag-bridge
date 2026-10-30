#!/usr/bin/env bash
# Research-only proof that a connected-but-silent helper cannot pin the shim.
# Uses loopback IPC only; it never binds NFQUEUE or modifies nftables.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
shim_dir="$repo_root/shim/nfapi"
work="$(mktemp -d)"
port="${EXL_TEST_PORT:-51902}"
silent_pid="" test_pid="" helper_pid=""

cleanup() {
    set +e
    [[ -n "$silent_pid" ]] && kill "$silent_pid" 2>/dev/null
    [[ -n "$test_pid" ]] && kill "$test_pid" 2>/dev/null
    [[ -n "$helper_pid" ]] && kill "$helper_pid" 2>/dev/null
    [[ -n "$silent_pid" ]] && wait "$silent_pid" 2>/dev/null
    [[ -n "$test_pid" ]] && wait "$test_pid" 2>/dev/null
    [[ -n "$helper_pid" ]] && wait "$helper_pid" 2>/dev/null
    rm -r -- "$work"
}
trap cleanup EXIT

"$shim_dir/build.sh" >/dev/null
clang-cl /nologo /c /O2 /GS- /GR- /EHs-c- /Zl \
    "$repo_root/tests/nfapi-reconnect-smoketest.cpp" \
    "/Fo$work/nfapi-reconnect-smoketest.obj"
lld-link /entry:mainCRTStartup /subsystem:console /nodefaultlib \
    "/out:$work/nfapi-reconnect-smoketest.exe" \
    "$work/nfapi-reconnect-smoketest.obj" \
    "$shim_dir/build/nfapi.lib" "$shim_dir/build/kernel32.lib"
cp "$shim_dir/build/nfapi.dll" "$work/nfapi.dll"

# First endpoint: accept the HELLO and remain completely silent for longer than
# the shim's five-second receive timeout.
python3 - "$port" <<'PY' &
import socket, sys, time
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1])))
s.listen(4)
c, _ = s.accept()
c.recv(4096)
time.sleep(8)
c.close()
s.close()
PY
silent_pid=$!
sleep 0.2

# The shim will not open a connection without a token to present, so the silent
# endpoint would never be contacted and the retry behaviour under test would not
# be exercised. Seed a placeholder; the real helper overwrites it with its own
# session token later, and the shim re-reads the file on every attempt.
printf '%064x\n' 1 >"$work/token"
chmod 600 "$work/token"

wine_log="Z:${work//\//\\}\\shim.log"
(
    cd "$work"
    env WINEPREFIX="$repo_root/runtime/prefix-game" \
        EXITLAG_NETD="127.0.0.1:$port" EXITLAG_NFAPI_TRACE=1 \
        EXITLAG_NETD_TOKEN="Z:${work//\//\\}\\token" \
        EXITLAG_NFAPI_LOG="$wine_log" \
        "$repo_root/wine-wbemprox/runtime-staging/x86_64-unix/wine" \
        ./nfapi-reconnect-smoketest.exe >"$work/wine.out" 2>&1
) &
test_pid=$!

wait "$silent_pid"
silent_pid=""
"$repo_root/backend/exitlag-netd" --no-queue --port "$port" \
    --token-file "$work/token" --log "$work/helper.log" &
helper_pid=$!

wait "$test_pid"
test_pid=""
kill "$helper_pid" 2>/dev/null || true
wait "$helper_pid" 2>/dev/null || true
helper_pid=""

connects="$(grep -c 'call=ipc.connect ' "$work/shim.log" || true)"
helper_connects="$(grep -c 'shim connected' "$work/helper.log" || true)"
grep -q 'shim connected' "$work/helper.log"
# Derive the expected version from the header so this assertion cannot go stale
# the way it did when the handshake gained a token and became version 3.
expected_version="$(grep -oE '^#define EXL_PROTOCOL_VERSION[[:space:]]+[0-9]+' \
                    "$repo_root/backend/protocol.h" | grep -oE '[0-9]+$')"
grep -q "call=ipc.hello_ack version=$expected_version" "$work/shim.log"
(( connects >= 2 ))
(( helper_connects == 1 ))
printf 'PASS: silent endpoint timed out; shim retried, then held one healthy version-%s session (%s total attempts)\n' "$expected_version" "$connects"
