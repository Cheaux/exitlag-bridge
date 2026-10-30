# Wine missing-import crash guards

Date: 2026-09-11. Status: **implemented and verified against the live client.**

## Symptom

Opening PC Boost — and several other pages — killed the whole ExitLag client
immediately. Route optimisation itself was unaffected; only certain UI pages
died, taking the process with them.

## Root cause

Crashpad minidump
(`runtime/backups/pc-boost-guard-20260911T175000/02d71de2-….dmp`) parsed
directly:

```text
thread=36  code=0x80000100  flags=0x1 (EH_NONCONTINUABLE)
params    = 0x146c7e076, 0x146c7de66
```

`0x80000100` is `EXCEPTION_WINE_STUB`. Both parameters point into ExitLag's own
import directory and resolve, from the on-disk image, to:

```text
param0 -> "IPHLPAPI.DLL"
param1 -> "GetInterfaceDnsSettings"
```

ExitLag imports functions that **Wine does not export**. Wine's loader resolves
each missing import to a synthesised stub that raises `EXCEPTION_WINE_STUB` when
called. The exception is flagged non-continuable, so the process dies; there is
no error path for ExitLag to handle.

### An earlier diagnosis was wrong

A previous in-flight fix guarded `iphlpapi!ConvertInterfaceLuidToGuid` on the
theory that Wine exported it as a fatal stub. That function is **fully
implemented** in Wine 11.17 (`dlls/iphlpapi/iphlpapi.spec` line 19, code at
`iphlpapi_main.c:4415`, no FIXME), and it is not what the dump names. That guard
was never verified and has been replaced.

## The complete set

Derived by diffing every import of `ExitLag.exe` and each bundled DLL against
the exports of the Wine DLLs actually loaded from
`/usr/lib/wine/x86_64-windows`. Eleven imports are missing, all in the main
executable — no bundled ExitLag DLL is affected, so only the main image's import
table is touched.

| DLL | missing import | page it breaks |
| --- | --- | --- |
| `IPHLPAPI.DLL` | `GetInterfaceDnsSettings` | DNS settings (the confirmed crash) |
| `IPHLPAPI.DLL` | `SetInterfaceDnsSettings` | DNS settings |
| `IPHLPAPI.DLL` | `FreeInterfaceDnsSettings` | DNS settings |
| `IPHLPAPI.DLL` | `GetOwnerModuleFromUdpEntry` | connection/owner listings |
| `IPHLPAPI.DLL` | `GetOwnerModuleFromUdp6Entry` | connection/owner listings |
| `IPHLPAPI.DLL` | `InitializeIpInterfaceEntry` | adapter tuning |
| `IPHLPAPI.DLL` | `ResolveIpNetEntry2` | adapter tuning |
| `IPHLPAPI.DLL` | `SetIpInterfaceEntry` | adapter tuning |
| `POWRPROF.dll` | `PowerDuplicateScheme` | **PC Boost** (power schemes) |
| `POWRPROF.dll` | `PowerWriteDCValueIndex` | **PC Boost** |
| `POWRPROF.dll` | `PowerWriteFriendlyName` | **PC Boost** |

Reproduce the list with the diff in `tools/` or by comparing
`llvm-readobj --coff-imports` against `--coff-exports` of each Wine DLL.

## The fix

`shim/nfapi/nfapi_stub.cpp`, namespace `compat`. On `DLL_PROCESS_ATTACH` — which
runs before ExitLag's entry point, so no UI click can beat it — the shim walks
the main image's import descriptors and replaces each of the eleven IAT slots
with a function returning `ERROR_NOT_SUPPORTED` (50), the ordinary Win32
contract for an unavailable capability.

Deliberate choices:

- **Output buffers are left untouched.** Their contents are undefined on failure,
  and writing a guessed number of bytes into a caller structure could corrupt
  memory to paper over a feature that cannot work anyway.
- **The goal is clean failure, not function.** PC Boost cannot edit Windows power
  schemes on Linux and DNS settings cannot be written through Wine; these
  features are expected to report failure, not to work.
- The PE walk is bounds-checked against the image size and the declared import
  directory size, so malformed metadata cannot escape it.
- A zero-argument replacement is safe for any arity: the x64 convention is
  caller-cleanup with arguments in registers and caller-allocated shadow space.
  Functions declared `void` in Windows simply ignore the returned value.

## Verification

**Synthetic** (`tests/nfapi-compat-smoketest.cpp`, run via
`tests/build-nfapi-compat-smoketest.sh`): an executable that imports
`GetInterfaceDnsSettings` and `PowerWriteDCValueIndex` and calls both before
`nf_init`. Without the guard the process dies inside the first call; with it,
both return 50 and the process exits 0. The log shows the replaced slots and
their previous values (`before=0x270000`, i.e. Wine's stubs).

**Live client**: all eleven guards install —

```text
compat.install state=done guards=11/11
compat.guard IPHLPAPI.DLL!GetInterfaceDnsSettings state=installed slot=0x146c6c208 before=0x370000 after=0x6ffff…
… (11 entries, every one installed)
```

and the previously fatal path now survives:

```text
compat.call guard=IPHLPAPI.DLL!GetInterfaceDnsSettings call=1 policy=unsupported return=50
```

with the client still running and **zero new Crashpad dumps**.

Evidence: `runtime/backups/compat-guards-verified-20260911T185107/`.

## Side effect: the Multipath route controls now work

The TCP/UDP Routes spinners in a connection's Advanced settings previously could
not be changed. With the guards installed they behave normally. The page reaches
`GetInterfaceDnsSettings` while rendering, so before the fix it was dying part
way through instead of applying the change. This was not a display bug.

## Scope

This only prevents crashes. It does not make PC Boost, DNS optimisation or
adapter tuning do anything on Linux — those need Windows facilities Wine does
not provide. If a guarded page later needs to *work*, the corresponding
capability has to be implemented in the helper, not faked in the shim.

## Data-plane regression after the shim swap

Replacing `nfapi.dll` changes the binary that the whole backend talks to, so
both data planes were re-proved against the guarded build. `exitlag-netd`
rebuilt bit-identical to the binary from the last known-good run, which made the
shim the only changed component.

TCP passed directly: WoW's world connections to `:3724` produced `DNAT armed;
ACCEPT (active)` with ExitLag's live callback supplying the relay endpoint, and
the sockets stayed `ESTABLISHED` through in-world play.

UDP first reported `ACCEPT unchanged (UDP contract outside proven active
subset)`. That was **not** a regression. The active gate at
`backend/exitlag-netd.c:1092` requires `g_udp_post.status == 0`, and that
ExitLag session had been launched with the default `EXITLAG_NFAPI_SUCCEED`
list, which omits the UDP entry points — so the shim deliberately returned `-1`
from `nf_udpPostSend` and the helper failed open exactly as designed. The
callback shape was byte-identical to the known-good run (`posts=1,rules=1,
controls=0,direct_remote=0,direct_payload=0,direct_options=0`, 400 events in
both), which is what isolated the cause to the launcher argument.

Re-running with the UDP succeed list reproduced the full active path and matched
the original Stage 4 proof exactly: 20 `DROP original; POST_SEND reinjected with
bypass mark`, 20 packets / 940 bytes at the `0x584c0001` mark counter, 20
`ACCEPT POST_RECEIVE reply to original socket`, 20/20 payload hashes matching,
0 ENOBUFS, concurrent TCP unaffected, 0 new crash dumps.

Two things worth remembering for future runs: WoW Classic Era emits **no
in-world UDP**, only a ~2.4 s burst of realm-list latency probes at login (20
endpoints x 20 datagrams), so a UDP proof needs a client restart, not just
gameplay; and the runner's cleanup trap cannot fire while bash is blocked in
its foreground `sleep`, so terminate the `sleep` child rather than signalling
the script if a run must be stopped early.

Evidence: `runtime/backups/regression-postguard-20260911T203950/` and
`runtime/backups/tcp-verified-postguard-20260911T203548/`.
