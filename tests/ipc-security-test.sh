#!/usr/bin/env bash
# Negative tests for the privileged IPC surface.
#
#   sudo tests/ipc-security-test.sh
#
# The helper runs as root and applies destination rewrites supplied over this
# socket, so its input is untrusted. Every scenario here tries to abuse it.
# Runs with --no-queue, so netfilter is never touched.
set -uo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
port="${EXL_TEST_PORT:-51908}"
outdir="$repo_root/runtime/logs/ipcsec-$(date +%Y%m%dT%H%M%S)"
log="$outdir/helper.log"
token_file="$outdir/token"
pass=0; fail=0

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
mkdir -p "$outdir"
owner_uid="${SUDO_UID:-$(id -u)}"

cleanup() { [[ -n "${helper_pid:-}" ]] && { kill "$helper_pid" 2>/dev/null; wait "$helper_pid" 2>/dev/null; }; }
trap cleanup EXIT

ok()  { printf '  PASS  %s\n' "$1"; pass=$((pass+1)); }
bad() { printf '  FAIL  %s -- %s\n' "$1" "$2"; fail=$((fail+1)); }

start_helper() {
    "$repo_root/backend/exitlag-netd" --no-queue --port "$port" \
        --peer-uid "$owner_uid" --token-file "$token_file" --log "$log" &
    helper_pid=$!
    for _ in $(seq 1 100); do
        grep -q 'listening for shim' "$log" 2>/dev/null && [[ -s "$token_file" ]] && return 0
        sleep 0.05
    done
    return 1
}
stop_helper() { kill "$helper_pid" 2>/dev/null; wait "$helper_pid" 2>/dev/null; helper_pid=""; }

# handshake <token-hex> <version> [trailing-junk-bytes]  -> prints ACCEPTED|REJECTED
handshake() {
    python3 - "$port" "$1" "$2" "${3:-0}" <<'PY'
import socket, struct, sys, binascii
port, tok_hex, version, junk = int(sys.argv[1]), sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
MAGIC = 0x314c5845
try:
    tok = binascii.unhexlify(tok_hex) if tok_hex else b"\x00" * 32
except Exception:
    tok = b"\x00" * 32
tok = (tok + b"\x00" * 32)[:32]
try:
    s = socket.create_connection(("127.0.0.1", port), timeout=4)
    body = struct.pack("<IIQ", version, 4242, 0) + tok + (b"X" * junk)
    s.sendall(struct.pack("<IHHI", MAGIC, 1, 0, len(body)) + body)
    s.settimeout(3)
    d = s.recv(64)
    print("ACCEPTED" if len(d) >= 12 else "REJECTED")
    s.close()
except Exception:
    print("REJECTED")
PY
}

echo "helper: 127.0.0.1:$port  owner uid $owner_uid"
start_helper || { echo "helper did not start; see $log" >&2; exit 3; }
good_token="$(tr -d '\n' < "$token_file")"

# ------------------------------------------------------------ authentication
echo; echo "=== authentication ==="

result="$(sudo -u nobody python3 - "$port" <<'PY' 2>&1
import socket, struct, sys
MAGIC = 0x314c5845
try:
    s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=4)
    body = struct.pack("<IIQ", 3, 4242, 0) + b"\x00" * 32
    s.sendall(struct.pack("<IHHI", MAGIC, 1, 0, len(body)) + body)
    s.settimeout(3)
    print("ACCEPTED" if len(s.recv(64)) >= 12 else "REJECTED")
except Exception:
    print("REJECTED")
PY
)"
[[ "$result" == REJECTED* ]] && grep -q 'REFUSED shim connection from uid' "$log" \
    && ok "wrong uid refused" || bad "wrong uid refused" "got: $result"

[[ "$(handshake "$(printf 'ab%.0s' {1..32})" 3)" == REJECTED ]] \
    && ok "correct uid, wrong token refused" || bad "correct uid, wrong token refused" "accepted"

[[ "$(handshake "" 3)" == REJECTED ]] \
    && ok "correct uid, all-zero token refused" || bad "all-zero token refused" "accepted"

[[ "$(handshake "$good_token" 3)" == ACCEPTED ]] \
    && ok "correct uid + correct token accepted" || bad "correct uid + correct token accepted" "rejected"

[[ "$(handshake "$good_token" 2)" == REJECTED ]] \
    && ok "correct token, wrong protocol version refused" || bad "wrong version refused" "accepted"

# ------------------------------------------------------------ token lifecycle
echo; echo "=== token lifecycle ==="
stale_token="$good_token"
stop_helper
[[ -f "$token_file" ]] && bad "token removed on exit" "file still present" || ok "token file removed on helper exit"

start_helper || { echo "helper restart failed" >&2; exit 3; }
new_token="$(tr -d '\n' < "$token_file")"
[[ "$new_token" != "$stale_token" ]] && ok "token rotated on restart" \
                                     || bad "token rotated on restart" "identical token reissued"
[[ "$(handshake "$stale_token" 3)" == REJECTED ]] \
    && ok "replayed token from the previous session refused" \
    || bad "replayed token refused" "a stale token was accepted"
[[ "$(handshake "$new_token" 3)" == ACCEPTED ]] \
    && ok "current token accepted after restart" || bad "current token after restart" "rejected"

# ------------------------------------------------------------ malformed auth
echo; echo "=== malformed authentication ==="
malformed() {   # malformed <label> <python-bytes-expr>
    python3 - "$port" "$2" <<'PY' >/dev/null 2>&1
import socket, struct, sys
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=4)
s.sendall(eval(sys.argv[2]))
s.settimeout(1)
try: s.recv(64)
except Exception: pass
s.close()
PY
    sleep 0.2
    kill -0 "$helper_pid" 2>/dev/null && ok "survived: $1" || bad "survived: $1" "helper died"
}
MAGIC='0x314c5845'
malformed "bad magic"                  "struct.pack('<IHHI',0xDEADBEEF,1,0,0)"
malformed "hello too short for token"  "struct.pack('<IHHI',$MAGIC,1,0,16)+b'A'*16"
malformed "absurd declared length"     "struct.pack('<IHHI',$MAGIC,1,0,0xFFFFFFFF)"
malformed "length beyond max payload"  "struct.pack('<IHHI',$MAGIC,1,0,1<<30)"
malformed "truncated header"           "b'EXL'"
malformed "header then no payload"     "struct.pack('<IHHI',$MAGIC,1,0,64)"
malformed "routing message before auth" "struct.pack('<IHHI',$MAGIC,0x0011,0,0)"
malformed "unknown message type"       "struct.pack('<IHHI',$MAGIC,0x7FFF,0,0)"
malformed "immediate disconnect"       "b''"
malformed "junk burst"                 "struct.pack('<IHHI',$MAGIC,1,0,16)+b'A'*4096"
[[ "$(handshake "$new_token" 3 64)" == ACCEPTED ]] \
    && ok "oversized but valid hello accepted (trailing bytes drained)" \
    || bad "oversized valid hello" "rejected"

# -------------------------------------------- authenticated, then hostile
echo; echo "=== malformed traffic AFTER a successful handshake ==="
python3 - "$port" "$new_token" <<'PY' >/dev/null 2>&1
import socket, struct, sys, binascii
MAGIC = 0x314c5845
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), timeout=4)
body = struct.pack("<IIQ", 3, 4242, 0) + binascii.unhexlify(sys.argv[2])
s.sendall(struct.pack("<IHHI", MAGIC, 1, 0, len(body)) + body)
s.settimeout(3)
s.recv(64)                                   # authenticated
for frame in (struct.pack("<IHHI", MAGIC, 0x0011, 0, 0xFFFFFFFF),
              struct.pack("<IHHI", MAGIC, 0x0021, 0, 1 << 30),
              struct.pack("<IHHI", 0xDEADBEEF, 1, 0, 0),
              b"\xff" * 512):
    try: s.sendall(frame)
    except Exception: break
s.close()
PY
sleep 0.5
kill -0 "$helper_pid" 2>/dev/null && ok "survived malformed traffic from an authenticated peer" \
                                  || bad "survived post-auth malformed traffic" "helper died"

echo; echo "=== liveness ==="
[[ "$(handshake "$new_token" 3)" == ACCEPTED ]] \
    && ok "helper still serving valid clients after every hostile input" \
    || bad "helper still serving" "a valid handshake now fails"

echo; echo "=== secret hygiene ==="
grep -q "$new_token" "$log" && bad "token never logged" "the token appears in the helper log" \
                            || ok "token never appears in the log"

echo; printf '%d passed, %d failed\n' "$pass" "$fail"
echo "log: $log"
exit $(( fail > 0 ))
