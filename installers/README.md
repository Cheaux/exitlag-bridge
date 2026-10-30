# Installer release preparation

The bridge is GPL-3.0-only. ExitLag is downloaded separately from
[its official download page](https://www.exitlag.com/download). We do not ship,
mirror, automatically download, or silently accept terms for its Windows client.

Both desktop setup flows open that page, accept a local `.exe`, prepare a
private Wine runtime, and run the vendor wizard interactively. Review its terms,
use the proposed `C:\ExitLag` destination, and leave **Launch ExitLag** unchecked.
Setup stops on vendor failure, missing files, or an unsupported `nfapi` interface.
Sign in directly in the official client after setup completes.
The vendor wizard may warn that the Windows NDIS driver could not install
(`0x80004001`). Choose **OK** to continue this specific warning: native bridge
routing replaces the Windows driver. Other installer failures are not ignored.

## macOS

Install **ExitLag Mac Setup.pkg**, then open **ExitLag Bridge** in Applications.
First run shows setup; subsequent launches open the real ExitLag dashboard.
The package includes an immutable standalone Python interpreter for the routing
helper, so end users do not need Command Line Tools or Homebrew. Wine downloads
and the vendor wizard run as the ordinary user. Apple Silicon also needs Rosetta;
install it through Apple's own system prompt and review Apple's terms yourself.

The downloadable Mac beta is arm64 for Apple Silicon. It supports one installation user, macOS 15+, and the standard
`/Applications/World of Warcraft` paths for Classic Era and Forever. Current
live-game evidence is Apple Silicon/macOS 27.2. Intel builds, reboot/sleep
recovery, and Forever world entry still need validation. Forever has only been
verified through character selection.

**ExitLag Bridge Uninstall.pkg** removes the program and administrator helper,
after quitting the bridge. It preserves the private Wine prefix and account data.
The app has an ad-hoc signature. The `.pkg` installers are unsigned, and the
build is not notarized. Do not disable system-wide security settings to install it.

Build with Xcode Command Line Tools, Python 3.10+ and standard build tools:

```sh
python3 installers/build_runtime.py payloads --output installers/payloads
bash macos/app/build.sh
```

The builder pins and verifies Wine, Python and Zig downloads. Generated artifacts
are ignored by Git. The builder's own Python version may need the standard
`xz` support for tar archives; installed users do not build components.

## Linux

The packaged beta supports **x86_64**. For Ubuntu/Debian, install the `.deb` with
your graphical package manager or `sudo apt install ./exitlag-bridge_0.4.0_amd64.deb`.
This installs desktop integration and declares system runtime dependencies.
Open **ExitLag Bridge** from the applications menu as your normal user.

A portable `.tar.gz` contains `install.sh`, `uninstall.sh` and the program tree.
Install the listed dependencies using your distribution's package manager, then
run `sudo bash ./install.sh`. It installs under `/usr/local/lib/exitlag-bridge`.
On distributions other than Ubuntu 22.04/24.04 the portable package is untested.
`nftables`, NFQUEUE support, libmnl, Python 3.10+ with Tk, polkit with a desktop
agent, curl, certificates and the normal Wine graphics/audio libraries are needed.
A Wayland session needs XWayland for the current Wine client.

Use **Configure games** to select the actual native executable or the game's
Wine/Proton prefix and enter the Windows executable identity recognized by
ExitLag. A prefix mapping covers the entire prefix, so separate game prefixes
are preferable. Unmapped processes retain their normal identity. Apply the game
in ExitLag's dashboard before establishing new game connections.

Launching routing uses a graphical polkit administrator prompt. The installed
root-owned supervisor only runs the installed backend, snapshots data-only game
mappings, scopes nftables rules to the authorized UID, and stops when the launcher
exits or its six-second lease expires. Wine and login always run unprivileged.
Quitting the bridge restores its firewall surface; reconnect existing games.

Removing either package preserves data under the user's XDG data directory.
The shared setup CLI can remove downloaded components or explicitly purge a
managed prefix, with a separate purge confirmation. It refuses unrelated,
unmarked directories. Legacy developer setups are preserved rather than
silently migrated.

Build on Ubuntu/Debian with the dependencies in the validation workflow:

```sh
make -C backend
python3 installers/build_runtime.py payloads --output installers/payloads
python3 installers/linux/build.py --output build/releases
```

## Verification and distribution

`tests/test_installer.py` checks download verification, archive extraction,
private ownership, interrupted setup, interactive vendor handoff and removal.
`tests/installer_fresh_runtime.py` boots the actual pinned Wine distribution in a
fresh temporary prefix and verifies the patched WMI class, repeat setup and
preserved data. No real account or proprietary executable is used in CI.
Desktop/supervisor checks are in `tests/test_linux_desktop.py`.

Installer checks do not prove routing for every game. Reports must distinguish
world gameplay, character selection, synthetic tests and untested games. Submit
compatibility reports using the issue template; redact account information and
local paths. Do not upload prefixes or raw Wine logs.

Release assets must include the matching GPL bridge source and the generated
`exitlag-bridge-wine-source.tar.gz` corresponding to the patched Wine DLL.
Runtime licenses retain their upstream terms; see `THIRD_PARTY.md` and `LICENSES`.
This repository starts from a source-only snapshot. Runtime data, prior local
histories, build caches and settings are excluded. `tools/audit-source.py` checks
committed source and reachable author/committer emails before release. It detects
specific privacy hazards and does not provide legal or security clearance.
