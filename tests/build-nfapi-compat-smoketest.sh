#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
shim_dir="$repo_root/shim/nfapi"
build_dir="$shim_dir/build"
source_file="$repo_root/tests/nfapi-compat-smoketest.cpp"
object_file="$repo_root/tests/nfapi-compat-smoketest.obj"
output_file="$repo_root/tests/nfapi-compat-smoketest.exe"

"$shim_dir/build.sh" >/dev/null

clang-cl /nologo /c /O2 /GS- /GR- /EHs-c- /Zl \
    "$source_file" "/Fo$object_file"

# Import libraries for the two functions Wine does not export. Declaring them
# here is what makes this image carry the same missing imports ExitLag does.
llvm-dlltool -m i386:x86-64 -d "$shim_dir/iphlpapi-missing.def" -l "$build_dir/iphlpapi-missing.lib"
llvm-dlltool -m i386:x86-64 -d "$shim_dir/powrprof.def" -l "$build_dir/powrprof.lib"

lld-link /entry:mainCRTStartup /subsystem:console /nodefaultlib \
    "/out:$output_file" "$object_file" \
    "$build_dir/nfapi.lib" "$build_dir/iphlpapi-missing.lib" "$build_dir/powrprof.lib"

# The test image links against nfapi.dll, so a copy must sit beside it for the
# Wine loader to find; that copy is also what installs the guards.
cp "$build_dir/nfapi.dll" "$repo_root/tests/nfapi.dll"

echo "$output_file"
