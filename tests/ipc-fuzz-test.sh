#!/usr/bin/env bash
# Hostile-input testing for the authenticated privileged RPC surface.
#
#   sudo tests/ipc-fuzz-test.sh
#
# Complements ipc-security-test.sh (which covers authentication) by attacking
# the surface *after* a successful handshake -- the case where an attacker holds
# the token. Protocol semantics are not changed by this test; it only asserts
# the helper survives and refuses.
#
# Runs with --no-queue: netfilter is never touched, so nothing can be left
# behind.
set -uo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
port="${EXL_TEST_PORT:-51922}"
work="$(mktemp -d)"; pass=0; fail=0
ok(){ printf '  PASS  %s\n' "$1"; pass=$((pass+1)); }
bad(){ printf '  FAIL  %s -- %s\n' "$1" "$2"; fail=$((fail+1)); }
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
cleanup(){ set +e; [[ -n "${helper_pid:-}" ]] && kill "$helper_pid" 2>/dev/null
           "$repo_root/backend/nft-teardown.sh" >/dev/null 2>&1; rm -rf "$work"; }
trap cleanup EXIT

# EXITLAG_NETD_BIN lets this run against a sanitized build without duplicating
# the corpus; ASan/UBSan then report corruption the survival check cannot see.
HELPER="${EXITLAG_NETD_BIN:-$repo_root/backend/exitlag-netd}"
"$HELPER" --no-queue --port "$port" --allow-any-pid-resolve \
    --token-file "$work/token" --log "$work/helper.log" &
helper_pid=$!
for _ in $(seq 1 100); do
    grep -q 'listening for shim' "$work/helper.log" 2>/dev/null && [[ -s "$work/token" ]] && break
    sleep 0.05
done
[[ -s "$work/token" ]] || { echo "helper did not start" >&2; exit 3; }

# One authenticated session, then a battery of malformed frames on it.
fuzz() {   # fuzz <label> <python-expression-producing-bytes>
    python3 - "$port" "$work/token" "$2" >/dev/null 2>&1 <<'PY'
import socket, struct, sys, binascii
MAGIC=0x314c5845
tok=binascii.unhexlify(open(sys.argv[2]).read().strip())
s=socket.create_connection(("127.0.0.1",int(sys.argv[1])),timeout=5); s.settimeout(5)
s.sendall(struct.pack("<IHHI",MAGIC,1,0,48)+struct.pack("<IIQ",3,4242,0)+tok)
try: s.recv(64)
except Exception: pass
try:
    s.sendall(eval(sys.argv[3]))
    s.settimeout(1)
    s.recv(64)
except Exception:
    pass
finally:
    s.close()
PY
    sleep 0.15
    if kill -0 "$helper_pid" 2>/dev/null; then ok "survived: $1"
    else bad "survived: $1" "helper died"; fi
}

M='0x314c5845'
echo "=== framing and length ==="
fuzz "truncated header (3 bytes)"        "b'EXL'"
fuzz "header claiming 4 GiB"             "struct.pack('<IHHI',$M,0x0011,0,0xFFFFFFFF)"
fuzz "header claiming 1 GiB"             "struct.pack('<IHHI',$M,0x0011,0,1<<30)"
fuzz "length one byte over max payload"  "struct.pack('<IHHI',$M,0x0011,0,131073)"
fuzz "header then immediate close"       "struct.pack('<IHHI',$M,0x0011,0,4096)"
fuzz "zero-length body on a sized type"  "struct.pack('<IHHI',$M,0x0011,0,0)"
fuzz "bad magic after auth"              "struct.pack('<IHHI',0xDEADBEEF,0x0011,0,0)"

echo; echo "=== message types ==="
fuzz "unknown type 0x7FFF"               "struct.pack('<IHHI',$M,0x7FFF,0,0)"
fuzz "type 0 with payload"               "struct.pack('<IHHI',$M,0,0,8)+b'A'*8"
fuzz "helper-to-shim type sent inbound"  "struct.pack('<IHHI',$M,0x0010,0,0)"
fuzz "second HELLO mid-session"          "struct.pack('<IHHI',$M,1,0,48)+b'\\x00'*48"

echo; echo "=== sockaddr and address validation ==="
# tcp_connect_reply: tag Q, verdict I, flag I, remote[28], mutated_flag B, mutated_remote B, pad2
reply="lambda a: struct.pack('<IHHI',$M,0x0011,0,48)+struct.pack('<QII',1,1,0x210)+a+struct.pack('<BBBB',1,1,0,0)"
fuzz "reply with a 12-byte sockaddr"     "struct.pack('<IHHI',$M,0x0011,0,32)+b'\\x00'*32"
fuzz "reply, family 0"                   "($reply)(b'\\x00'*28)"
fuzz "reply, IPv4 0.0.0.0:0"             "($reply)(struct.pack('<HH',2,0)+b'\\x00'*24)"
fuzz "reply, IPv4 multicast"             "($reply)(struct.pack('<H',2)+struct.pack('>H',80)+bytes([224,0,0,1])+b'\\x00'*20)"
fuzz "reply, IPv4 broadcast"             "($reply)(struct.pack('<H',2)+struct.pack('>H',80)+b'\\xff\\xff\\xff\\xff'+b'\\x00'*20)"
fuzz "reply, IPv6 unspecified ::"        "($reply)(struct.pack('<H',23)+struct.pack('>H',80)+b'\\x00'*24)"
fuzz "reply, IPv6 multicast ff02::1"     "($reply)(struct.pack('<H',23)+struct.pack('>H',80)+b'\\x00'*4+b'\\xff\\x02'+b'\\x00'*13+b'\\x01'+b'\\x00'*4)"
fuzz "reply, IPv6 port zero"             "($reply)(struct.pack('<H',23)+b'\\x00\\x00'+b'\\x00'*4+b'\\x00'*15+b'\\x01'+b'\\x00'*4)"
fuzz "reply, family 0xFFFF"              "($reply)(struct.pack('<H',0xFFFF)+struct.pack('>H',80)+b'\\x00'*24)"

echo; echo "=== resolve-pid abuse ==="
rp="lambda p: struct.pack('<IHHI',$M,0x20,0,16)+struct.pack('<QII',7,p,0)"
fuzz "resolve pid 0"                     "($rp)(0)"
fuzz "resolve pid 0xFFFFFFFF"            "($rp)(0xFFFFFFFF)"
fuzz "resolve pid 1 (never reported)"    "($rp)(1)"
fuzz "resolve request, 4-byte body"      "struct.pack('<IHHI',$M,0x20,0,4)+b'\\x00'*4"

echo; echo "=== stream abuse ==="
fuzz "1 MiB of random bytes"             "__import__('os').urandom(1048576)"
fuzz "many tiny bad frames"              "b''.join(struct.pack('<IHHI',$M,0x7FFF,0,0) for _ in range(2000))"
fuzz "valid header, body arrives short"  "struct.pack('<IHHI',$M,0x0011,0,48)+b'\\x00'*10"

echo; echo "=== disconnect mid-request, then reconnect ==="
for i in 1 2 3; do
    python3 - "$port" "$work/token" >/dev/null 2>&1 <<'PY'
import socket, struct, binascii, sys
MAGIC=0x314c5845
tok=binascii.unhexlify(open(sys.argv[2]).read().strip())
s=socket.create_connection(("127.0.0.1",int(sys.argv[1])),timeout=5)
s.sendall(struct.pack("<IHHI",MAGIC,1,0,48)+struct.pack("<IIQ",3,4242,0)+tok)
s.sendall(struct.pack("<IHHI",MAGIC,0x0011,0,48)+b"\x00"*10)   # half a body
s.close()                                                       # then vanish
PY
done
sleep 0.5
kill -0 "$helper_pid" 2>/dev/null && ok "survived repeated mid-request disconnects" \
                                  || bad "mid-request disconnect" "helper died"

echo; echo "=== still serving a legitimate client afterwards ==="
python3 - "$port" "$work/token" > "$work/final" 2>&1 <<'PY'
import socket, struct, binascii, sys
MAGIC=0x314c5845
tok=binascii.unhexlify(open(sys.argv[2]).read().strip())
s=socket.create_connection(("127.0.0.1",int(sys.argv[1])),timeout=5); s.settimeout(5)
s.sendall(struct.pack("<IHHI",MAGIC,1,0,48)+struct.pack("<IIQ",3,4242,0)+tok)
print("ACCEPTED" if len(s.recv(64))>=12 else "REJECTED")
PY
grep -q ACCEPTED "$work/final" && ok "a valid authenticated session still works after all of it" \
                               || bad "still serving" "$(cat "$work/final")"

echo; echo "=== no netfilter state was created ==="
nft list table inet exitlag >/dev/null 2>&1 && bad "no nft state" "an exitlag table exists" \
                                             || ok "no nftables state left behind"

echo; printf '%d passed, %d failed\n' "$pass" "$fail"
exit $(( fail > 0 ))
