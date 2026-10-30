#!/bin/bash
set -euo pipefail
here="$(cd -- "$(dirname -- "$0")" && pwd)"
repo="$(cd -- "$here/.." && pwd)"
zig="$(printenv ZIG || true)"
if [ -z "$zig" ]; then zig=zig; fi
build="$here/build"
src="$repo/shim/nfapi"
mkdir -p "$build"
for library in kernel32 ws2_32 iphlpapi; do
  "$zig" dlltool -m i386:x86-64 -d "$src/$library.def" -l "$build/$library.lib"
done
printf 'LIBRARY KERNEL32.dll\nEXPORTS\n SetLastError\n OutputDebugStringA\n' > "$build/driver-kernel32.def"
"$zig" dlltool -m i386:x86-64 -d "$build/driver-kernel32.def" -l "$build/driver-kernel32.lib"
"$zig" c++ --target=x86_64-windows-msvc -c -O2 -fno-stack-protector \
  -fno-exceptions -fno-rtti -fno-unwind-tables -fno-asynchronous-unwind-tables \
  "$here/driver-registration.cpp" -o "$build/driver-registration.obj"
"$zig" c++ --target=x86_64-windows-msvc -c -O2 -fno-stack-protector \
  -fno-exceptions -fno-rtti -fno-unwind-tables -fno-asynchronous-unwind-tables \
  -fms-extensions -fshort-wchar "$src/nfapi_stub.cpp" -o "$build/nfapi_stub.obj"
"$zig" build-lib -target x86_64-windows-msvc -dynamic -fentry=MacDllMain \
  -fcompiler-rt -fstrip --subsystem windows --name nfapi \
  "$build/nfapi_stub.obj" "$build/driver-registration.obj" \
  "$build/driver-kernel32.lib" "$build/kernel32.lib" "$build/ws2_32.lib" \
  "$build/iphlpapi.lib" "-femit-bin=$build/nfapi.dll"
printf '%s\n' "$build/nfapi.dll"
driver="$repo/shim/nfextlag"
"$zig" rc "/fo$build/nfextlag.res" "$driver/nfextlag.rc"
"$zig" cc --target=x86_64-windows-msvc -c -O2 -fno-stack-protector \
  "$driver/nfextlag_stub.c" -o "$build/nfextlag.obj"
"$zig" build-lib -target x86_64-windows-msvc -dynamic -fentry=DriverEntry \
  -fno-compiler-rt -fstrip --subsystem native --name nfextlag \
  "$build/nfextlag.obj" "$build/nfextlag.res" "-femit-bin=$build/nfextlag.sys"
printf '%s\n' "$build/nfextlag.sys"
