#!/usr/bin/env bash
# Regression suite entry point.
#
#   tests/run-all.sh unit          no privileges, no ExitLag  (any contributor)
#   sudo tests/run-all.sh system   adds netfilter and IPC tests
#   sudo tests/run-all.sh live     adds tests needing a running ExitLag
#   sudo tests/run-all.sh all      everything available in this environment
#
# Default is `unit`. Tiers are cumulative. A tier whose requirements are not met
# is reported as SKIP with the reason, never as a pass.
set -uo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
tier="${1:-unit}"
stamp="$(date +%Y%m%dT%H%M%S)"
outdir="$repo_root/runtime/logs/suite-$stamp"
mkdir -p "$outdir"
pass=0; fail=0; skip=0; results=()

have_root()    { [[ $EUID -eq 0 ]]; }
have_exitlag() { pgrep -f '^\./ExitLag\.exe' >/dev/null 2>&1; }
# The official client is not redistributable, so a fresh checkout will not have
# it. Tests that inspect it must skip with a reason, not look like breakage.
have_exitlag_files() { [[ -f "${EXITLAG_APP_DIR:-$repo_root/runtime/app-shim}/ExitLag.exe" ]]; }

run_test() {   # run_test <name> <requirement: none|root|exitlag> <command...>
    local name="$1" req="$2"; shift 2
    case "$req" in
        root)    have_root    || { results+=("SKIP  $name (needs sudo)"); skip=$((skip+1)); return; } ;;
        exitlag) have_root    || { results+=("SKIP  $name (needs sudo)"); skip=$((skip+1)); return; }
                 have_exitlag || { results+=("SKIP  $name (ExitLag is not running)"); skip=$((skip+1)); return; } ;;
        installed) have_exitlag_files || {
                     results+=("SKIP  $name (the official ExitLag client is not installed)")
                     skip=$((skip+1)); return; } ;;
    esac
    printf '\n--- %s\n' "$name"
    # Unprivileged tests run as the invoking user even under sudo: building as
    # root would leave root-owned objects in a user-owned tree and break the
    # next plain build.
    local -a cmd=("$@")
    if [[ "$req" == none && -n "${SUDO_USER:-}" ]]; then
        cmd=(sudo -u "$SUDO_USER" --preserve-env=PATH,HOME env "$@")
    fi
    if "${cmd[@]}" >"$outdir/${name//[^a-zA-Z0-9]/_}.log" 2>&1; then
        results+=("PASS  $name"); pass=$((pass+1)); printf '    PASS\n'
    else
        results+=("FAIL  $name  (see $outdir/${name//[^a-zA-Z0-9]/_}.log)"); fail=$((fail+1))
        printf '    FAIL -- last lines:\n'; tail -5 "$outdir/${name//[^a-zA-Z0-9]/_}.log" | sed 's/^/      /'
    fi
}

echo "regression suite: tier=$tier"
echo "logs: $outdir"
have_root    && echo "  root: yes"    || echo "  root: no (system/live tiers will skip)"
have_exitlag && echo "  ExitLag: running" || echo "  ExitLag: not running (live tier will skip)"

# ---------------------------------------------------------------- unit
if [[ "$tier" == unit || "$tier" == system || "$tier" == live || "$tier" == all ]]; then
    echo; echo "===== unit ====="
    run_test "shim builds"            none "$repo_root/shim/nfapi/build.sh"
    run_test "helper builds"          none make -B -C "$repo_root/backend"
    run_test "nfapi ABI matches ExitLag" installed "$repo_root/tools/check-exitlag-abi.sh"
    run_test "identity map pos/neg"   none "$repo_root/tests/test-identity-map.sh"
    run_test "crash-guard smoketest"  none "$repo_root/tests/build-nfapi-compat-smoketest.sh"
    run_test "doctor self-check"      installed "$repo_root/tools/exitlag-doctor.sh"
    run_test "doctor diagnoses"       none "$repo_root/tests/doctor-test.sh"
    run_test "RPC surface scope"      none "$repo_root/tests/rpc-surface-test.sh"
    run_test "process discovery"      none "$repo_root/tests/discovery-test.sh"
    run_test "identity-map assistant" none "$repo_root/tests/identity-assistant-test.sh"
    run_test "setup simulation"       none "$repo_root/tests/setup-simulation-test.sh"
fi

# ---------------------------------------------------------------- system
if [[ "$tier" == system || "$tier" == live || "$tier" == all ]]; then
    echo; echo "===== system (needs root) ====="
    run_test "IPC security negatives" root "$repo_root/tests/ipc-security-test.sh"
    run_test "IPC hostile input"      root "$repo_root/tests/ipc-fuzz-test.sh"
    run_test "lifecycle hardening"    root "$repo_root/tests/harden-runtime-test.sh"
    run_test "IPv6 active rewrite"    root "$repo_root/tests/ipv6-redirect-test.sh"
fi

# ---------------------------------------------------------------- live
if [[ "$tier" == live || "$tier" == all ]]; then
    echo; echo "===== live (needs root + running ExitLag) ====="
    run_test "external Wine prefix routing" exitlag "$repo_root/tests/validate-external-prefix.sh"
fi

# ---------------------------------------------------------------- summary
echo; echo "===== summary ====="
printf '%s\n' "${results[@]}"
printf '\n%d passed, %d failed, %d skipped\n' "$pass" "$fail" "$skip"

if (( skip )); then
    echo
    echo "Skipped tests are not passes. To run them:"
    have_root          || echo "  - re-run with sudo for the system tier"
    have_exitlag_files || echo "  - install the official ExitLag client (see README.md); it is not redistributable, so a fresh checkout will not have it"
    have_exitlag       || echo "  - start ExitLag (tools/run-exitlag-production.sh) for the live tier"
fi

cat <<'NOTE'

Not automated (need a real game and a human):
  - TCP steering with live WoW world traffic
  - UDP take-and-reinject against a real game's datagrams
  - Proton routing with a real title
  See docs/architecture.md for what each of those proved.
NOTE
exit $(( fail > 0 ))
