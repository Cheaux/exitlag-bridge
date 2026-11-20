# Compatibility and evidence

An ExitLag dashboard entry does not prove traffic is being relayed. Keep user
reports, automated tests and observed game connections distinct.

Client versions are a separate compatibility boundary. The
[5.24.0 review](exitlag-updates.md#client-5240-review--2026-11-02) records static
import checks and fresh-prefix configuration on macOS. Login and live routing
with 5.24.0 remain untested; the earlier game results below are not new-version
certification.

| Game | Platform | Recorded result | Tested scope |
|---|---|---|---|
| World of Warcraft Classic Era | **Arch Linux, x86_64** | Working — maintainer verified | Real gameplay on Arch Linux with the original bridge. |
| World of Warcraft Classic Era | macOS / Apple Silicon | Working | Stable gameplay and bidirectional world-server traffic through ExitLag. |
| World of Warcraft Forever | macOS / Apple Silicon | Partial | Relayed character-selection connection; world entry was unavailable before release. |
| Minecraft Java Edition | Native Linux | Unstable | Traffic was relayed, but sessions disconnected; not considered working gameplay. |
| Dota 2 | Native Linux | Detection only | Game/UDP traffic recognized; relay steering was not established. |
| Tibia | Native Linux | Detection only | In-world flows recognized; no redirect was supplied in the recorded test. |
| PEAK | Arch Linux / Proton Experimental | Community report — relay unverified | AlexYT18 reported detection, UDP callbacks and lobby creation in his modified fork on November 5, 2026. The observed game-server flow passed through unchanged. [Report](https://github.com/AlexYT18/ExitLag-bridge/pull/1#issuecomment-6654023267). |
| Other games | Linux / macOS | Untested | Community reports and pull requests are welcome. |

**Arch Linux is the maintainer's verified Linux gameplay platform.** The
[Arch setup guide](arch-linux.md) uses the shared Linux portable installer.
Automated packaging checks are separate from gameplay: Ubuntu 22.04 and 24.04
cover clean Wine/WMI setup, installed first-run UI, ownership checks, UID-scoped
routing startup, removed/expired leases, parent-exit cleanup and account-preserving
removal. CI uses no official client or account.

The same Linux portable archive now also passes these checks on both Ubuntu
versions and in Arch userspace in a disposable Docker container. The Arch
container uses an Ubuntu CI host kernel; it is not a native Arch kernel or live
gameplay test. [Portable installer checks](https://github.com/Cheaux/exitlag-bridge/actions/runs/41244802664).

Minecraft, Dota 2 and Tibia results come from earlier native Linux experiments.
Their source reports establish the scope above, not current-version support.
The Minecraft tests carried real relay traffic and then disconnected; detection
or byte counters alone must not be relabelled as working gameplay.

## Community report — PEAK on AlexYT18’s fork

On November 5, 2026, AlexYT18 reported a local test on Arch Linux x86_64,
kernel 6.18.60-1-lts, Steam / Proton Experimental and ExitLag 5.24.0. The test
used his modified fork, build `0.4.0+arch.1` at
`f1a957cbff08d1e60969dd8425e57318c8c2088c`, with experimental UDP enabled.

He reported detection of PEAK (Steam AppID `3527290`), real UDP callbacks,
main-menu access and network-lobby creation. His inspected log showed an applied
`nf_udpPostSend` for one UDP flow. The game-server flow on UDP/5055 passed through
unchanged in both directions. Bidirectional gameplay through ExitLag’s relay,
co-op play, long-session stability and reconnect remain unverified. This report
applies to Alex’s fork and has not been reproduced with this project’s release.
[Read Alex’s original report](https://github.com/AlexYT18/ExitLag-bridge/pull/1#issuecomment-6654023267).

## Platform scope and further testing

Mac live-game testing used Apple Silicon, macOS 27.2 and Wine 11.17 Staging.
The current Mac adapter recognizes Classic Era and Forever at the standard WoW
installation paths and supports IPv4 TCP only. Linux uses exact executable or
Wine-prefix mappings; UDP/IPv6 experiments do not imply support for every game.

The first public version should be labelled experimental. Reboot/sleep recovery,
Intel Macs, a second Mac and a complete fresh graphical Linux install/login/game
session remain useful follow-up tests. They are not claimed as completed.

Use the game-compatibility issue template for new reports. Include the exact game
build, OS/architecture, client/Wine versions, tested stage, observed relay traffic
and cleanup behavior. Remove personal information before attaching diagnostics.
