#!/usr/bin/env bash
# Negative tests for the helper's privileged RPC surface.
#
#   tests/rpc-surface-test.sh
#
# These run as an AUTHENTICATED peer. Authentication is not authorisation: a
# client that holds the session token must still not be able to make the root
# helper do more than the proven runtime needs. IPC-only (--no-queue).
set -uo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
port="${EXL_TEST_PORT:-51912}"
work="$(mktemp -d)"
pass=0; fail=0

cleanup() { [[ -n "${helper_pid:-}" ]] && { kill "$helper_pid" 2>/dev/null; wait "$helper_pid" 2>/dev/null; }; rm -rf "$work"; }
trap cleanup EXIT
ok()  { printf '  PASS  %s\n' "$1"; pass=$((pass+1)); }
bad() { printf '  FAIL  %s -- %s\n' "$1" "$2"; fail=$((fail+1)); }

"$repo_root/backend/exitlag-netd" --no-queue --port "$port" \
    --token-file "$work/token" --log "$work/helper.log" &
helper_pid=$!
for _ in $(seq 1 100); do
    grep -q 'listening for shim' "$work/helper.log" 2>/dev/null && [[ -s "$work/token" ]] && break
    sleep 0.05
done
[[ -s "$work/token" ]] || { echo "helper did not start" >&2; exit 3; }

echo "=== PID resolution scope ==="
# An authenticated peer asks about a PID the helper never reported (init).
python3 - "$port" "$work/token" 1 <<'PY' >"$work/pid1.out" 2>&1
import socket, struct, sys, binascii
MAGIC = 0x314c5845
tok = binascii.unhexlify(open(sys.argv[2]).read().strip())
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5)
s.settimeout(5)

def recv_exact(n):
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise RuntimeError("closed")
        buf += chunk
    return buf

def recv_frame():
    magic, kind, flags, length = struct.unpack("<IHHI", recv_exact(12))
    assert magic == MAGIC, "bad magic in reply"
    return kind, (recv_exact(length) if length else b"")

def recv_kind(want):
    # The helper heartbeats (PING) every 2s, so the next frame is not
    # necessarily the reply; skip anything that is not what we asked for.
    for _ in range(20):
        kind, body = recv_frame()
        if kind == want:
            return body
    raise RuntimeError("no 0x%x frame arrived" % want)

s.sendall(struct.pack("<IHHI", MAGIC, 1, 0, 48) + struct.pack("<IIQ", 3, 4242, 0) + tok)
recv_kind(0x0002)                                 # HELLO_ACK
s.sendall(struct.pack("<IHHI", MAGIC, 0x20, 0, 16) + struct.pack("<QII", 7, int(sys.argv[3]), 0))
body = recv_kind(0x0021)                          # RESOLVE_PID_REPLY
path_len = struct.unpack("<QII", body[:16])[1] if len(body) >= 16 else -1
print("path_length=%d" % path_len)
s.close()
PY
grep -q 'path_length=0' "$work/pid1.out" && grep -q 'REFUSED (not a pid this helper reported)' "$work/helper.log" \
    && ok "an unreported PID is refused, not resolved" \
    || bad "unreported PID refused" "$(cat "$work/pid1.out")"

echo
echo "=== redirect target scope ==="
echo "  (checked statically: the helper refuses non-loopback DNAT targets)"
if grep -q 'non-loopback target' "$repo_root/backend/exitlag-netd.c"; then
    ok "non-loopback redirect targets are refused by policy"
else
    bad "non-loopback redirect policy" "the guard is not present in the helper"
fi
if grep -q 'allow-remote-redirect' "$repo_root/backend/exitlag-netd.c"; then
    ok "the policy has an explicit, documented override"
else
    bad "override flag" "no way to re-enable for deliberate experiments"
fi

echo
echo "=== oversized and malformed payloads from an authenticated peer ==="
python3 - "$port" "$work/token" <<'PY' >/dev/null 2>&1
import socket, struct, sys, binascii
MAGIC = 0x314c5845
tok = binascii.unhexlify(open(sys.argv[2]).read().strip())
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=5)
s.sendall(struct.pack("<IHHI", MAGIC, 1, 0, 48) + struct.pack("<IIQ", 3, 4242, 0) + tok)
s.settimeout(4); s.recv(64)
for frame in (
    struct.pack("<IHHI", MAGIC, 0x20, 0, 4) + b"\x00" * 4,            # short resolve req
    struct.pack("<IHHI", MAGIC, 0x42, 0, 0xFFFF0000),                  # absurd udp length
    struct.pack("<IHHI", MAGIC, 0x0011, 0, 8) + b"\x00" * 8,           # short tcp reply
    struct.pack("<IHHI", MAGIC, 0x20, 0, 16) + struct.pack("<QII", 9, 0xFFFFFFFF, 0),  # pid -1
):
    try: s.sendall(frame)
    except Exception: break
s.close()
PY
sleep 0.5
kill -0 "$helper_pid" 2>/dev/null && ok "helper survived malformed privileged requests" \
                                  || bad "helper survived malformed requests" "helper died"

echo
printf '%d passed, %d failed\n' "$pass" "$fail"
exit $(( fail > 0 ))
