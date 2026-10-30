#!/usr/bin/env bash
# Research-only, IPC-only identity translation test.  It does not create an
# nftables table, bind NFQUEUE, or send game traffic.
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
work="$(mktemp -d)"
port="${EXL_TEST_PORT:-51901}"
helper_pid=""
mapped_pid=""
spoof_pid=""

cleanup() {
    set +e
    [[ -n "$helper_pid" ]] && kill "$helper_pid" 2>/dev/null
    [[ -n "$mapped_pid" ]] && kill "$mapped_pid" 2>/dev/null
    [[ -n "$spoof_pid" ]] && kill "$spoof_pid" 2>/dev/null
    [[ -n "$helper_pid" ]] && wait "$helper_pid" 2>/dev/null
    [[ -n "$mapped_pid" ]] && wait "$mapped_pid" 2>/dev/null
    [[ -n "$spoof_pid" ]] && wait "$spoof_pid" 2>/dev/null
    rm -r -- "$work"
}
trap cleanup EXIT

sleep_exe="$(readlink -f "$(command -v sleep)")"
python_exe="$(readlink -f "$(command -v python3)")"
windows_path='G:\World of Warcraft\_classic_era_\WoWClassic.exe'
printf '%s\t%s\n' "$sleep_exe" "$windows_path" >"$work/identity.map"

sleep 30 & mapped_pid=$!
# Negative control: the old researchd test could smuggle this .exe-looking
# argument through argv. Strict mapping mode must report python itself.
python3 -c 'import time; time.sleep(30)' "$windows_path" & spoof_pid=$!

"$repo_root/backend/exitlag-netd" --no-queue --port "$port" --token-file "$work/token" --allow-any-pid-resolve \
    --identity-map "$work/identity.map" --log "$work/helper.log" &
helper_pid=$!
for _ in $(seq 1 100); do
    grep -q 'listening for shim' "$work/helper.log" 2>/dev/null && break
    kill -0 "$helper_pid" 2>/dev/null || { cat "$work/helper.log"; exit 1; }
    sleep 0.02
done

python3 - "$port" "$mapped_pid" "$spoof_pid" "$windows_path" "$python_exe" "$work/token" <<'PY'
import binascii
import socket, struct, sys

port, mapped_pid, spoof_pid = map(int, sys.argv[1:4])
expected_mapped, expected_spoof = sys.argv[4:6]
# The helper now requires this session's token in the handshake.
TOKEN = binascii.unhexlify(open(sys.argv[6]).read().strip())
MAGIC = 0x314C5845

def frame(kind, payload):
    return struct.pack("<IHHI", MAGIC, kind, 0, len(payload)) + payload

def recv_exact(sock, size):
    data = b""
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise RuntimeError("unexpected EOF")
        data += part
    return data

def receive(sock):
    magic, kind, flags, length = struct.unpack("<IHHI", recv_exact(sock, 12))
    if magic != MAGIC:
        raise RuntimeError("bad magic")
    return kind, recv_exact(sock, length)

def receive_reply(sock, expected_kind):
    while True:
        kind, payload = receive(sock)
        if kind == 0x03 and not payload:
            sock.sendall(frame(0x04, b""))
            continue
        if kind != expected_kind:
            raise RuntimeError(f"unexpected reply {kind:#x}")
        return payload

def resolve(sock, tag, pid):
    sock.sendall(frame(0x20, struct.pack("<QII", tag, pid, 0)))
    payload = receive_reply(sock, 0x21)
    reply_tag, length, reserved = struct.unpack("<QII", payload[:16])
    if reply_tag != tag or reserved != 0:
        raise RuntimeError("malformed resolve reply")
    return payload[16:16 + length].decode("utf-8")

with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
    sock.sendall(frame(1, struct.pack("<IIQ", 3, 9999, 0) + TOKEN))
    payload = receive_reply(sock, 2)
    translated = resolve(sock, 1, mapped_pid)
    genuine = resolve(sock, 2, spoof_pid)

print(f"mapped={translated}")
print(f"negative={genuine}")
if translated != expected_mapped:
    raise SystemExit(f"mapped identity mismatch: {translated!r}")
if genuine != expected_spoof:
    raise SystemExit(f"strict negative mismatch: {genuine!r}")
PY

grep -q "resolve pid=$mapped_pid identity=translated" "$work/helper.log"
grep -q "resolve pid=$spoof_pid identity=genuine" "$work/helper.log"
echo "PASS: exact native identity translated; unmapped .exe argv spoof rejected"
