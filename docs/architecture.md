# Architecture

This describes the design as it is actually proven to work. It is a freeze of
the current implementation, not a plan.

The project replaces exactly one thing: the Windows packet-interception backend
that ExitLag's client talks to. Everything else — login, subscription, relay
selection, route optimisation, the UI — remains ExitLag's own code, running
unmodified under Wine.

## The stack

```text
       ExitLag.exe + libexitlag.dll   (official, unmodified, under Wine)
                    |
                    |  imports nfapi.dll
                    v
       nfapi.dll compatibility shim   (shim/nfapi/nfapi_stub.cpp)
                    |
                    |  loopback TCP, 127.0.0.1:51900
                    v
       exitlag-netd                   (backend/exitlag-netd.c, runs as root)
                    |
                    |  libmnl / NFQUEUE verdicts, nftables map updates
                    v
       nftables `inet exitlag`        (dedicated table, created and destroyed
                    |                  by this project only)
                    v
       TCP destination rewrite / UDP take-and-reinject
```

The loopback socket is not a stylistic choice: Wine's `ws2_32` has no `AF_UNIX`
support (`dlls/ws2_32/unixlib.c`), so a Windows-side DLL cannot use a unix
socket to reach a native helper.

## Control plane vs data plane

**Control plane — ExitLag's, untouched.** Authentication, subscription checks,
relay selection, route analysis and optimisation, the connection list, and the
per-connection decision of *whether and where* to redirect. The shim never
invents a routing decision; it only reports facts and applies what ExitLag
returns.

**Data plane — this project.** Seeing packets, attributing them to a process,
asking ExitLag what to do, and carrying out that answer with nftables.

The boundary is the `nfapi` ABI. ExitLag calls into the shim to configure
filtering and receives callbacks; the shim forwards those events to the helper
and returns the helper's observations. Nothing in the helper knows what a relay
is.

## TCP flow

Filtering rules ExitLag installs are deliberately broad:

```text
nf_setRulesEx.rule protocol=6(TCP) processId=0 direction=2(OUT)
  filteringFlag=0x10(INDICATE_CONNECT_REQUESTS) processName=""
```

`processId=0` and `processName=""` mean every process is filtered and the real
decision is deferred to a per-connection callback. That single fact is what
makes external-process routing possible at all.

A redirected connection goes like this:

1. `output_queue` (filter hook, priority -150) queues **only** the first SYN of
   a new outbound TCP connection. Loopback is skipped, and `ct status dnat` is
   skipped so an already-redirected flow is never re-examined.
2. The helper attributes the socket to a PID, then calls the shim.
3. The shim runs ExitLag's `tcpConnectRequest` callback. ExitLag either leaves
   the address alone or rewrites it to one of its local proxy endpoints and sets
   `filteringFlag` to `0x210` (redirect).
4. On a complete address+port mutation, the helper installs one exact element
   into the family's map — `tcp_redirect_v4` or `tcp_redirect_v6` — keyed by the
   full 4-tuple, with a 10-second timeout. The target is validated per family
   before anything is armed (not unspecified, not multicast, not loopback), and
   a mutation the validator rejects arms nothing rather than arming something
   approximate.
5. The SYN is ACCEPTed and continues to `output_nat` (nat hook, dstnat
   priority), where the map performs the DNAT. Conntrack carries the translation
   for the rest of the connection, so the steady-state data path is untouched.

The queue sits after conntrack but before NAT specifically so the map element
can be installed while the SYN is still suspended.

Observed end to end: a process believing it is connected to `66.40.178.239:3724`
has its traffic terminated inside `ExitLag.exe` on `127.0.0.1`.

## UDP flow

UDP has no connection to pin a DNAT map to, so it uses take-and-reinject.

Two chains (`output_udp_observe`, `input_udp_observe`, filter priority -10)
queue datagrams in both directions. The helper tracks endpoints keyed by a
synthetic 64-bit id and reports lifecycle events (`udpCreated`,
`udpConnectRequest`) plus each datagram to ExitLag.

For a datagram ExitLag has posted a replacement for, the helper drops the
original and re-sends ExitLag's version from a raw socket carrying mark
`0x584c0001`. A dedicated rule accepts that mark so the reinjected copy does not
re-enter the queue. Replies are delivered back to the original socket.

Endpoint state is bounded and self-expiring: at most 2048 endpoints, 16 unique
remote-address rules each. An endpoint is released when its socket inode
disappears (checked once idle for 2s) or unconditionally after 60s idle, with a
lifecycle event emitted to ExitLag so its own per-endpoint state is released too.

## Process attribution and identity

The helper attributes a queued packet to a socket inode, then to a holding PID
via `/proc`. What that PID *is* differs by case, and this is the subtlest part
of the system:

- **Native Linux process** — the PID is the process itself. `/proc/<pid>/exe` is
  its real executable.
- **Wine or Proton** — at SYN time the socket is held **only by that prefix's
  `wineserver`**. The game process acquires the fd later, too late to attribute.
  Every `wineserver` has an identical `/proc/<pid>/exe` and argv, so neither can
  identify which game it is serving.
- **Inside ExitLag's own prefix** — ExitLag resolves its own Wine PIDs through
  `GetExtendedTcpTable(TCP_TABLE_OWNER_PID_ALL)`, which works only within its
  own prefix.

The shim asks the helper to resolve any PID it does not recognise as a Windows
one (`nf_getProcessNameFromKernel` → nested `RESOLVE_PID` request on the same
connection, served while the helper is already waiting for that callback's
reply).

### Identity mapping

`exitlag-netd --identity-map FILE` loads exact `SOURCE<TAB>WINDOWS_PATH` pairs.
Three source forms, each matching a different attribution case:

| Source form | Matched against | Use |
|---|---|---|
| absolute host path | canonical `/proc/<pid>/exe` | native Linux game |
| exact `.exe` argv token | one whole argv entry | a process that owns its own socket |
| `wineprefix:<dir>` | `WINEPREFIX` from `/proc/<pid>/environ` | **required** for Wine and Proton |

Loading a map enables **strict mode**, which disables the fallback argv scan.
Without that, any process could impersonate a configured game by putting the
path in its own command line. In strict mode an unmapped process keeps its
genuine executable and therefore fails open, unrouted.

Both sides of a comparison pass through `realpath()`, which matters in practice:
Proton sets `WINEPREFIX` with a trailing slash while a user writes the entry
without one.

See `docs/process-identity-translation.md`.

## Runtime ownership

`backend/runtime-guard.sh`, sourced by the active runners, makes the backend
single-owner:

- an `flock` on `/run/exitlag-linux/backend.lock`; a second runner refuses to start
- a pidfile recording the helper PID **and** its resolved binary path, so a
  recycled PID belonging to an unrelated process is never signalled
- on startup, an orphaned helper from a killed runner is terminated and its
  table removed
- a table with no verifiable project ownership is **refused, not deleted**
- the lock fd is closed in spawned children, so a SIGKILLed runner cannot leave
  its helper holding the lock

## Heartbeat and reconnect

The helper sends `EXL_MSG_PING` every 2s; the shim replies `PONG`. The shim sets
5s send/receive timeouts on its socket and retries a lost connection every 2s.

This exists because Wine can leave a loopback connection *established* after the
helper is gone, which would otherwise pin the shim's thread forever on a peer
that will never answer. The timeout converts that into the ordinary fail-open
reconnect loop.

## Fail-open semantics

Every failure path accepts the packet unchanged. There is no configuration in
which losing this software breaks networking:

- `queue ... bypass` — if the helper is not running, the kernel accepts rather
  than dropping. This is a kernel-level guarantee, not application logic.
- no shim connected, shim reply slower than 300ms, malformed reply, unchanged
  callback result, partial or non-IPv4 mutation, unsupported UDP contract shape,
  raw-send failure, attribution miss → ACCEPT unchanged.
- protocol version mismatch → disconnect and fail open.

## Cleanup guarantees

All state lives in one dedicated `inet exitlag` table, so teardown is a single
atomic delete. Runners install it only after the helper is live and remove it
from an EXIT trap that also covers Ctrl-C and crashes. `runtime_guard_wait`
returns as soon as the helper dies rather than waiting out a timer, so the table
does not linger in fail-open mode.

Verified: no table and no helper process remain after normal exit, Ctrl-C,
SIGKILL of the runner, or helper crash.

## Wine-specific compatibility

- **Crash guards.** ExitLag imports 11 functions Wine does not export (8 in
  IPHLPAPI, 3 in POWRPROF). Wine resolves a missing import to a stub that raises
  the non-continuable `EXCEPTION_WINE_STUB` (0x80000100), which kills the
  process — this is what crashed the client on the PC Boost and DNS pages. The
  shim patches the main image's IAT at process attach so those return
  `ERROR_NOT_SUPPORTED` instead. See `docs/wine-stub-crash-guards.md`.
- **WMI.** ExitLag queries `Win32_LogicalDiskRootDirectory`, which Wine's
  `wbemprox` does not implement; a prefix-local patched `wbemprox` supplies it.
- **Rendering.** The Qt UI needs `QSG_RHI_BACKEND=opengl` under this Wine build.
- **`nfextlag`.** A compatibility surface for the driver-name ExitLag expects.

## Status of each behaviour

**Proven with live ExitLag and real traffic**

- TCP destination rewrite, including real WoW world connections
- UDP take-and-reinject (20/20 payload hashes matched, 0 ENOBUFS)
- crash guards (11/11 installed; previously fatal pages now survive)
- native Linux process identity translation
- ExitLag associating a live session for a process it cannot itself see: its
  UI showed the native Dota 2 session, matching endpoint, without a restart
- separate Wine prefix routing, with an unmapped control not routed
- Proton routing including pressure-vessel containment
- helper crash, runner SIGKILL, reconnect, version mismatch, stale-table refusal,
  unreachable destination — all fail open and recover

**Optional / debug only**

- `EXITLAG_NFAPI_TRACE=1` — full per-call argument trace. Production logs only
  lifecycle events and explicitly unsupported calls.
- `EXITLAG_NFAPI_SUCCEED`, `EXITLAG_NFAPI_EXTVER` — research overrides for the
  built-in export table and extensions version. **Not required in production**
  and not consulted unless set.
- `EXITLAG_NFAPI_INJECT` — synthetic callback injection, research only.
- `--self-test`, `--udp-active-once`, `--udp-active-index`, `--udp-exe` — narrow
  experiment scoping.

**Known limitations**

- `wineprefix:` maps an entire prefix to one game identity. Correct for Proton
  (one prefix per title); too coarse for a hand-made prefix holding several games.
- Strict mode applies to in-prefix games too. A game inside ExitLag's own prefix
  that is not in the map resolves to the ELF loader and stops being routed.
- TCP rewriting handles both families. Active **UDP** rewriting is IPv4 only.
- The IPv6 TCP path is proven against a synthetic peer, not a live title: no
  game observed here has yet been given an IPv6 route by ExitLag, so the
  decision stage has never exercised it. See `docs/ipv6-active-rewrite.md`.
- Redirection method must stay Automatic/WFP. NDIS needs `\\.\NDEXTLAG`.
- Route analysis protocol should be TCP; ICMP works but is widely rate-limited.
- Not tested: physical link down/up, and long-duration soak beyond ~25 minutes.
