# Release-readiness test matrix

Which tests need nothing, which need root, and which need the official ExitLag
client or a real game. Tests requiring an account or a game are separated so a
contributor without either can still verify most of the project.

## Automated — no account, no game

Run with `tests/run-all.sh unit`. Nothing here needs privileges, ExitLag, or a
network game.

| Test | What it proves | Requires |
|---|---|---|
| shim builds | the nfapi shim compiles and links | toolchain |
| helper builds | `exitlag-netd` compiles warning-free | C compiler, libmnl |
| identity map pos/neg | an exact mapping translates; an unmapped `.exe` argv spoof is refused | — |
| RPC surface scope | an authenticated peer cannot resolve unreported PIDs, redirect off-box, or crash the helper with malformed privileged requests | — |
| crash-guard smoketest | the Wine missing-import guards hold; without them this exits non-zero | Wine |

## Automated — needs root, no account

`sudo tests/run-all.sh system`.

| Test | What it proves |
|---|---|
| IPC authentication | wrong uid; correct uid with wrong/all-zero token; correct token; wrong protocol version; token removed on exit; rotated on restart; replayed previous-session token refused |
| malformed IPC | ten malformed-handshake cases, a routing message sent before authenticating, malformed traffic from an authenticated peer, liveness afterwards, and that the token never reaches the log |
| heartbeat / reconnect | a connected-but-silent endpoint times out and the shim reconnects to a fresh helper |
| orphan cleanup | a SIGKILLed runner's helper and table are recovered on the next start |
| ownership-safe cleanup | a table with no verifiable ownership is refused, not deleted; SIGTERM and SIGINT tear down immediately |
| fail-open | traffic flows with rules installed and no helper, and survives a helper SIGKILL |
| repeated cycles | five start/stop cycles leave no table and no helper |
| unreachable destination | a dead destination neither kills nor wedges the helper |

## Automated — needs the official client installed

Skipped with a reason when it is absent, since it is not redistributable.

| Test | What it proves | Requires |
|---|---|---|
| nfapi ABI | every nfapi symbol the installed client imports is exported by the shim, and every import Wine lacks is covered by a crash guard | ExitLag installed |
| doctor self-check | the environment is coherent end to end | ExitLag installed |
| external Wine prefix routing | a Windows process in a *different* prefix is translated and redirected, an unmapped prefix is not, and ExitLag's own process table sees neither | ExitLag **running** + root |

## Live / manual — needs an account and a game

Not automated: these need a human, a subscription, and a supported title. Each
was performed during development; see `docs/architecture.md` for the evidence.

| Check | How | Requires |
|---|---|---|
| ExitLag GUI starts and logs in | `tools/run-exitlag-production.sh`, sign in, enable a game | account |
| TCP steering with a supported title | start the backend, launch the game, confirm `DNAT armed` and an established relay socket | account + game |
| UDP steering | same, with a title that sends game UDP; confirm reinjection counts and payload-hash match | account + game |
| Proton identity routing | map the compatdata prefix, launch through Steam | account + Proton title |
| ExitLag restart | restart the client with the backend running; the shim reconnects | account |
| Game restart | restart the game; TCP state expires and re-arms | account + game |
| Helper crash / fail-open | kill the backend mid-session; the game keeps working unsteered | account + game |
| Network reconnect | drop and restore connectivity; flows expire and recover | account + game |
| Clean teardown | stop everything; no `inet exitlag` table, no helper, no lock | — |

## Current status

| Tier | Result |
|---|---|
| unit (fresh checkout, no client) | 5 passed, 0 failed, 2 skipped |
| unit + system + live (full environment) | 10 passed, 0 failed, 0 skipped |

A skip is never counted as a pass, and the runner prints what to install or
start in order to unskip it.
