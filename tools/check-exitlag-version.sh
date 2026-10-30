#!/usr/bin/env bash
# Detect that the ExitLag build under the shim has changed.
#
#   tools/check-exitlag-version.sh            # compare against the baseline
#   tools/check-exitlag-version.sh --record   # record the current build as baseline
#
# The shim reproduces a private ABI. An ExitLag update can change the nfapi
# surface it expects, the callback vtable layout, or the set of Wine imports the
# crash guards cover -- all silently. This records the identity of the build the
# runtime was verified against so drift is caught before it is debugged as a
# mystery.
#
# Exit codes: 0 unchanged, 1 drift detected, 2 no baseline recorded.
set -uo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exe="${EXITLAG_EXE:-$repo_root/runtime/app-shim/ExitLag.exe}"
baseline="$repo_root/runtime/exitlag-build-baseline.txt"

[[ -f "$exe" ]] || { echo "ExitLag.exe not found at $exe" >&2; exit 2; }

# The product version string, the bundled library version, and the image hash.
# The hash is what actually matters; the versions make a report readable.
version="$(strings -a "$exe" | grep -oE '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -u | tr '\n' ' ' | sed 's/ $//')"
sha="$(sha256sum "$exe" | cut -d' ' -f1)"
size="$(stat -c%s "$exe")"
# The imports the crash guards cover; a new build may add more missing ones.
guarded_imports="$(strings -a "$exe" | grep -cE '^(GetInterfaceDnsSettings|SetInterfaceDnsSettings|FreeInterfaceDnsSettings|GetOwnerModuleFromUdpEntry|GetOwnerModuleFromUdp6Entry|InitializeIpInterfaceEntry|ResolveIpNetEntry2|SetIpInterfaceEntry|PowerDuplicateScheme|PowerWriteDCValueIndex|PowerWriteFriendlyName)$')"

current="version=$version
sha256=$sha
size=$size
guarded_imports_present=$guarded_imports"

if [[ "${1:-}" == "--record" ]]; then
    printf '%s\n' "$current" > "$baseline"
    echo "recorded baseline:"; printf '%s\n' "$current"
    exit 0
fi

if [[ ! -f "$baseline" ]]; then
    echo "no baseline recorded. run: tools/check-exitlag-version.sh --record" >&2
    printf 'current:\n%s\n' "$current" >&2
    exit 2
fi

if diff -q <(printf '%s\n' "$current") "$baseline" >/dev/null; then
    echo "ExitLag build unchanged (version $version)"
    exit 0
fi

echo "ExitLag BUILD DRIFT DETECTED" >&2
diff <(printf '%s\n' "$current") "$baseline" | sed 's/^/  /' >&2
cat >&2 <<'NOTE'

The shim reproduces a private ABI against a specific build. Before trusting the
runtime on this one, re-check:
  - the nfapi export surface and callback vtable (research/reversing/analyze-pe.sh)
  - the Wine missing-import set the crash guards cover
    (docs/wine-stub-crash-guards.md)
  - a TCP and UDP data-plane run
Then re-record with --record.
NOTE
exit 1
