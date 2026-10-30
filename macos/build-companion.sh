#!/bin/bash
set -euo pipefail
here="$(cd -- "$(dirname -- "$0")" && pwd)"
zig="${ZIG:-zig}"
build="$here/build"
mkdir -p "$build"
cat > "$build/companion-kernel32.def" <<'DEF'
LIBRARY KERNEL32.dll
EXPORTS
 ReadFile
 HeapAlloc
 HeapFree
 GetProcessHeap
 CreateThread
 WaitForSingleObject
 GetCurrentProcessId
 GetEnvironmentVariableW
 CreateFileW
 WriteFile
 CloseHandle
 GetFileAttributesExW
 GetSystemTimeAsFileTime
 Sleep
 ExitProcess
DEF
"$zig" dlltool -m i386:x86-64 -d "$build/companion-kernel32.def" -l "$build/companion-kernel32.lib"
cat > "$build/socket-ws2_32.def" <<'DEF'
LIBRARY WS2_32.dll
EXPORTS
 WSAStartup
 socket
 bind
 listen
 accept
 connect
 getsockname
 setsockopt
 recv
 send
 shutdown
 closesocket
DEF
"$zig" dlltool -m i386:x86-64 -d "$build/socket-ws2_32.def" -l "$build/socket-ws2_32.lib"
"$zig" cc --target=x86_64-windows-msvc -c -O2 -fno-stack-protector \
  -fshort-wchar -fno-unwind-tables -fno-asynchronous-unwind-tables \
  "$here/wine-socket-companion.c" -o "$build/game-companion.obj"
"$zig" build-exe -target x86_64-windows-msvc -fentry=entry -fcompiler-rt \
  -fstrip --subsystem windows "$build/game-companion.obj" "$build/companion-kernel32.lib" "$build/socket-ws2_32.lib" \
  "-femit-bin=$build/WowClassic.exe"
