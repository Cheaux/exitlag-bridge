# Testing

`tests/run-all.sh` is the entry point. Tiers are cumulative, and a tier whose
requirements are not met is reported as **SKIP with the reason** — never as a
pass.

```sh
tests/run-all.sh unit          # no privileges, no ExitLag: any contributor
sudo tests/run-all.sh system   # adds netfilter and privileged-IPC tests
sudo tests/run-all.sh live     # adds tests needing a running ExitLag
sudo tests/run-all.sh all      # everything available here
```

Current state: **16 passed, 0 failed, 0 skipped** with root and ExitLag running.
On a fresh checkout without the client the unit tier is **9 passed, 0 failed,
2 skipped**, both skips attributable to the absent non-redistributable client.

## unit — no privileges, no ExitLag

| Test | Proves |
|---|---|
| shim builds | the nfapi shim compiles and links |
| helper builds | `exitlag-netd` compiles warning-free |
| nfapi ABI matches ExitLag | every nfapi symbol the installed client imports is exported by the shim, and every import Wine lacks is covered by a crash guard |
| identity map pos/neg | an exact mapping translates; an unmapped `.exe` argv spoof is refused |
| crash-guard smoketest | the Wine missing-import guards hold — without them this exits non-zero |
| doctor self-check | the environment is coherent |
| doctor diagnoses | the doctor names the right cause for eight distinct broken environments, rather than only detecting that something is wrong |
| RPC surface scope | every privileged operation refuses out-of-scope arguments: non-loopback redirect targets, and PIDs the helper never observed |
| process discovery | the discovery tool attributes a socket to the right PID across native, Wine and thread-group cases, and excludes shells and launchers rather than proposing them |
| identity-map assistant | it proposes the correct source form, refuses to write an entry without a confirmed Windows identity, is idempotent, and the map it writes loads in the real helper |
| setup simulation | install / status / uninstall / reinstall against a disposable HOME, including recovery from a corrupted prefix, with no write outside the state directory |

## system — needs root

| Test | Proves |
|---|---|
| IPC security negatives | a foreign uid cannot drive the backend; nine malformed-framing cases, version mismatch, and liveness afterwards (12 checks) |
| IPC hostile input | the authenticated RPC surface survives truncated, oversized, and structurally invalid messages, and stays live afterwards |
| lifecycle hardening | start/stop cycles, stale-table refusal, orphan recovery after runner SIGKILL, fail-open with no helper, helper SIGKILL, shim reconnect, version mismatch, unreachable destination (8 scenarios) |
| IPv6 active rewrite | an IPv6 TCP connection is redirected to an address the helper was told at runtime, and an IPv6 target the validator rejects is not armed |

## live — needs root and a running ExitLag

| Test | Proves |
|---|---|
| external Wine prefix routing | a Windows process in a *different* Wine prefix is translated and redirected, an unmapped prefix is not, and ExitLag's own process table cannot see either |

## Not automated

These need a real game and a human, and are recorded in `docs/architecture.md`
rather than run by CI:

- TCP steering with live WoW world traffic
- UDP take-and-reinject against a real game's datagrams
- Proton routing with a real title (the mechanism is covered by
  `docs/process-identity-translation.md`, verified on Proton 9.0 with
  pressure-vessel)

## Notes for contributors

Running the suite under `sudo` executes the unprivileged tier as the invoking
user. Building as root would leave root-owned objects in a user-owned tree and
break the next plain build — which is exactly what happened while writing this
suite, surfacing as a confusing `Operation not permitted` during a rebuild.

Individual tests are runnable on their own; each states its own requirements at
the top. `runtime/logs/suite-<stamp>/` holds one log per test.
