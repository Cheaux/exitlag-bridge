#!/bin/bash
# Official client only; existing account/subscription and vendor installer required.
set -euo pipefail
here="$(cd -- "$(dirname -- "$0")" && pwd)"
if [ "$#" -lt 1 ]; then
  printf 'Usage: client.sh install OFFICIAL_INSTALLER.exe | configure | run\n' >&2
  exit 2
fi
mode="$1"
state="$HOME/Library/Application Support/ExitLag Mac Bridge"
wine="/Applications/Wine Staging.app/Contents/Resources/wine/bin/wine"
if [ -x "$HOME/Library/Application Support/ExitLag Mac Bridge/runtime/bin/wine" ]; then
  wine="$HOME/Library/Application Support/ExitLag Mac Bridge/runtime/bin/wine"
fi
prefix="$state/prefix"
app="$prefix/drive_c/ExitLag"
export WINEPREFIX="$prefix"
export WINEDEBUG=-all
export MVK_CONFIG_LOG_LEVEL=0
unset QT_QUICK_BACKEND QT_OPENGL
export QSG_RHI_BACKEND=vulkan
export QSG_RENDER_LOOP=basic
export EXITLAG_MACOS_SAFE_QT_TIMERS="${EXITLAG_MACOS_SAFE_QT_TIMERS:-0}"
if [ ! -x "$wine" ]; then
  printf 'Wine runtime not found: %s\n' "$wine" >&2
  exit 1
fi
mkdir -p "$state"
if [ "$mode" = install ] || [ "$mode" = configure ]; then
 if [ "$mode" = install ]; then
  if [ "$#" -ne 2 ]; then
    printf 'Supply the official ExitLag installer as the second argument.\n' >&2
    exit 2
  fi
  installer="$2"
  if [ ! -f "$installer" ]; then
    printf 'Supply the official ExitLag installer as the second argument.\n' >&2
    exit 1
  fi
  printf 'Complete the vendor installer and review its terms yourself.\n'
  printf 'Use C:\\ExitLag as the destination. At the end, uncheck Launch ExitLag.\n'
  "$wine" "$installer" '/DIR=C:\ExitLag' /NORESTART
 elif [ "$#" -ne 1 ]; then
  printf 'Usage: client.sh configure\n' >&2
  exit 2
 fi
  if [ ! -f "$app/ExitLag.exe" ]; then
    printf 'ExitLag.exe was not installed at C:\\ExitLag; setup has stopped.\n' >&2
    exit 1
  fi
  /usr/bin/python3 "$here/pe_check.py" "$here/build/nfapi.dll" "$app"
  if [ -f "$app/nfapi.dll" ] && [ ! -f "$app/nfapi.vendor.dll" ]; then
    cp -p "$app/nfapi.dll" "$app/nfapi.vendor.dll"
  fi
  cp "$here/build/nfapi.dll" "$app/nfapi.dll"
  mkdir -p "$prefix/drive_c/windows/system32/drivers"
  cp "$here/build/nfextlag.sys" "$prefix/drive_c/windows/system32/drivers/nfextlag.sys"
  "$wine" sc.exe create nfextlag type= kernel start= demand error= normal 'binPath=system32\drivers\nfextlag.sys' || true
  "$wine" reg.exe add 'HKCU\Software\Wine\DllOverrides' /v nfapi /t REG_SZ /d native,builtin /f
  printf '\nShim installed in the dedicated prefix. Live routing is not enabled.\n'
elif [ "$mode" = run ]; then
  test -f "$app/ExitLag.exe"
  token="/var/run/exitlag-macos-$(id -u)/session.token"
  export EXITLAG_NETD=127.0.0.1:51900
  export EXITLAG_NETD_TOKEN="Z:$token"
  if [ ! -r "$token" ]; then
    printf 'No routing session is active; opening the client without optimization.\n'
  fi
  cd "$app"
  exec "$wine" "$app/ExitLag.exe"
else
  printf 'Usage: client.sh install OFFICIAL_INSTALLER.exe | configure | run\n' >&2
  exit 1
fi
