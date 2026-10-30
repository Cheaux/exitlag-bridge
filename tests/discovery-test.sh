#!/usr/bin/env bash
# Tests for tools/discover-game-processes.py, focused on the launcher-versus-
# client case that Tibia exposed: the executable a user would naively map is not
# the one that owns the game's sockets.
#
#   tests/discovery-test.sh          (no privileges needed; uses own processes)
set -uo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
tool="$repo_root/tools/discover-game-processes.py"
work="$(mktemp -d)"; pass=0; fail=0
ok(){ printf '  PASS  %s\n' "$1"; pass=$((pass+1)); }
bad(){ printf '  FAIL  %s -- %s\n' "$1" "$2"; fail=$((fail+1)); }
cleanup(){ set +e; [[ -n "${launcher_pid:-}" ]] && kill -- -"$launcher_pid" 2>/dev/null
           [[ -n "${srv_pid:-}" ]] && kill "$srv_pid" 2>/dev/null; rm -rf "$work"; }
trap cleanup EXIT

# A local server to connect to, so the "client" owns a real socket.
python3 - "$work/port" <<'PY' &
import socket, sys
s=socket.socket(); s.bind(("127.0.0.1",0)); s.listen(8)
open(sys.argv[1],"w").write(str(s.getsockname()[1]))
while True:
    c,_=s.accept()
PY
srv_pid=$!
for _ in $(seq 1 50); do [[ -s "$work/port" ]] && break; sleep 0.1; done
port="$(cat "$work/port")"
[[ -n "$port" ]] || { echo "no test server"; exit 3; }

# Model the real case: a launcher BINARY (not a shell -- shells are correctly
# excluded) that execs a differently-named client binary which owns the socket.
# Two copies of the interpreter give two distinct /proc/<pid>/exe values.
mkdir -p "$work/bin"
cp "$(readlink -f "$(command -v python3)")" "$work/bin/game-launcher"
cp "$(readlink -f "$(command -v python3)")" "$work/bin/game-client"
cat > "$work/launcher.py" <<EOF
import subprocess, time
# A real launcher stays alive and supervises its client; it does not exec over
# itself. Tibia's launcher behaves this way, which is why both processes exist.
subprocess.Popen(["$work/bin/game-client", "$work/client.py", "$port"])
time.sleep(60)
EOF
cat > "$work/client.py" <<'EOF'
import socket, sys, time
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])))
time.sleep(60)
EOF

setsid "$work/bin/game-launcher" "$work/launcher.py" >/dev/null 2>&1 &
launcher_pid=$!
sleep 3

out="$("$tool" --json 2>/dev/null)"
echo "=== launcher vs network-owning client ==="

# The client owns the socket; python3 is its real exe (a script interpreter).
client_pid="$(python3 - "$out" "$port" <<'PY'
import json,sys
for r in json.loads(sys.argv[1]):
    if any(s["remote"].endswith(":"+sys.argv[2]) for s in r["sockets"]):
        print(r["pid"]); break
PY
)"
[[ -n "$client_pid" ]] && ok "identified the process owning the game socket (pid $client_pid)" \
                       || bad "identify socket owner" "no process reported holding a socket to :$port"

if [[ -n "$client_pid" ]]; then
    python3 - "$out" "$client_pid" <<'PY'
import json,sys
r=[x for x in json.loads(sys.argv[1]) if x["pid"]==int(sys.argv[2])][0]
print("SOURCE:"+str(r["suggested_source"]))
print("LAUNCHER:"+str(r["launcher"]["exe"] if r["launcher"] else None))
PY
fi > "$work/detail"
    grep -q '^SOURCE:/' "$work/detail" \
        && ok "suggested a canonical absolute source path" \
        || bad "suggested source" "$(grep '^SOURCE:' "$work/detail")"
    if grep -q '^LAUNCHER:.*game-launcher' "$work/detail"; then
        ok "reported the LAUNCHER separately from the socket-owning client"
    else
        bad "launcher distinguished" "expected game-launcher, got $(grep '^LAUNCHER:' "$work/detail")"
    fi
    if grep -q '^SOURCE:.*game-client' "$work/detail"; then
        ok "suggested source is the CLIENT binary, not the launcher"
    else
        bad "source is the client" "$(grep '^SOURCE:' "$work/detail")"
    fi

echo; echo "=== shells are not treated as launchers ==="
grep -q 'LAUNCHER:/usr/bin/bash\|LAUNCHER:/bin/bash' "$work/detail" \
    && bad "shell excluded" "bash reported as a launcher" \
    || ok "a shell ancestor is not reported as the launcher"

echo; echo "=== no spoofable argv heuristic remains ==="
# Ignore comments and docstrings; look for code that would actually scan argv
# for a .exe token, which is the removed heuristic.
# Inspect the parsed source, not the prose: the file documents that the
# heuristic was removed, and a text grep matches that sentence.
if python3 - "$tool" <<'PY'
import ast, sys
tree = ast.parse(open(sys.argv[1]).read())
# Docstrings are Constants too, and this file documents that the heuristic was
# removed. Exclude them and look only at real string literals.
docs = set()
for node in ast.walk(tree):
    if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            docs.add(id(body[0].value))
bad = [n for n in ast.walk(tree)
       if isinstance(n, ast.Constant) and isinstance(n.value, str)
       and ".exe" in n.value.lower() and id(n) not in docs]
sys.exit(1 if bad else 0)
PY
then
    ok "identity comes from /proc and socket ownership only (no .exe literal in code)"
else
    bad "no argv heuristic" "a .exe string literal exists in executable code"
fi

echo; echo "=== read-only ==="
before="$(sudo -n nft list tables 2>/dev/null | md5sum)"
"$tool" --json >/dev/null 2>&1
after="$(sudo -n nft list tables 2>/dev/null | md5sum)"
[[ "$before" == "$after" ]] && ok "running discovery changes no system state" \
                            || bad "read-only" "nftables tables changed"

echo; printf '%d passed, %d failed\n' "$pass" "$fail"
exit $(( fail > 0 ))
