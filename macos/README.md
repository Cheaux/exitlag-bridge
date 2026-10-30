# ExitLag Bridge for macOS — experimental

An unofficial compatibility bridge for the official ExitLag Windows client, using Wine and a native macOS TCP routing helper. It requires the official client and a valid ExitLag account. It is not affiliated with or supported by ExitLag.

Opening **ExitLag Bridge.app** opens the actual ExitLag dashboard. The bridge runs in the menu bar; its status window stays hidden until **Show routing status** is selected. The launcher has an original icon and does not redistribute vendor branding assets.

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
python3 -m unittest -v test_bridge.py test_pf_plan.py test_session_control.py test_wine_socket.py test_recover.py test_auto_launcher.py
```

The current 32 local checks cover real loopback data transfer, the shim protocol, source-tuple preservation, authentication, private configuration checks, both game identities, companion restart handling, fallback, PF scope and recovery validation. They do not replace live game testing.

The command-line foreground launchers remain available for diagnostics; do not run them concurrently with the installed app. See the repository's Linux README for Linux installation and testing.

Licensing and the outstanding interface review are documented in `../THIRD_PARTY.md`. Wine-derived patches retain their LGPL notices. The official ExitLag installer and full Wine runtime are obtained separately.
