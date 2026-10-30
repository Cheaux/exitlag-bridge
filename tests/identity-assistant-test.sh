#!/usr/bin/env bash
# Tests for tools/identity-map-assistant.py, especially that it never invents a
# Windows identity and never writes an entry a human has not confirmed.
set -uo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
tool="$repo_root/tools/identity-map-assistant.py"
work="$(mktemp -d)"; pass=0; fail=0
ok(){ printf '  PASS  %s\n' "$1"; pass=$((pass+1)); }
bad(){ printf '  FAIL  %s -- %s\n' "$1" "$2"; fail=$((fail+1)); }
cleanup(){ set +e; [[ -n "${launcher_pid:-}" ]] && kill -- -"$launcher_pid" 2>/dev/null
           [[ -n "${srv_pid:-}" ]] && kill "$srv_pid" 2>/dev/null; rm -rf "$work"; }
trap cleanup EXIT

python3 - "$work/port" <<'PY' &
import socket, sys
s=socket.socket(); s.bind(("127.0.0.1",0)); s.listen(8)
open(sys.argv[1],"w").write(str(s.getsockname()[1]))
while True: c,_=s.accept()
PY
srv_pid=$!
for _ in $(seq 1 50); do [[ -s "$work/port" ]] && break; sleep 0.1; done
port="$(cat "$work/port")"

# launcher binary that survives + differently-named client that owns the socket
mkdir -p "$work/bin"
cp "$(readlink -f "$(command -v python3)")" "$work/bin/game-launcher"
cp "$(readlink -f "$(command -v python3)")" "$work/bin/game-client"
cat > "$work/launcher.py" <<EOF
import subprocess, time
subprocess.Popen(["$work/bin/game-client", "$work/client.py", "$port"])
time.sleep(60)
EOF
cat > "$work/client.py" <<'EOF'
import socket, sys, time
s=socket.create_connection(("127.0.0.1", int(sys.argv[1]))); time.sleep(60)
EOF
setsid "$work/bin/game-launcher" "$work/launcher.py" >/dev/null 2>&1 &
launcher_pid=$!
sleep 3

# Match the REMOTE endpoint: the test server also holds a socket on this port
# as its local address, and would otherwise be picked instead of the client.
pid="$("$tool" --json 2>/dev/null | python3 -c "
import json,sys
want='-> 127.0.0.1:$port'
for r in json.load(sys.stdin):
    if any(e.endswith(want) for e in r['endpoints']): print(r['pid']); break")"
[[ -n "$pid" ]] && ok "found the network-owning process (pid $pid)" \
                || { bad "found process" "none matched port $port"; echo "$pass passed, $((fail)) failed"; exit 1; }

echo "=== never invents a Windows identity ==="
j="$("$tool" --pid "$pid" --json 2>/dev/null)"
python3 - "$j" <<'PY' && ok "windows_identity is null until confirmed" || bad "windows_identity null" "it was populated"
import json,sys
r=json.loads(sys.argv[1])[0]
sys.exit(0 if r["windows_identity"] is None else 1)
PY
python3 - "$j" <<'PY' && ok "candidates are offered, each with its evidence" || bad "candidates" "none offered"
import json,sys
r=json.loads(sys.argv[1])[0]
sys.exit(0 if r["windows_candidates"] and all("evidence" in c for c in r["windows_candidates"]) else 1)
PY
python3 - "$j" <<'PY' && ok "suggested source is the client binary" || bad "suggested source" "wrong"
import json,sys
r=json.loads(sys.argv[1])[0]
sys.exit(0 if r["suggested_source"] and "game-client" in r["suggested_source"] else 1)
PY
python3 - "$j" <<'PY' && ok "launcher reported separately and flagged as not-the-owner" || bad "launcher" "not distinguished"
import json,sys
r=json.loads(sys.argv[1])[0]
sys.exit(0 if r["launcher"] and "game-launcher" in r["launcher"] else 1)
PY

echo; echo "=== refuses to write without confirmation ==="
: > "$work/m.map"
"$tool" --pid "$pid" --append "$work/m.map" >/dev/null 2>&1
rc=$?; [[ $rc -ne 0 && ! -s "$work/m.map" ]] \
    && ok "refuses --append without --windows-identity (exit $rc, file untouched)" \
    || bad "refuse unconfirmed" "wrote something or exited 0"

echo; echo "=== writes only a confirmed entry ==="
"$tool" --pid "$pid" --windows-identity 'C:\Games\Test\game.exe' --append "$work/m.map" >/dev/null 2>&1
grep -q $'\t''C:\\Games\\Test\\game.exe' "$work/m.map" \
    && ok "wrote a tab-separated entry with the confirmed identity" \
    || bad "write confirmed" "$(cat "$work/m.map")"
grep -q 'game-client' "$work/m.map" && ok "wrote the CLIENT as source, not the launcher" \
                                    || bad "source is client" "$(cat "$work/m.map")"

echo; echo "=== idempotent ==="
before="$(md5sum < "$work/m.map")"
"$tool" --pid "$pid" --windows-identity 'C:\Games\Test\game.exe' --append "$work/m.map" >/dev/null 2>&1
[[ "$(md5sum < "$work/m.map")" == "$before" ]] && ok "re-running does not duplicate the entry" \
                                               || bad "idempotent" "file changed"

echo; echo "=== the helper accepts the generated map ==="
t="$(mktemp -d)"
"$repo_root/backend/exitlag-netd" --no-queue --port 51921 --identity-map "$work/m.map" \
    --token-file "$t/k" --log "$t/l" >/dev/null 2>&1 &
hp=$!; sleep 1.2
grep -q 'identity map: loaded' "$t/l" && ok "generated map loads in the real helper" \
                                      || bad "map loads" "$(tail -2 "$t/l" 2>/dev/null)"
kill $hp 2>/dev/null; rm -rf "$t"

echo; printf '%d passed, %d failed\n' "$pass" "$fail"
exit $(( fail > 0 ))
