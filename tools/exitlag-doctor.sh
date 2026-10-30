#!/usr/bin/env bash
# Pre-flight diagnostics. Run this first when something does not work.
#
#   tools/exitlag-doctor.sh            # check everything it can without root
#   sudo tools/exitlag-doctor.sh       # additionally check netfilter and locks
#
# Every failure states what is wrong, why it matters, and what to do about it.
# Nothing here changes any state.
set -uo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
prefix="${EXITLAG_PREFIX:-$repo_root/runtime/prefix-game}"
app_dir="${EXITLAG_APP_DIR:-$repo_root/runtime/app-shim}"
local_wine="$repo_root/wine-wbemprox/runtime-staging/x86_64-unix/wine"
supported_wine="11.17"
ok_count=0; warn_count=0; fail_count=0

c_ok=""; c_warn=""; c_bad=""; c_off=""
if [[ -t 1 ]]; then c_ok=$'\e[32m'; c_warn=$'\e[33m'; c_bad=$'\e[31m'; c_off=$'\e[0m'; fi

pass() { printf '  %sOK%s    %s\n' "$c_ok" "$c_off" "$1"; ok_count=$((ok_count+1)); }
warn() { printf '  %sWARN%s  %s\n' "$c_warn" "$c_off" "$1"; [[ -n "${2:-}" ]] && printf '        %s\n' "$2"; [[ -n "${3:-}" ]] && printf '        fix: %s\n' "$3"; warn_count=$((warn_count+1)); }
bad()  { printf '  %sFAIL%s  %s\n' "$c_bad" "$c_off" "$1"; [[ -n "${2:-}" ]] && printf '        %s\n' "$2"; [[ -n "${3:-}" ]] && printf '        fix: %s\n' "$3"; fail_count=$((fail_count+1)); }
section() { printf '\n%s\n' "$1"; }

# ----------------------------------------------------------------- host
section "Host"

if [[ "$(uname -s)" == "Linux" ]]; then pass "Linux $(uname -r)"
else bad "not Linux" "this project is a Linux packet backend" "run on Linux"; fi

if command -v nft >/dev/null 2>&1; then pass "nftables present ($(nft --version 2>/dev/null | head -1))"
else bad "nftables (nft) not found" \
        "the backend installs its rules with nft; without it nothing can be steered" \
        "install the 'nftables' package"; fi

ldconfig_out="$(ldconfig -p 2>/dev/null || true)"
if [[ "$ldconfig_out" == *libmnl* ]] || compgen -G "/usr/lib*/libmnl.so*" >/dev/null 2>&1; then pass "libmnl present"
else bad "libmnl not found" \
        "exitlag-netd links against libmnl to talk to NFQUEUE" \
        "install 'libmnl' (and its -dev package to rebuild)"; fi

lsmod_out="$(lsmod 2>/dev/null || true)"
if [[ -e /proc/net/netfilter/nfnetlink_queue ]] || [[ "$lsmod_out" == *nfnetlink_queue* ]]; then
    pass "NFQUEUE available"
else
    warn "NFQUEUE not visibly loaded" \
         "packets are handed to the helper through NFQUEUE; it may be built in" \
         "if steering fails, try: sudo modprobe nfnetlink_queue"
fi

if [[ $EUID -eq 0 ]]; then pass "running as root (netfilter checks enabled)"
elif sudo -n true 2>/dev/null; then pass "passwordless sudo available"
else warn "not root and sudo needs a password" \
          "the backend needs CAP_NET_ADMIN; the runners call sudo" \
          "run the backend with sudo when you start it"; fi

# ----------------------------------------------------------------- wine
section "Wine"

wine_bin=""
if [[ -x "$local_wine" ]]; then wine_bin="$local_wine"; wine_kind="project-local"
elif command -v wine >/dev/null 2>&1; then wine_bin="$(command -v wine)"; wine_kind="system"
fi

if [[ -z "$wine_bin" ]]; then
    bad "no Wine found" \
        "ExitLag is a Windows program and runs under Wine" \
        "install Wine, or build the project-local one (wine-wbemprox/build-local.sh)"
else
    wine_version="$("$wine_bin" --version 2>/dev/null)"
    if [[ "$wine_version" == *"$supported_wine"* ]]; then
        pass "Wine $wine_version ($wine_kind)"
    else
        warn "Wine $wine_version ($wine_kind); verified against $supported_wine Staging" \
             "the crash guards target the exact set of imports this Wine does not export; a different build may miss or add some" \
             "if ExitLag crashes opening a settings page, re-check docs/wine-stub-crash-guards.md"
    fi
    if [[ "$wine_kind" == "system" ]]; then
        warn "using system Wine, not the project-local build" \
             "the WMI fix (Win32_LogicalDiskRootDirectory) lives in the project-local Wine" \
             "build it with wine-wbemprox/build-local.sh, or expect the WMI query to fail"
    fi
fi

if [[ -d "$prefix" ]]; then
    pass "Wine prefix present ($prefix)"
    [[ -d "$prefix/drive_c" ]] || bad "prefix has no drive_c" \
        "the prefix looks uninitialised" \
        "WINEPREFIX=$prefix wineboot -i"
else
    bad "Wine prefix missing ($prefix)" \
        "ExitLag runs inside this prefix" \
        "create it: WINEPREFIX=$prefix wineboot -i   (or set EXITLAG_PREFIX)"
fi

# ----------------------------------------------------------------- exitlag
section "ExitLag"

if [[ -f "$app_dir/ExitLag.exe" ]]; then
    pass "ExitLag.exe found ($app_dir)"
    if [[ -x "$repo_root/tools/check-exitlag-version.sh" ]]; then
        drift="$("$repo_root/tools/check-exitlag-version.sh" 2>&1)"; drift_rc=$?
        case $drift_rc in
            0) pass "ExitLag build matches the verified baseline" ;;
            1) warn "ExitLag build has changed since the baseline" \
                    "the shim reproduces a private ABI against a specific build; an update can change it" \
                    "run tools/check-exitlag-abi.sh, then re-record with tools/check-exitlag-version.sh --record" ;;
            *) warn "no ExitLag baseline recorded" \
                    "version drift cannot be detected until one exists" \
                    "tools/check-exitlag-version.sh --record" ;;
        esac
    fi
else
    bad "ExitLag.exe not found in $app_dir" \
        "this project steers traffic for the official client; it does not replace it" \
        "install the official ExitLag client into that directory (see README.md)"
fi

if [[ -f "$app_dir/nfapi.dll" ]]; then
    # Shim builds are not byte-reproducible (the PE header carries a timestamp),
    # so compare identity rather than bytes: our shim contains its own log
    # markers, the vendor DLL does not.
    if strings -a "$app_dir/nfapi.dll" 2>/dev/null | grep -q 'compat.install'; then
        pass "nfapi.dll in place and is this project's shim"
    else
        bad "nfapi.dll in $app_dir is NOT this project's shim" \
            "it looks like the vendor DLL, which expects the Windows driver; nothing will be steered and ExitLag may fail to start" \
            "shim/nfapi/build.sh && cp shim/nfapi/build/nfapi.dll $app_dir/"
    fi
else
    bad "nfapi.dll missing from $app_dir" \
        "without the shim ExitLag has no backend and nothing is steered" \
        "shim/nfapi/build.sh && cp shim/nfapi/build/nfapi.dll $app_dir/"
fi

# ----------------------------------------------------------------- capability
section "Capabilities"

if [[ -x "$repo_root/backend/exitlag-netd" ]]; then
    if grep -q 'tcp_redirect_v6' "$repo_root/backend/nft-setup.sh" 2>/dev/null; then
        pass "IPv6 active rewrite supported (mechanism validated synthetically)"
        printf '        no ExitLag-provided IPv6 redirect has been observed in a real game\n'
    else
        warn "IPv6 active rewrite not available" \
             "an IPv6 game whose callback returns an IPv6 target could not be steered" \
             "update backend/nft-setup.sh; see docs/ipv6-active-rewrite.md"
    fi
    pass "IPv4 active rewrite supported (proven with live game traffic)"
fi

if [[ -e /proc/sys/net/ipv6/conf/all/disable_ipv6 ]]; then
    if [[ "$(cat /proc/sys/net/ipv6/conf/all/disable_ipv6 2>/dev/null)" == "1" ]]; then
        printf '  %s--%s    IPv6 is disabled on this host; IPv6 games cannot be steered\n' "$c_warn" "$c_off"
    else
        pass "host has IPv6 enabled"
    fi
fi

# ----------------------------------------------------------------- backend
section "Backend"

if [[ -x "$repo_root/backend/exitlag-netd" ]]; then pass "exitlag-netd built"
else bad "exitlag-netd not built" \
         "this is the privileged helper that does the steering" \
         "cd backend && make"; fi

if pgrep -x exitlag-netd >/dev/null 2>&1; then
    pass "exitlag-netd is running"
    if [[ -e /run/exitlag-linux/ipc.token ]]; then
        pass "IPC session token published (helper is reachable by the shim)"
    else
        warn "helper running but no session token published" \
             "the shim cannot authenticate, so nothing will be steered" \
             "restart the backend runner; see docs/security-review.md"
    fi
else
    printf '  %s--%s    exitlag-netd is not running (expected unless you started a run)\n' "$c_warn" "$c_off"
    [[ -e /run/exitlag-linux/ipc.token ]] && warn "a stale IPC token exists with no helper running" \
        "left by an unclean exit" \
        "harmless; the next run replaces it"
fi

if pgrep -f '^\./ExitLag\.exe' >/dev/null 2>&1; then pass "ExitLag is running"
else printf '  %s--%s    ExitLag is not running\n' "$c_warn" "$c_off"; fi

# ----------------------------------------------------------------- state
section "Runtime state"

if [[ $EUID -eq 0 ]] || sudo -n true 2>/dev/null; then
    sudo_maybe(){ if [[ $EUID -eq 0 ]]; then "$@"; else sudo -n "$@"; fi; }
    if sudo_maybe nft list table inet exitlag >/dev/null 2>&1; then
        if pgrep -x exitlag-netd >/dev/null 2>&1; then
            pass "inet exitlag table present and a helper is running"
        else
            warn "stale 'inet exitlag' table with no helper running" \
                 "rules are installed but nothing is serving the queue; traffic still flows (queue bypass) but nothing is steered" \
                 "backend/nft-teardown.sh"
        fi
    else
        pass "no leftover nftables state"
    fi
else
    printf '  %s--%s    skipped netfilter checks (needs root)\n' "$c_warn" "$c_off"
fi

if [[ -e /run/exitlag-linux/helper.pid ]]; then
    read -r stale_pid stale_bin < /run/exitlag-linux/helper.pid 2>/dev/null || true
    if [[ -n "${stale_pid:-}" ]] && [[ -d "/proc/$stale_pid" ]]; then
        pass "runtime lock held by live helper pid $stale_pid"
    else
        warn "stale runtime pidfile (/run/exitlag-linux/helper.pid -> pid ${stale_pid:-?})" \
             "a previous runner did not shut down cleanly" \
             "the next run recovers it automatically; or remove the file"
    fi
else
    pass "no stale runtime lock"
fi

# ----------------------------------------------------------------- setup state
section "Installation"

state_dir="${EXITLAG_STATE_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/exitlag-linux}"
if [[ -d "$state_dir" ]]; then
    pass "managed installation present ($state_dir)"
    [[ -f "$state_dir/install-manifest.txt" ]] \
        && pass "install manifest present (uninstall can clean up safely)" \
        || warn "no install manifest in $state_dir" \
                "uninstall will refuse to act without one" \
                "re-run tools/exitlag-setup.sh install"
else
    printf '  %s--%s    no managed installation (using this repo directly is fine)\n' "$c_warn" "$c_off"
    printf '        for a guided setup: tools/exitlag-setup.sh install\n'
fi

# ----------------------------------------------------------------- config
section "Identity map"

map="${EXITLAG_IDENTITY_MAP:-}"
if [[ -z "$map" ]]; then
    printf '  %s--%s    EXITLAG_IDENTITY_MAP not set\n' "$c_warn" "$c_off"
    printf '        only games inside ExitLag'"'"'s own Wine prefix will be routed.\n'
    printf '        to route a native, Proton or other-prefix game, see backend/identity-map.example\n'
elif [[ ! -f "$map" ]]; then
    bad "identity map not found: $map" \
        "the helper refuses to start without it once it is configured" \
        "create it or unset EXITLAG_IDENTITY_MAP (see backend/identity-map.example)"
else
    line_no=0; map_bad=0
    while IFS= read -r line || [[ -n "$line" ]]; do
        line_no=$((line_no+1))
        [[ -z "$line" || "${line:0:1}" == "#" ]] && continue
        if [[ "$line" != *$'\t'* ]]; then
            bad "identity map line $line_no has no tab separator" \
                "the format is SOURCE<TAB>WINDOWS_PATH; spaces are part of the paths, so only a tab separates them" \
                "re-enter that line with a literal tab"
            map_bad=1; continue
        fi
        src="${line%%$'\t'*}"
        case "$src" in
            wineprefix:*)
                dir="${src#wineprefix:}"
                [[ -d "$dir" ]] || { bad "identity map line $line_no: prefix does not exist: $dir" \
                    "a wineprefix: source must be an existing directory or the helper refuses to start" \
                    "correct the path, or remove the line"; map_bad=1; } ;;
            /*)
                [[ -e "$src" ]] || { bad "identity map line $line_no: path does not exist: $src" \
                    "an absolute source is matched against /proc/<pid>/exe and must resolve" \
                    "correct the path, or remove the line"; map_bad=1; } ;;
            *.exe|*.EXE) : ;;
            *) bad "identity map line $line_no: unrecognised source '$src'" \
                   "sources must be an absolute path, an exact .exe argv token, or wineprefix:<dir>" \
                   "see backend/identity-map.example"; map_bad=1 ;;
        esac
    done < "$map"
    (( map_bad )) || pass "identity map parses ($map)"
fi

# ----------------------------------------------------------------- summary
section "Summary"
printf '  %d ok, %d warnings, %d failures\n' "$ok_count" "$warn_count" "$fail_count"
if (( fail_count )); then
    printf '\n  Fix the FAIL items above before running. Routing will not work otherwise.\n'
    exit 1
elif (( warn_count )); then
    printf '\n  Usable, but read the warnings -- they explain what may not work.\n'
    exit 0
fi
printf '\n  Everything checks out.\n'
