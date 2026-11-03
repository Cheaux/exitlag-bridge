# ExitLag Bridge for macOS — experimental

An unofficial compatibility bridge for the official ExitLag Windows client, using Wine and a native macOS TCP routing helper. It requires the official client and a valid ExitLag account. It is not affiliated with or supported by ExitLag.

Opening **ExitLag Bridge.app** opens the actual ExitLag dashboard. The bridge runs in the menu bar; its status window stays hidden until **Show routing status** is selected. The launcher has an original icon and does not redistribute vendor branding assets.

The official client runs inside a generated, private `ExitLag.app` wrapper, so
macOS labels its Dock icon **ExitLag**. Reopening that app or choosing **Show
ExitLag dashboard** in the bridge menu restores its minimized window through
macOS. Qt uses the client's bundled software OpenGL renderer to avoid the black
Vulkan surface seen after Dock restore. This affects the dashboard, not the game.
The wrapper's transparent red-and-white icon is extracted locally from the
installed official client's dashboard assets. It has transparent padding to
keep its Dock size balanced; older client layouts fall back to the Windows
executable icon. No vendor icon is distributed in the bridge.
The scoped Wine Dock correction draws that image directly, preserving its
transparency when macOS would otherwise add a white backing to the app icon.
A minimal Dock tile plug-in draws the same locally extracted image while the
app is closed. The running-client correction alone cannot control the pinned,
inactive tile. The plug-in reads only the generated wrapper's marker and icon.
The Finder icon receives the same transparent image after code signing.
The visible wrapper is a foreground app so macOS can animate its Dock launch;
the separate menu-bar bridge remains an accessory app.

The launcher enables the existing Qt timer compatibility fallback. Qt uses its
Windows message timer implementation when the precision timer is unavailable;
this avoids the stalled login and unresponsive route controls observed with
the precision timer under Wine. The foreground launcher uses the same default.
Before opening the dashboard or starting a game companion, the launcher waits
for a built-in Wine command to complete the prefix's normal service startup.
This lets the official client restore its own saved session on a cold start;
the bridge never reads or re-enters account credentials.

The native Wine driver patch in
[`wine-patches/0001-preserve-exitlag-cropped-window-frame.patch`](wine-patches/0001-preserve-exitlag-cropped-window-frame.patch)
retains the masked, cropped window image during the Dock animation and fits the
temporary image to the viewport on restore. Capturing Wine's whole backing
buffer would include padding and briefly show a smaller picture. The installer
bundles the rebuilt driver and applies it automatically when the private runtime
matches the pinned build, including upgrades from an earlier recorded correction.
The original module is backed up. Unrecognized Wine modifications are preserved.
Live window testing used Wine 11.17 and ExitLag 5.23.3.

## Compatibility

| Game | Evidence | Scope |
|---|---|---|
| World of Warcraft Classic Era | Verified | In-world TCP relay traffic and stable gameplay |
| World of Warcraft Forever | Partial | Character-selection TCP relay traffic; world entry unavailable before release |
| Other games | Untested / not implemented | The current Mac adapter supports these two exact game identities |

The Mac backend currently handles IPv4 TCP only. UDP and IPv6 game redirection are not implemented. Native process attribution uses exact executable paths; a matching game label in the dashboard alone does not prove routing.

The downloadable Mac package is an arm64 build for Apple Silicon. Validation was performed with macOS 27.2 and Wine 11.17 Staging. Other hardware, OS versions and long-duration sessions need testing. Full Mac reboot and sleep/wake recovery have not been exercised with the launcher.

## Current installation scope

The package installs the native launcher, a fixed-scope administrator helper, and its own standalone Python interpreter. The first-run screen links to ExitLag's official download page and accepts a Windows `.exe` downloaded by the user. It prepares a verified private Wine runtime, checks the WMI patch in a fresh prefix and runs the official wizard interactively. The user's ExitLag account and settings remain local. See [`../installers/README.md`](../installers/README.md) for build and validation details.

The installer selects the signed-in installation user's UID. The helper derives that user's home from the operating system account database; no developer username or fixed UID is required.

1. Install **ExitLag Mac Setup.pkg** using macOS Installer.
2. Open **ExitLag Bridge** from Applications or Spotlight. Complete first-run setup using your own official `.exe`; review the vendor terms and leave Launch ExitLag unchecked. Subsequent launches need no Terminal or daily password.
3. Select the game and apply its routes in the official ExitLag dashboard, then launch the game.
4. Use the bridge's menu-bar icon to inspect routing status or quit.

Quitting and reopening either game automatically replaces its own socket companion. Closing the status window leaves routing available. Quitting the bridge stops the official client and routing; existing routed connections may need to reconnect.

## Architecture and privilege boundary

The official client runs in a dedicated Wine prefix. A separate ordinary-user socket companion is started only while the corresponding actual native game is present. Era and Forever use separate authenticated loopback services. Every new game flow loads the current private companion configuration, which supports game restarts without restarting the bridge.

Only the installed native routing service runs as root. Its code is root-owned, Python starts in isolated mode, and its API accepts only an enable/disable lease from the installation user. It cannot execute user-selected scripts or run Wine as root. The lease expires after six seconds if the launcher stops responding.

PF rules affect only the installation user's IPv4 TCP3724 connections to the configured Blizzard server ranges. The service owns a dedicated child anchor under `com.apple/*`; it does not reload global PF configuration or flush another application's rules. It recovers its own fixed anchor after a crash. Unknown processes or unavailable adapters fall back to the original destination.

## Privacy

Account data, the Wine prefix, authentication tokens, runtime logs, screenshots, game account files and diagnostic dumps are local and excluded from source control. No account credentials or game payloads are logged by the bridge. The official client still connects to ExitLag's services normally. Review and redact diagnostics before submitting a compatibility report.

## Build and validation

From the repository root, with the matching Zig toolchain available:

```sh
python3 installers/build_runtime.py payloads --output installers/payloads
bash macos/app/build.sh
```

The app build creates a signed-local app and installer in `macos/app/dist`. It does not notarize a public release. Generated binaries, package staging and runtime state are ignored by Git.

```sh
cd macos
python3 -m unittest -v test_bridge.py test_pf_plan.py test_session_control.py test_wine_socket.py test_recover.py test_auto_launcher.py test_client_app.py test_window_driver.py
```

The local checks cover real loopback data transfer, the shim protocol, source-tuple preservation, authentication, private configuration checks, both game identities, companion restart handling, fallback, PF scope, recovery, the generated client app, and atomic window-driver upgrades. They do not replace live game testing.

`python3 -m unittest -v test_client_app.py` checks the generated app's identity,
private configuration, preservation of unrelated apps, and refusal of links that
could overwrite unrelated files. These checks do not replace a live dashboard
minimize/restore test on macOS.

The native rendering regression check uses hidden AppKit windows and the actual
patched Wine drawing methods. It checks a padded backing buffer, full-size
restore, input transparency, and removal of the temporary image:

```sh
python3 test_native_window_snapshot.py \
  --wine-source ../build/installer/wine-source/wine-wine-11.17
python3 test_native_dock_icon.py \
  --wine-source ../build/installer/wine-source/wine-wine-11.17
```

It requires macOS, Xcode command-line tools, and the unmodified pinned Wine
source already downloaded by the payload build. It does not use an ExitLag
account, run Wine, or capture the desktop.
The Dock check invokes the actual view drawing callback and checks visible
pixels as well as transparent padding, so an empty tile cannot pass. It also
checks that Wine applications without the ExitLag icon setting keep their Dock
behavior.

The command-line foreground launchers remain available for diagnostics; do not run them concurrently with the installed app. See the repository's Linux README for Linux installation and testing.

Licensing and the outstanding interface review are documented in `../THIRD_PARTY.md`. Wine-derived patches retain their LGPL notices. The official ExitLag installer and full Wine runtime are obtained separately.
