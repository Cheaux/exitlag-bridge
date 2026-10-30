#!/bin/bash
set -euo pipefail
here="$(cd -- "$(dirname -- "$0")" && pwd)"
source="$here/.."
out="${EXITLAG_MAC_OUTPUT:-$here/dist}"
mkdir -p "$out"
app_identity="${EXITLAG_APP_SIGN_IDENTITY:-}"
installer_identity="${EXITLAG_INSTALLER_SIGN_IDENTITY:-}"
notary_profile="${EXITLAG_NOTARY_PROFILE:-}"
if { [ -n "$app_identity" ] && [ -z "$installer_identity" ]; } || { [ -z "$app_identity" ] && [ -n "$installer_identity" ]; }; then
  echo 'Provide both Developer ID Application and Installer identities, or neither.' >&2; exit 1
fi
if [ -n "$notary_profile" ] && [ -z "$app_identity" ]; then
  echo 'Notarization requires Developer ID signing identities.' >&2; exit 1
fi
build_package() {
  if [ -n "$installer_identity" ]; then
    /usr/bin/pkgbuild --sign "$installer_identity" "$@"
  else
    /usr/bin/pkgbuild "$@"
  fi
}
app="$here/build/ExitLag Bridge.app"
payload="$here/package/payload"
root="$payload/Library/PrivilegedHelperTools/local.cheaux.ExitLagRouting"
# Never package files left over from an earlier build.
rm -rf "$here/build" "$here/package"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources/bridge/build" "$payload/Applications" "$root" "$payload/Library/LaunchDaemons" "$here/package/scripts"
python3 "$source/../installers/build_runtime.py" python --arch "$(uname -m)" --output "$root/python"
/usr/bin/xcrun swiftc -target "$(uname -m)-apple-macos15.0" -O -parse-as-library "$here/ExitLag.swift" -o "$app/Contents/MacOS/ExitLag"
/usr/bin/xcrun swift "$here/make-icon.swift" "$here/build/BridgeIcon.iconset"
/usr/bin/iconutil -c icns "$here/build/BridgeIcon.iconset" -o "$app/Contents/Resources/BridgeIcon.icns"
for file in entry.py setup_worker.py auto_agent.py auto_common.py bridge.py wine_socket.py companion.py games.auto.map; do
  /bin/cp "$source/$file" "$app/Contents/Resources/bridge/$file"
done
/bin/cp "$source/../installers/payloads/WowClassic.exe" "$app/Contents/Resources/bridge/build/WowClassic.exe"
/usr/bin/ditto "$source/../installers/bridge_setup" "$app/Contents/Resources/bridge/bridge_setup"
/usr/bin/ditto "$source/../installers/payloads" "$app/Contents/Resources/bridge/payloads"
/usr/bin/ditto "$source/../LICENSES" "$app/Contents/Resources/LICENSES"
/bin/cp "$source/../LICENSE" "$source/../THIRD_PARTY.md" "$app/Contents/Resources/"
mkdir -p "$app/Contents/Resources/docs"
/bin/cp "$source/../docs/provenance-review.md" "$app/Contents/Resources/docs/"
/usr/bin/find "$app" -name __pycache__ -type d -exec /bin/rm -rf '{}' +
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleExecutable</key><string>ExitLag</string>
<key>CFBundleIdentifier</key><string>local.cheaux.ExitLagMac</string>
<key>CFBundleName</key><string>ExitLag Bridge</string>
<key>CFBundleDisplayName</key><string>ExitLag Bridge</string>
<key>CFBundleIconFile</key><string>BridgeIcon</string>
<key>CFBundleVersion</key><string>4</string>
<key>CFBundleShortVersionString</key><string>0.4</string>
<key>LSMinimumSystemVersion</key><string>15.0</string>
<key>LSMultipleInstancesProhibited</key><true/>
<key>LSUIElement</key><true/>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
/usr/bin/codesign --force --sign - "$app"
/usr/bin/ditto "$app" "$payload/Applications/ExitLag Bridge.app"
for file in entry.py auto_service.py auto_common.py bridge.py wine_socket.py pf_plan.py games.auto.map; do
  /bin/cp "$source/$file" "$root/$file"
  /bin/chmod 644 "$root/$file"
done
cat > "$payload/Library/LaunchDaemons/local.cheaux.ExitLagRouting.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>local.cheaux.ExitLagRouting</string>
<key>ProgramArguments</key><array><string>/Library/PrivilegedHelperTools/local.cheaux.ExitLagRouting/python/bin/python3</string><string>-I</string><string>/Library/PrivilegedHelperTools/local.cheaux.ExitLagRouting/entry.py</string><string>--uid</string><string>INSTALL_UID</string></array>
<key>WorkingDirectory</key><string>/</string>
<key>RunAtLoad</key><true/>
<key>KeepAlive</key><true/>
<key>ThrottleInterval</key><integer>5</integer>
<key>ExitTimeOut</key><integer>20</integer>
<key>StandardOutPath</key><string>/var/log/cheaux-exitlag.log</string>
<key>StandardErrorPath</key><string>/var/log/cheaux-exitlag.log</string>
</dict></plist>
PLIST
cat > "$here/package/scripts/preinstall" <<'PRE'
#!/bin/bash
set -euo pipefail
if [ "${3:-/}" != / ]; then echo 'Install on the startup volume.' >&2; exit 1; fi
install_uid="$(/usr/bin/stat -f %u /dev/console)"
if ! [[ "$install_uid" =~ ^[0-9]+$ ]] || [ "$install_uid" -lt 500 ]; then
  echo 'Install while signed into the Mac as the game user.' >&2; exit 1
fi
for app in '/Applications/ExitLag.app' '/Applications/ExitLag Bridge.app'; do
  if [ -e "$app" ]; then
    id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist")"
    if [ "$id" != local.cheaux.ExitLagMac ]; then echo 'An unrelated app already exists at the installation path.' >&2; exit 1; fi
  fi
done
if [ -f /Library/LaunchDaemons/local.cheaux.ExitLagRouting.plist ]; then
  /bin/launchctl bootout system /Library/LaunchDaemons/local.cheaux.ExitLagRouting.plist || true
fi
PRE
cat > "$here/package/scripts/postinstall" <<'POST'
#!/bin/bash
set -euo pipefail
root=/Library/PrivilegedHelperTools/local.cheaux.ExitLagRouting
plist=/Library/LaunchDaemons/local.cheaux.ExitLagRouting.plist
install_uid="$(/usr/bin/stat -f %u /dev/console)"
if ! [[ "$install_uid" =~ ^[0-9]+$ ]] || [ "$install_uid" -lt 500 ]; then
  echo 'Install while signed into the Mac as the game user.' >&2; exit 1
fi
/usr/libexec/PlistBuddy -c "Set :ProgramArguments:4 $install_uid" "$plist"
/usr/sbin/chown -R root:wheel "$root"
/bin/chmod 755 "$root"
/bin/chmod -R go-w "$root"
/usr/sbin/chown root:wheel "$plist"
/bin/chmod 644 "$plist"
/usr/bin/touch /var/log/cheaux-exitlag.log
/usr/sbin/chown root:wheel /var/log/cheaux-exitlag.log
/bin/chmod 600 /var/log/cheaux-exitlag.log
/usr/bin/plutil -lint "$plist"
/bin/launchctl enable system/local.cheaux.ExitLagRouting
/bin/launchctl bootstrap system "$plist"
# Remove only the prior version of our launcher; all account data is outside it.
if [ -d /Applications/ExitLag.app ]; then
  old_id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' /Applications/ExitLag.app/Contents/Info.plist)"
  if [ "$old_id" = local.cheaux.ExitLagMac ]; then /bin/rm -rf /Applications/ExitLag.app; fi
fi
POST
/bin/chmod 755 "$here/package/scripts/preinstall" "$here/package/scripts/postinstall"
if [ -n "$app_identity" ]; then
  python3 "$here/sign_payload.py" "$payload" --identity "$app_identity"
fi
/usr/bin/pkgbuild --analyze --root "$payload" "$here/package/components.plist"
/usr/libexec/PlistBuddy -c 'Add :0:BundleIsRelocatable bool false' "$here/package/components.plist"
/usr/libexec/PlistBuddy -c 'Set :0:BundleIsVersionChecked false' "$here/package/components.plist"
build_package --root "$payload" --component-plist "$here/package/components.plist" --scripts "$here/package/scripts" --identifier local.cheaux.ExitLagMac.Setup --version 0.4 --install-location / "$out/ExitLag Mac Setup.pkg"
/usr/bin/ditto "$payload/Applications/ExitLag Bridge.app" "$out/ExitLag Bridge.app"
mkdir -p "$here/package/uninstall"
cat > "$here/package/uninstall/preinstall" <<'UNINSTALL'
#!/bin/bash
set -euo pipefail
if /usr/bin/pgrep -f '^/Applications/ExitLag Bridge.app/Contents/MacOS/ExitLag' >/dev/null; then
  echo 'Quit ExitLag Bridge before uninstalling.' >&2; exit 1
fi
app='/Applications/ExitLag Bridge.app'
plist=/Library/LaunchDaemons/local.cheaux.ExitLagRouting.plist
root=/Library/PrivilegedHelperTools/local.cheaux.ExitLagRouting
if [ -d "$app" ]; then
  id="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$app/Contents/Info.plist")"
  [ "$id" = local.cheaux.ExitLagMac ] || { echo 'The application is unrelated; refusing removal.' >&2; exit 1; }
fi
if [ -f "$plist" ]; then
  label="$(/usr/libexec/PlistBuddy -c 'Print :Label' "$plist")"
  [ "$label" = local.cheaux.ExitLagRouting ] || exit 1
  /bin/launchctl bootout system "$plist" || true
fi
# The daemon cleans its own PF anchor as it exits. Never flush global rules.
/bin/rm -f "$plist"
if [ -d "$root" ] && [ ! -L "$root" ]; then /bin/rm -rf "$root"; fi
if [ -d "$app" ]; then /bin/rm -rf "$app"; fi
echo 'ExitLag Bridge was removed. Your account data and private Wine environment were preserved.'
UNINSTALL
chmod 755 "$here/package/uninstall/preinstall"
build_package --nopayload --scripts "$here/package/uninstall" --identifier local.cheaux.ExitLagMac.Uninstall --version 0.4 "$out/ExitLag Bridge Uninstall.pkg"

if [ -n "$notary_profile" ]; then
  python3 "$here/notarize.py" --profile "$notary_profile" "$out/ExitLag Mac Setup.pkg" "$out/ExitLag Bridge Uninstall.pkg"
fi
