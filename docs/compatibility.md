# Compatibility and evidence

An ExitLag dashboard entry does not prove traffic is being relayed. Keep user
reports, automated tests and observed game connections distinct.

| Platform / game | Status | Evidence and limits |
|---|---|---|
| Linux / WoW Classic Era | User-verified gameplay | Maintainer reports working gameplay with the original Linux bridge. This does not establish every distribution or the complete new graphical installer flow. |
| macOS Apple Silicon / WoW Classic Era | Verified gameplay | Actual bidirectional world-server traffic through ExitLag and stable gameplay. |
| macOS Apple Silicon / WoW Forever | Partial | Actual relayed character-selection connection. World entry was unavailable before game release. |
| Ubuntu 22.04 and 24.04 / packaged helper | Automated verification | Clean Wine/WMI setup, installed first-run UI, ownership checks, UID-scoped routing startup, expired/removed leases, parent-exit cleanup and account-preserving removal. CI uses no vendor client or account. |
| Other games, architectures and distributions | Untested | Reports and pull requests are welcome. A working dashboard alone is insufficient evidence. |

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
