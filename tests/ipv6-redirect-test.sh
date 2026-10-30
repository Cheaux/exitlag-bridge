#!/usr/bin/env bash
# Synthetic integration proof for IPv6 active TCP rewrite.
#
#   sudo tests/ipv6-redirect-test.sh
#
# No ExitLag game has yet returned an IPv6 redirect target, so this stands in
# for one. It is NOT a unit test: it drives the real privileged path end to end
# -- the production helper, real nftables state, a real IPv6 TCP connection --
# with a harness playing the shim's role over the authenticated IPC socket and
# returning a legitimate IPv6 mutation.
#
#   IPv6 client -> helper -> (harness returns ::1 target) -> nft IPv6 DNAT
#                -> local IPv6 listener -> reply -> reverse translation -> client
set -uo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
port="${EXL_TEST_PORT:-51920}"
listen_port="${EXL_V6_LISTEN:-39999}"
# A real, routable external IPv6 so the kernel actually emits a SYN to queue.
dest="${V6_DEST:-2606:4700::6812:b908}"
dest_port=443
work="$(mktemp -d)"
pass=0; fail=0

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
ok()  { printf '  PASS  %s\n' "$1"; pass=$((pass+1)); }
bad() { printf '  FAIL  %s -- %s\n' "$1" "$2"; fail=$((fail+1)); }

cleanup() {
    set +e
    [[ -n "${harness_pid:-}" ]] && kill "$harness_pid" 2>/dev/null
    [[ -n "${listener_pid:-}" ]] && kill "$listener_pid" 2>/dev/null
    [[ -n "${helper_pid:-}"  ]] && kill "$helper_pid" 2>/dev/null
    "$repo_root/backend/nft-teardown.sh" >/dev/null 2>&1
    rm -rf "$work"
}
trap cleanup EXIT

ip -6 addr show dev lo 2>/dev/null | grep -q '::1' || { echo "no IPv6 loopback; skipping" >&2; exit 0; }
ip -6 route get "$dest" >/dev/null 2>&1 || { echo "no IPv6 route to $dest; skipping (needs IPv6 connectivity)" >&2; exit 0; }

# ---- local IPv6 listener standing in for ExitLag's proxy -------------------
python3 - "$listen_port" "$work/listener.log" <<'PY' &
import socket, sys
s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("::1", int(sys.argv[1]))); s.listen(4)
log = open(sys.argv[2], "w", buffering=1)
while True:
    c, a = s.accept()
    log.write("accepted from %s\n" % (a,))
    try:
        data = c.recv(256)
        log.write("received %d bytes: %r\n" % (len(data), data[:32]))
        c.sendall(b"IPV6-PROXY-OK")
        log.write("replied\n")
    except Exception as e:
        log.write("error %r\n" % (e,))
    c.close()
PY
listener_pid=$!
sleep 1

# ---- the production helper, real netfilter --------------------------------
"$repo_root/backend/exitlag-netd" --queue 0 --port "$port" --apply-tcp \
    --token-file "$work/token" --log "$work/helper.log" &
helper_pid=$!
for _ in $(seq 1 100); do
    grep -q 'bound NFQUEUE 0' "$work/helper.log" 2>/dev/null && [[ -s "$work/token" ]] && break
    sleep 0.05
done
grep -q 'bound NFQUEUE 0' "$work/helper.log" || { echo "helper did not start" >&2; exit 3; }

# ---- harness acting as the shim: returns an IPv6 redirect to ::1 -----------
start_harness() {   # start_harness <mode> <logfile>
python3 - "$port" "$work/token" "$listen_port" "$2" "$1" <<'PY' &

import socket, struct, sys, binascii
MAGIC=0x314c5845
port, tokfile, lport, logf = int(sys.argv[1]), sys.argv[2], int(sys.argv[3]), sys.argv[4]
mode = sys.argv[5] if len(sys.argv) > 5 else "valid"
log=open(logf,"w",buffering=1)
tok=binascii.unhexlify(open(tokfile).read().strip())
s=socket.create_connection(("127.0.0.1",port),timeout=30); s.settimeout(30)
def rx(n):
    b=b""
    while len(b)<n:
        c=s.recv(n-len(b))
        if not c: raise RuntimeError("closed")
        b+=c
    return b
s.sendall(struct.pack("<IHHI",MAGIC,1,0,48)+struct.pack("<IIQ",3,4242,0)+tok)
while True:
    magic,kind,flags,length=struct.unpack("<IHHI",rx(12))
    body=rx(length) if length else b""
    if kind==0x0003:                                  # PING
        s.sendall(struct.pack("<IHHI",MAGIC,0x0004,0,0)); continue
    if kind!=0x0010: continue                         # TCP_CONNECT_REQ
    # exl_tcp_connect_req: tag Q, endpoint_id Q, process_id I, filtering_flag I,
    # direction B, reserved[3], ip_family H, local_address[28], remote_address[28]
    tag,eid,pid,flag = struct.unpack("<QQII", body[0:24])
    ip_family = struct.unpack("<H", body[28:30])[0]
    remote = bytearray(body[58:86])                    # remote_address[28]
    fam = struct.unpack("<H", bytes(remote[0:2]))[0]   # sockaddr family, little-endian
    log.write("req tag=%d ip_family=%d sockaddr_family=%d\n" % (tag, ip_family, fam))
    if fam == 23:                                      # Windows AF_INET6
        remote[2:4]  = struct.pack(">H", lport)
        if mode == "valid":         remote[8:24] = b"\x00"*15 + b"\x01"        # ::1
        elif mode == "unspecified": remote[8:24] = b"\x00"*16                  # ::
        elif mode == "multicast":   remote[8:24] = b"\xff\x02" + b"\x00"*13 + b"\x01"
        elif mode == "nonloopback": remote[8:24] = bytes.fromhex("20010db8") + b"\x00"*11 + b"\x01"
        elif mode == "zeroport":
            remote[2:4] = b"\x00\x00"; remote[8:24] = b"\x00"*15 + b"\x01"
        verdict, mutated = 1, 1
        log.write("mode=%s returning target\n" % mode)
    else:
        verdict, mutated = 0, 0
    # exl_tcp_connect_reply: tag Q, verdict I, filtering_flag I,
    # remote_address[28], mutated_flag B, mutated_remote B, reserved[2]
    reply = (struct.pack("<QII", tag, verdict, flag | 0x200) + bytes(remote)
             + struct.pack("<BBBB", 1 if mutated else 0, mutated, 0, 0))
    assert len(reply) == 48, len(reply)
    s.sendall(struct.pack("<IHHI", MAGIC, 0x0011, 0, len(reply)) + reply)
PY
harness_pid=$!
}

start_harness valid "$work/harness.log"
sleep 2
grep -q 'closed\|Traceback' "$work/harness.log" 2>/dev/null && { echo "harness failed:"; cat "$work/harness.log"; exit 3; }

QUEUE_NUM=0 TEST_DEST="$dest" "$repo_root/backend/nft-setup.sh" >/dev/null 2>&1 || { echo "nft-setup failed" >&2; exit 3; }

echo "=== IPv6 map installed? ==="
nft list map inet exitlag tcp_redirect_v6 >/dev/null 2>&1 \
    && ok "tcp_redirect_v6 map exists" || bad "tcp_redirect_v6 map exists" "missing"
nft list chain inet exitlag output_nat 2>/dev/null | grep -q 'tcp_redirect_v6' \
    && ok "IPv6 dnat rule present" || bad "IPv6 dnat rule present" "missing"

echo; echo "=== real IPv6 connection through the production path ==="
python3 - "$dest" "$dest_port" "$work/client.log" <<'PY'
import socket, sys
s=socket.socket(socket.AF_INET6, socket.SOCK_STREAM); s.settimeout(8)
log=open(sys.argv[3],"w",buffering=1)
try:
    s.connect((sys.argv[1], int(sys.argv[2])))
    log.write("peer=%s\n" % (s.getpeername(),))
    s.sendall(b"hello-from-v6-client")
    log.write("reply=%r\n" % (s.recv(64),))
except Exception as e:
    log.write("error=%r\n" % (e,))
finally:
    s.close()
PY
sleep 1
grep -q 'IPV6-PROXY-OK' "$work/client.log" 2>/dev/null \
    && ok "IPv6 connection was redirected to the local listener" \
    || bad "IPv6 redirect" "client did not reach the listener: $(tr '\n' ' ' < "$work/client.log")"
grep -q 'accepted from' "$work/listener.log" 2>/dev/null \
    && ok "listener accepted the redirected connection" || bad "listener accepted" "no accept logged"
grep -q "$dest" "$work/client.log" 2>/dev/null \
    && ok "client still observes the ORIGINAL remote endpoint (reverse translation)" \
    || bad "reverse translation" "getpeername did not report $dest: $(tr '\n' ' ' < "$work/client.log")"
grep -q 'DNAT armed' "$work/helper.log" 2>/dev/null \
    && ok "helper armed the IPv6 DNAT" || bad "helper armed DNAT" "not logged"

echo; echo "=== invalid IPv6 targets are actually rejected (real connections) ==="
# Each mode makes the harness return a deliberately illegal target. The helper
# must refuse it, log why, and leave the connection unredirected.
for mode in unspecified multicast zeroport nonloopback; do
    kill "$harness_pid" 2>/dev/null; wait "$harness_pid" 2>/dev/null
    : > "$work/helper.log"
    start_harness "$mode" "$work/harness-$mode.log"
    sleep 1.5
    python3 - "$dest" "$dest_port" >/dev/null 2>&1 <<'PY'
import socket, sys
s=socket.socket(socket.AF_INET6, socket.SOCK_STREAM); s.settimeout(4)
try: s.connect((sys.argv[1], int(sys.argv[2])))
except Exception: pass
finally: s.close()
PY
    sleep 1
    if grep -q 'DNAT armed' "$work/helper.log" 2>/dev/null; then
        bad "reject $mode" "helper ARMED a redirect for an illegal target"
    elif grep -qE 'invalid unicast target|port zero|non-loopback|non-IPv6' "$work/helper.log" 2>/dev/null; then
        ok "rejected $mode: $(grep -oE 'callback returned [a-z ]+' "$work/helper.log" | head -1)"
    else
        bad "reject $mode" "no redirect armed, but no refusal logged either"
    fi
done

echo; echo "=== teardown removes all IPv6 state ==="
"$repo_root/backend/nft-teardown.sh" >/dev/null 2>&1
nft list map inet exitlag tcp_redirect_v6 >/dev/null 2>&1 \
    && bad "teardown removes IPv6 map" "map still present" \
    || ok "teardown removed the IPv6 map and all table state"

echo; printf '%d passed, %d failed\n' "$pass" "$fail"
echo "logs: $work (removed on exit)"
exit $(( fail > 0 ))
