#!/usr/bin/env bash
# Build the presence-only nfextlag.sys stand-in as a 64-bit native PE image.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
build_dir="$script_dir/build"

command -v clang-cl >/dev/null
command -v lld-link >/dev/null
command -v llvm-rc >/dev/null

mkdir -p "$build_dir"

llvm-rc /nologo "/fo$build_dir/nfextlag.res" "$script_dir/nfextlag.rc"

clang-cl /nologo /c /O2 /GS- /Gs9999999 /Zl \
    "$script_dir/nfextlag_stub.c" "/Fo$build_dir/nfextlag_stub.obj"

lld-link /driver /subsystem:native /entry:DriverEntry /nodefaultlib /machine:x64 \
    "/out:$build_dir/nfextlag.sys" \
    "$build_dir/nfextlag_stub.obj" "$build_dir/nfextlag.res"

sha256sum "$build_dir/nfextlag.sys"
