# Compatibility and evidence

An ExitLag dashboard entry does not prove traffic is being relayed. Keep user
reports, automated tests and observed game connections distinct.

| Game | Platform | Recorded result | Tested scope |
|---|---|---|---|
| World of Warcraft Classic Era | Linux | Working — maintainer verified | Real gameplay with the original Linux bridge. |
| World of Warcraft Classic Era | macOS / Apple Silicon | Working | Stable gameplay and bidirectional world-server traffic through ExitLag. |
| World of Warcraft Forever | macOS / Apple Silicon | Partial | Relayed character-selection connection; world entry was unavailable before release. |
| Minecraft Java Edition | Native Linux | Unstable | Traffic was relayed, but sessions disconnected; not considered working gameplay. |
| Dota 2 | Native Linux | Detection only | Game/UDP traffic recognized; relay steering was not established. |
| Tibia | Native Linux | Detection only | In-world flows recognized; no redirect was supplied in the recorded test. |
| Other games | Linux / macOS | Untested | Community reports and pull requests are welcome. |

Automated packaging checks are separate from gameplay: Ubuntu 22.04 and 24.04
cover clean Wine/WMI setup, installed first-run UI, ownership checks, UID-scoped
routing startup, removed/expired leases, parent-exit cleanup and account-preserving
removal. CI uses no official client or account.

Minecraft, Dota 2 and Tibia results come from earlier native Linux experiments.
Their source reports establish the scope above, not current-version support.
The Minecraft tests carried real relay traffic and then disconnected; detection
or byte counters alone must not be relabelled as working gameplay.

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
