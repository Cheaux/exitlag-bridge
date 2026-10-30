# ExitLag Bridge

An **unofficial** compatibility project with a Linux packet-steering backend and an experimental macOS launcher for the official ExitLag client,
so games running natively, under Proton, or in another Wine prefix can be routed
through ExitLag's relays on Linux.

> **Not affiliated with, endorsed by, or supported by ExitLag.**
> This project contains no ExitLag code and redistributes no ExitLag files.
> You need the official client and your own valid account and subscription.

ExitLag's Windows client normally intercepts game connections through a Windows
filtering driver. This bridge supplies the interception and steering layer on
Linux/macOS while keeping the official client for login, route selection and
relay transport.

## How traffic flows

```text
Game <-> our native bridge <-> official ExitLag client under Wine
                                  <-> ExitLag relay(s) <-> game server
```

For a recognized game connection, our bridge asks the official client's routing
callback where that connection should go. The proven path returns a local ExitLag
proxy endpoint. We steer the game's traffic into that proxy, and the official
client carries it through its selected relay route. Return traffic follows the
same chain back to the game.

The complete setup uses ExitLag's service and relays. This project implements the
native interception, process attribution and traffic steering; it does not
independently implement ExitLag's remote route API or relay protocol. A valid
official client, account and subscription are required.

Download experimental installers from [Releases](https://github.com/Cheaux/exitlag-bridge/releases)
and follow the [installation instructions](installers/README.md). The official
Windows installer is downloaded separately by each user. The Mac launcher opens
the official dashboard directly and keeps optional status in the menu bar.

## Games tested

| Game | Platform | Recorded result | Tested scope |
|---|---|---|---|
| World of Warcraft Classic Era | Linux | Working — maintainer verified | Real gameplay with the original Linux bridge. |
| World of Warcraft Classic Era | macOS / Apple Silicon | Working | Stable gameplay and bidirectional world-server traffic through ExitLag. |
| World of Warcraft Forever | macOS / Apple Silicon | Partial | Relayed character-selection connection; world entry was unavailable before release. |
| Minecraft Java Edition | Native Linux | Unstable | Traffic was relayed, but sessions disconnected; not considered working gameplay. |
| Dota 2 | Native Linux | Detection only | Game/UDP traffic recognized; relay steering was not established. |
| Tibia | Native Linux | Detection only | In-world flows recognized; no redirect was supplied in the recorded test. |
| Other games | Linux / macOS | Untested | Community reports and pull requests are welcome. |

These are the recorded results, not a guarantee for every OS, client build or
transport. The Minecraft, Dota 2 and Tibia entries describe earlier Linux
experiments; they have not been revalidated with the new packaged installer.
See [compatibility and evidence](docs/compatibility.md) for limits and use the
[game report template](.github/ISSUE_TEMPLATE/game-compatibility.yml) for new tests.

## macOS compatibility bridge

An experimental native Mac launcher and TCP routing backend now live in [`macos/`](macos/README.md). The launcher opens the official ExitLag dashboard directly and keeps optional routing status in the menu bar. Classic Era world routing and Forever character-selection routing have been validated. The current Mac adapter is WoW-specific; it does not implement UDP or IPv6 routing.

The release-preparation installers provide fresh setup on macOS and Linux. Users download the official Windows `.exe` from ExitLag, choose it in the bridge, and complete the vendor wizard themselves. Wine is downloaded separately from pinned upstream releases and checked by SHA-256. No proprietary client or account state is bundled. See [`installers/README.md`](installers/README.md) for packages, requirements, validation limits and removal.

## Does this include the commercial NetFilter SDK?

No. The bridge supplies its own `nfapi.dll` compatibility implementation and a
presence-only `nfextlag.sys` stub. The official client still calls that interface,
but packet routing is performed by our native backend. Linux's open-source
nftables/NFQUEUE is separate from the commercial SDK. Users obtain the official
ExitLag installer themselves. See `THIRD_PARTY.md` for interface provenance and
runtime licensing.

## Release status

This is an experimental release candidate. The source-only repository excludes
prior development history and private network traces. See
[compatibility and evidence](docs/compatibility.md) for user-verified Linux
WoW gameplay, observed Mac gameplay, automated installer coverage and untested
cases. Mac installers are unsigned and not notarized.

The interface provenance review remains open; see
[THIRD_PARTY.md](THIRD_PARTY.md) and
[the review record](docs/provenance-review.md). No statement here represents
vendor endorsement or legal approval.

## Requirements

| | |
|---|---|
| OS | Linux with `nftables` and NFQUEUE (`libmnl`) |
| Wine | verified against 11.17 Staging |
| Privileges | the backend needs `CAP_NET_ADMIN`; the runners use `sudo` |
| ExitLag | the official client, installed by you into a Wine prefix |
| Build | `clang-cl`, `lld-link`, `llvm-dlltool`, `llvm-readobj`, a C compiler, GNU make |

Developed and verified on Arch Linux (kernel 7.2, nftables 1.1.7) with Wine
11.17 Staging. Other distributions are untested. Other Wine versions may need the
crash-guard set re-checked — `tools/check-exitlag-abi.sh` tells you whether they
do.

## Setup

```sh
make -C backend            # the privileged helper
shim/nfapi/build.sh        # the nfapi compatibility shim

# install the shim over the client's own nfapi.dll
cp shim/nfapi/build/nfapi.dll /path/to/your/ExitLag/

tools/exitlag-doctor.sh    # check everything before running
```

`exitlag-doctor.sh` is the first thing to run when something does not work. Each
failure states what is wrong, why it matters, and what to do.

Wine also needs a few prefix-local fixes — a WMI class Wine does not implement,
an OpenGL rendering setting, and guards for imports Wine does not export. See
`docs/wine-setup.md`. All of them are reversible and none modifies your system
Wine installation.

## Running

```sh
tools/run-exitlag-production.sh                                   # start the client
# enable your game in ExitLag's UI as usual
sudo env TEST_DEST=any ENABLE_REDIRECT=1 backend/run-tcp-redirect.sh
```

Routing activates when ExitLag has an active route **and** the backend can
attribute a connection to that game's identity. Start the backend before the
game connects: an already-established socket cannot be moved.

Interrupt the runner to stop. It removes its nftables table on exit, including
on Ctrl-C or a crash, and traffic keeps flowing if the backend dies (`queue …
bypass` is a kernel-level fail-open, not application logic).

## Routing games outside ExitLag's own prefix

By default only games inside ExitLag's Wine prefix are recognised, because that
is all the client can see. To route anything else, map a real process to a game
identity ExitLag knows:

```sh
sudo env EXITLAG_IDENTITY_MAP=/path/to/games.map \
     TEST_DEST=any ENABLE_REDIRECT=1 backend/run-tcp-redirect.sh
```

One entry per line, `SOURCE<TAB>WINDOWS_PATH` — **a literal tab**, because paths
contain spaces:

```text
# native Linux game: the real executable
/opt/mygame/bin/game	C:\Games\Supported Game\game.exe

# another Wine prefix: the prefix, not the executable
wineprefix:/home/user/prefixes/mygame	C:\Games\Supported Game\Game.exe

# Proton: the compatdata prefix for that app id
wineprefix:/home/user/.local/share/Steam/steamapps/compatdata/12345/pfx	C:\Games\Supported Game\Game.exe
```

Matching is exact — no fuzzy or partial matching anywhere — and a malformed map
stops the backend from starting rather than loading half of it.

Two things to know:

- **Wine and Proton games need the `wineprefix:` form.** They do not own their
  own socket: when attribution happens it belongs to that prefix's `wineserver`,
  and every `wineserver` looks identical. The prefix is what distinguishes them.
  That makes it exactly right for Proton, where each title has its own prefix,
  and too coarse for one hand-made prefix shared by several games.
- **Loading a map makes matching strict for everything**, including games inside
  ExitLag's own prefix. Anything unmapped keeps its real identity and is not
  routed. Without this, any process could impersonate a game by putting the
  right string in its command line.

See `docs/process-identity-translation.md` and `backend/identity-map.example`.

## Security model

The backend runs as root, because NFQUEUE and nftables require it. Its control
socket is therefore treated as an attack surface:

- only **root or the account that owns the ExitLag process** may connect
- connections must additionally prove knowledge of a **per-session token**,
  generated fresh on every start, published `0600` to that user, compared in
  constant time, verified before any routing command is served, never logged,
  and removed on exit
- redirect targets are restricted to **loopback**; PID lookups are restricted to
  PIDs the backend itself reported
- all state lives in one dedicated `inet exitlag` nftables table, removed
  atomically on exit; a table it cannot prove it owns is never deleted

A process running as your user that can read your files can still impersonate
the shim. See `docs/security-review.md` and `docs/rpc-surface.md`.

## Known limitations

- ExitLag's **Redirection method must stay Automatic**; NDIS needs a Windows
  driver that does not exist here.
- **Route analysis protocol: prefer TCP.** ICMP works but many ISPs rate-limit
  it enough that ExitLag reports failure.
- Active **UDP rewriting is IPv4 only**.
- Whether ExitLag's UI *lists* an external process as a detected session is
  untested; routing works regardless.
- Not tested: physical link down/up, and sessions beyond about 25 minutes.

## Troubleshooting

Run `tools/exitlag-doctor.sh` first.

| Symptom | Likely cause |
|---|---|
| ExitLag crashes opening a settings page | crash guards missing, or a different Wine build — run `tools/check-exitlag-abi.sh` |
| Client runs, nothing is steered | backend not running, or started after the game connected |
| An external game is not routed | missing identity-map entry, or wrong source form — Wine/Proton need `wineprefix:` |
| Backend refuses to start | leftover table or lock from an unclean exit; the doctor says which |
| `REFUSED shim connection` | something other than your ExitLag tried to drive the backend |

Debug logging:

```sh
EXITLAG_NFAPI_TRACE=1 tools/run-exitlag-gameprefix.sh
```

Normal logs record startup, connections, routing decisions, identity
translation, warnings and cleanup. Both logs cap at 8 MB and keep one previous
generation. Account credentials are never logged.

## Tests

```sh
tests/run-all.sh unit          # no privileges, no ExitLag needed
sudo tests/run-all.sh all      # everything available here
```

Tests that need the official client **skip with a reason** rather than failing,
so a fresh checkout is not mistaken for a broken one. See `docs/testing.md` and
`docs/test-matrix.md`.

## Licensing

Our own code is licensed GPL-3.0-only. Third-party provenance and obligations,
including one component whose redistribution status is explicitly unresolved:
see `THIRD_PARTY.md`.
