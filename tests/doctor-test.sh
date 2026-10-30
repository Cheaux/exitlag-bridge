#!/usr/bin/env bash
# Verify exitlag-doctor distinguishes failure classes and never changes state.
# A checker that only ever says OK is useless, so each class is induced.
set -uo pipefail
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
doc="$repo_root/tools/exitlag-doctor.sh"
work="$(mktemp -d)"; pass=0; fail=0
ok(){ printf '  PASS  %s\n' "$1"; pass=$((pass+1)); }
bad(){ printf '  FAIL  %s -- %s\n' "$1" "$2"; fail=$((fail+1)); }
trap 'rm -rf "$work"' EXIT

expect(){ # expect <label> <pattern> <env...>
    local label="$1" pat="$2"; shift 2
    local out; out="$(env "$@" "$doc" 2>&1)"
    grep -qiE "$pat" <<<"$out" && ok "$label" || bad "$label" "no line matching /$pat/"
}

echo "=== each failure class is distinguished ==="
expect "missing Wine prefix"        'Wine prefix missing'            EXITLAG_PREFIX=/nonexistent/pfx
expect "ExitLag not installed"      'ExitLag.exe not found'          EXITLAG_APP_DIR="$work/empty"
mkdir -p "$work/noshim" && : > "$work/noshim/ExitLag.exe"
expect "shim missing"               'nfapi.dll missing'              EXITLAG_APP_DIR="$work/noshim"
printf 'not-our-dll' > "$work/noshim/nfapi.dll"
expect "wrong DLL selected"         'NOT this project'"'"'s shim'    EXITLAG_APP_DIR="$work/noshim"
expect "identity map missing"       'identity map not found'         EXITLAG_IDENTITY_MAP=/nonexistent.map
printf '/bin/sh C:\\g.exe\n' > "$work/bad.map"
expect "identity map malformed"     'no tab separator'               EXITLAG_IDENTITY_MAP="$work/bad.map"
printf 'wineprefix:/nope\tC:\\g.exe\n' > "$work/bad2.map"
expect "map source unresolvable"    'prefix does not exist'          EXITLAG_IDENTITY_MAP="$work/bad2.map"

echo; echo "=== capabilities are reported ==="
out="$("$doc" 2>&1)"
grep -q 'IPv4 active rewrite' <<<"$out" && ok "reports IPv4 capability" || bad "IPv4 capability" "absent"
grep -q 'IPv6 active rewrite' <<<"$out" && ok "reports IPv6 capability" || bad "IPv6 capability" "absent"
grep -qi 'no ExitLag-provided IPv6 redirect' <<<"$out" \
    && ok "IPv6 capability is qualified honestly, not claimed as proven" \
    || bad "IPv6 honesty" "the synthetic-only caveat is missing"

echo; echo "=== every failure explains itself ==="
out="$(EXITLAG_PREFIX=/nonexistent/pfx "$doc" 2>&1)"
# a FAIL block should carry a why line and a fix line
grep -A2 'FAIL.*Wine prefix missing' <<<"$out" | grep -q 'fix:' \
    && ok "failures include a 'fix:' line" || bad "fix line" "absent"

echo; echo "=== doctor never modifies the system ==="
before_nft="$(sudo -n nft list tables 2>/dev/null | md5sum)"
before_home="$(find "$HOME/.local/share" -maxdepth 1 2>/dev/null | sort | md5sum)"
"$doc" >/dev/null 2>&1
[[ "$(sudo -n nft list tables 2>/dev/null | md5sum)" == "$before_nft" ]] \
    && ok "nftables unchanged by running doctor" || bad "read-only nft" "tables changed"
[[ "$(find "$HOME/.local/share" -maxdepth 1 2>/dev/null | sort | md5sum)" == "$before_home" ]] \
    && ok "no state directory created by running doctor" || bad "read-only home" "something was created"

echo; echo "=== exit codes are meaningful ==="
EXITLAG_PREFIX=/nonexistent/pfx "$doc" >/dev/null 2>&1
[[ $? -ne 0 ]] && ok "non-zero exit when something is broken" || bad "exit code" "exited 0 despite a failure"
# A tree without the official client is legitimately unhealthy, so only assert
# the healthy case where ExitLag is actually installed.
if [[ -f "${EXITLAG_APP_DIR:-$repo_root/runtime/app-shim}/ExitLag.exe" ]]; then
    "$doc" >/dev/null 2>&1
    [[ $? -eq 0 ]] && ok "zero exit on a healthy system" || bad "exit code" "non-zero on a healthy system"
else
    printf '  SKIP  zero exit on a healthy system (ExitLag not installed here)\n'
fi

echo; printf '%d passed, %d failed\n' "$pass" "$fail"
exit $(( fail > 0 ))
