# Repository layout

The project grew through reverse engineering, so the tree separates what the
runtime needs from what was used to discover it. Nothing was deleted; research
material moved rather than disappearing.

## Production — required to run

```text
backend/          the privileged Linux helper and its nftables surface
  exitlag-netd.c    NFQUEUE helper: attribution, IPC, TCP DNAT, UDP reinjection
  protocol.h        shim <-> helper wire format
  Makefile
  nft-setup.sh      installs the dedicated `inet exitlag` table
  nft-teardown.sh   removes it (single atomic delete)
  runtime-guard.sh  sourced lifecycle ownership: flock, pidfile, orphan recovery
  run-tcp-redirect.sh   active TCP runner
  run-udp-steer.sh      active TCP + narrow UDP runner
  run-passive.sh        observation only, never mutates
  run-udp-observe.sh    UDP observation only
  identity-map.example  documented configuration template

shim/
  nfapi/            the nfapi.dll ABI shim ExitLag loads (build.sh, *.def)
  nfextlag/         driver-name compatibility surface

wine-wbemprox/    prefix-local Wine WMI patch (Win32_LogicalDiskRootDirectory)

tools/            user-facing entry points
  exitlag-setup.sh             guided install / status / uninstall (idempotent)
  exitlag-doctor.sh            pre-flight diagnosis; changes nothing
  discover-game-processes.py   which process actually owns the game's sockets
  identity-map-assistant.py    turn that into a map entry (Windows side confirmed)
  run-exitlag-production.sh    production launcher, no research instrumentation
  run-exitlag-gameprefix.sh    same prefix with the research trace enabled
  check-exitlag-version.sh     ExitLag build-drift detection
  install-nfextlag-stub.sh / remove-nfextlag-stub.sh
  install-wbemprox-prefix-override.sh / restore-wbemprox-prefix-override.sh
```

## tests/ — automated verification

Runnable by anyone; only some need a live ExitLag.

```text
harden-runtime-test.sh        lifecycle/failure scenarios (needs sudo)
validate-external-prefix.sh   external Wine prefix routing (needs sudo + ExitLag)
test-identity-map.sh          identity translation, IPC only, no privileges
discovery-test.sh             launcher-versus-client detection
identity-assistant-test.sh    never invents a Windows identity
setup-simulation-test.sh      fresh install, idempotence, recovery, uninstall
ipc-fuzz-test.sh              hostile input after authentication (needs sudo)
ipv6-redirect-test.sh         synthetic IPv6 active rewrite (needs sudo)
doctor-test.sh                each failure class is distinguished
test-shim-reconnect.sh        heartbeat timeout and reconnect
test-external-route.sh        external routing smoke test
test-tcp-redirect.py          TCP callback loop
test-udp-observe.py           UDP lifecycle
build-nfapi-compat-smoketest.sh + nfapi-compat-smoketest.cpp
                              crash-guard regression against Wine stub imports
nfapi-reconnect-smoketest.cpp
verify-nfapi-abi.sh           shim export/ABI sanity
external-probe/               minimal Windows TCP probe for prefix tests
fakegame/                     placeholder Windows process
```

## research/ — how the design was discovered

Not required to run anything. Kept because it documents the reverse
engineering and would be expensive to recreate.

```text
reversing/          PE analysis, disassembly helpers, handle comparison
probes/             one-off Windows probes: WMI, WebView2, nfextlag surface,
                    GetExtendedTcpTable, shim smoke/policy tests
wine-experiments/   Wine bring-up, prefix experiments, synthetic callback
                    injection, gate sessions, game installation helpers
ui/                 screenshot and window-interaction helpers
stage3a-probe.py, stage3a-discriminate.py
                    early flow-discrimination experiments
```

## runtime/ — machine-specific, not portable

Wine prefixes, session logs, captured evidence, and checkpoint backups. Almost
all of it is generated and much is specific to this machine. See
the private release audit before publishing anything from here.

## What the runtime must never depend on

The production path does not use synthetic callback injectors, memory scanners,
disassembly output, or research-only environment variables. `research/` can be
deleted entirely and the runtime still works; the test suite will still pass
except for the tests that live there.
