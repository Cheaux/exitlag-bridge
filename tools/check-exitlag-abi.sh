#!/usr/bin/env bash
# Verify that the compatibility layer still matches the installed ExitLag build.
#
#   tools/check-exitlag-abi.sh
#
# ExitLag auto-updates. Rather than pinning one binary hash, this re-derives the
# things the shim actually depends on and checks them against the build that is
# installed right now:
#
#   1. every nfapi export ExitLag imports is exported by the shim
#   2. every import ExitLag has that Wine does not provide is covered by a crash
#      guard (an uncovered one is a fatal EXCEPTION_WINE_STUB waiting to happen)
#
# Exit codes: 0 compatible, 1 incompatible (do not run), 2 cannot check.
set -uo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
app_dir="${EXITLAG_APP_DIR:-$repo_root/runtime/app-shim}"
exe="$app_dir/ExitLag.exe"
shim="${EXITLAG_SHIM_DLL:-$repo_root/shim/nfapi/build/nfapi.dll}"
wine_dll_dir="${WINE_PE_DIR:-$repo_root/wine-wbemprox/runtime-staging/x86_64-windows}"
rc=0

command -v llvm-readobj >/dev/null 2>&1 || { echo "need llvm-readobj (package: llvm)" >&2; exit 2; }
[[ -f "$exe"  ]] || { echo "ExitLag.exe not found at $exe" >&2; exit 2; }
[[ -f "$shim" ]] || { echo "shim not built: $shim (run shim/nfapi/build.sh)" >&2; exit 2; }

# Imports of one DLL, as decorated symbol names.
imports_of() {   # imports_of <image> <dll-name>
    llvm-readobj --coff-imports "$1" 2>/dev/null |
      awk -v want="$2" '
        /^ *Import *\{/      { indll=0 }
        /Name: /             { if (tolower($2) == tolower(want)) indll=1; else if ($0 ~ /\.dll/) indll=0 }
        indll && /Symbol: /  { s=$0; sub(/.*Symbol: /,"",s); sub(/ \(.*/,"",s); if (s != "") print s }
      '
}
exports_of() { llvm-readobj --coff-exports "$1" 2>/dev/null | awk '/Name: /{s=$0; sub(/.*Name: /,"",s); print s}'; }

echo "ExitLag : $exe"
echo "shim    : $shim"

# ---------------------------------------------------------------- 1. nfapi ABI
echo
echo "== nfapi ABI =="
mapfile -t needed < <(imports_of "$exe" "nfapi.dll" | sort -u)
mapfile -t provided < <(exports_of "$shim" | sort -u)

if (( ${#needed[@]} == 0 )); then
    echo "  CANNOT CHECK: ExitLag imports no nfapi symbols."
    echo "    Either the build no longer uses nfapi, or the import table could not be read."
    echo "    Do not assume compatibility; investigate before running."
    exit 2
fi

missing=()
for sym in "${needed[@]}"; do
    found=0
    for have in "${provided[@]}"; do [[ "$sym" == "$have" ]] && { found=1; break; }; done
    (( found )) || missing+=("$sym")
done

echo "  ExitLag imports : ${#needed[@]} symbols"
echo "  shim exports    : ${#provided[@]} symbols"
if (( ${#missing[@]} == 0 )); then
    echo "  OK: the shim provides every nfapi symbol this build imports"
else
    echo "  INCOMPATIBLE: this ExitLag build imports ${#missing[@]} nfapi symbol(s) the shim does not export:"
    for sym in "${missing[@]}"; do
        echo "      $sym"
        if command -v llvm-undname >/dev/null 2>&1; then
            # Do not pipe llvm-undname's stdout: with a non-tty stdout it
            # switches to reading symbols from stdin and prints nothing.
            undname_out="$(llvm-undname "$sym" </dev/null 2>&1)"
            echo "        -> ${undname_out##*$'\n'}"
        fi
    done
    echo "    Why it matters: Wine's loader turns an unresolved import into a stub that"
    echo "    raises a non-continuable exception, so ExitLag will die when it calls one."
    echo "    What to do: add the export to shim/nfapi/nfapi_stub.cpp, rebuild, re-test."
    rc=1
fi

# ------------------------------------------------- 2. Wine missing-import guards
echo
echo "== Wine missing-import crash guards =="
if [[ ! -d "$wine_dll_dir" ]]; then
    echo "  SKIPPED: Wine PE dll directory not found ($wine_dll_dir)"
    echo "    Set WINE_PE_DIR to check whether new imports need crash guards."
else
    guarded="$(grep -oE '"[A-Za-z0-9_]+", *0, *0, *0' "$repo_root/shim/nfapi/nfapi_stub.cpp" 2>/dev/null | \
               grep -oE '"[A-Za-z0-9_]+"' | tr -d '"' | sort -u)"
    [[ -z "$guarded" ]] && guarded="$(sed -n '/g_guards\[GUARD_COUNT\]/,/};/p' "$repo_root/shim/nfapi/nfapi_stub.cpp" 2>/dev/null | \
               grep -oE '"[A-Za-z0-9_]+"' | tr -d '"' | sort -u)"

    uncovered=0; checked=0
    # Every DLL ExitLag imports from that Wine ships as a PE.
    mapfile -t dlls < <(llvm-readobj --coff-imports "$exe" 2>/dev/null |
                        awk '/Name: .*\.dll/{s=$2; print tolower(s)}' | sort -u)
    for dll in "${dlls[@]}"; do
        wine_dll="$wine_dll_dir/$dll"
        [[ -f "$wine_dll" ]] || continue
        checked=$((checked+1))
        wine_exports="$(exports_of "$wine_dll" | sort -u)"
        while read -r sym; do
            [[ -z "$sym" ]] && continue
            [[ "$sym" == \?* ]] && continue        # C++ decorated: not a Wine stub case
            if ! grep -qxF "$sym" <<<"$wine_exports"; then
                if grep -qxF "$sym" <<<"$guarded"; then
                    : # covered
                else
                    echo "  UNGUARDED: $dll!$sym is imported but not exported by Wine"
                    uncovered=$((uncovered+1))
                fi
            fi
        done < <(imports_of "$exe" "$dll")
    done

    if (( uncovered == 0 )); then
        echo "  OK: every import Wine does not provide is covered by a crash guard"
        echo "      ($checked DLLs checked, $(wc -l <<<"$guarded") guards installed)"
    else
        echo "    Why it matters: Wine resolves a missing import to a stub that raises"
        echo "    EXCEPTION_WINE_STUB (0x80000100), which is non-continuable and kills the"
        echo "    client as soon as that code path runs."
        echo "    What to do: add each to the guard table in shim/nfapi/nfapi_stub.cpp."
        echo "    See docs/wine-stub-crash-guards.md."
        rc=1
    fi
fi

# ---------------------------------------------------------------- verdict
echo
if (( rc == 0 )); then
    echo "VERDICT: compatible with the installed ExitLag build."
    echo "  Record it as known-good with: tools/check-exitlag-version.sh --record"
else
    echo "VERDICT: NOT compatible. Running anyway risks a crash or unsteered traffic."
fi
exit $rc
