#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# Default to the project's own app directory, not a path that happens to exist
# on one developer's machine.
repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
app_dir="${1:-${EXITLAG_APP_DIR:-$repo_root/runtime/app-shim}}"
shim="$script_dir/build/nfapi.dll"
exe="$app_dir/ExitLag.exe"

test -f "$shim" || {
    echo "build the shim first: $script_dir/build.sh" >&2
    exit 1
}

tmp_dir="$(mktemp -d)"
trap 'rm -rf -- "$tmp_dir"' EXIT

llvm-readobj --coff-imports "$exe" |
    awk '
        $0 == "  Name: nfapi.dll" { active = 1; next }
        # Keep consuming the input instead of exiting early. With pipefail,
        # exiting here closes the pipe while llvm-readobj is still writing and
        # turns a successful comparison into an LLVM "Broken pipe" failure.
        active && /^}/ { active = 0; next }
        active && /^  Symbol:/ {
            sub(/^  Symbol: /, "")
            sub(/ \([0-9]+\)$/, "")
            print
        }
    ' |
    sort -u > "$tmp_dir/required"

llvm-readobj --coff-exports "$shim" |
    sed -n 's/^  Name: //p' |
    sort -u > "$tmp_dir/provided"

if comm -23 "$tmp_dir/required" "$tmp_dir/provided" | grep -q .; then
    echo "Missing required exports:"
    comm -23 "$tmp_dir/required" "$tmp_dir/provided"
    exit 1
fi

echo "ABI export check passed"
echo "required: $(wc -l < "$tmp_dir/required")"
echo "provided: $(wc -l < "$tmp_dir/provided")"
