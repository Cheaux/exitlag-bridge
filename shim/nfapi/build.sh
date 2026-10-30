#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
build_dir="$script_dir/build"

command -v clang-cl >/dev/null
command -v lld-link >/dev/null
command -v llvm-dlltool >/dev/null

mkdir -p "$build_dir"

llvm-dlltool \
    -m i386:x86-64 \
    -d "$script_dir/kernel32.def" \
    -l "$build_dir/kernel32.lib"

llvm-dlltool \
    -m i386:x86-64 \
    -d "$script_dir/ws2_32.def" \
    -l "$build_dir/ws2_32.lib"

llvm-dlltool \
    -m i386:x86-64 \
    -d "$script_dir/iphlpapi.def" \
    -l "$build_dir/iphlpapi.lib"

clang-cl \
    /nologo \
    /c \
    /O2 \
    /GS- \
    /Gs9999999 \
    /GR- \
    /EHs-c- \
    /Zl \
    "$script_dir/nfapi_stub.cpp" \
    "/Fo$build_dir/nfapi_stub.obj"

lld-link \
    /dll \
    /entry:DllMain \
    /nodefaultlib \
    "/out:$build_dir/nfapi.dll" \
    "$build_dir/nfapi_stub.obj" \
    "$build_dir/kernel32.lib" \
    "$build_dir/ws2_32.lib" \
    "$build_dir/iphlpapi.lib"

echo "$build_dir/nfapi.dll"
