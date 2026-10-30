# The helper's privileged RPC surface

`exitlag-netd` runs as root. Authentication (`docs/security-review.md`) decides
*who* may talk to it; this document is about *what they can then ask for*. The
goal is that holding the session token is not a licence to make a root process
do arbitrary networking — the helper must be able to do only what the proven
ExitLag runtime needs.

## Messages the helper accepts

| Message | Privileged action it can trigger | Trusted input | Bounds and limits |
|---|---|---|---|
| `HELLO` (0x0001) | none — establishes the session | none; token verified in constant time before anything else | exact length required; version must match; trailing bytes drained |
| `PONG` (0x0004) | none — liveness only | none | length must be 0 |
| `TCP_CONNECT_REPLY` (0x0011) | **installs an nftables DNAT element** | the rewritten destination address and port | see below |
| `RESOLVE_PID_REQ` (0x0020) | reads `/proc/<pid>` | a PID | see below |
| `UDP_POST` (0x0042) | **sends a UDP datagram from a raw socket** | payload bytes, remote address | see below |
| `UDP_STATE` (0x0043) | records per-endpoint state | endpoint id, flags, address | bounded tables; see below |

Anything else is ignored. Every frame is checked for magic, and a declared
length above `EXL_MAX_PAYLOAD` (128 KiB) drops the connection rather than
allocating. Payload reads are bounded by a 300 ms timeout, after which the
packet is accepted unchanged. No message is ever interpreted as a path to
execute or a command; replies are consumed as addresses, ports and bytes.

## TCP destination rewrite

The most powerful operation, and the one most worth narrowing. A reply is only
applied when **all** of these hold:

- the original flow is IPv4 and the reply declares `EXL_VERDICT_MUTATED`
- the returned family is `AF_INET`, the port is non-zero
- the address is not `INADDR_ANY`, not broadcast, not multicast
- the endpoint actually differs from the original (a padding-only change is not
  a redirect)
- **the target is a loopback address (`127/8`)**

That last condition is new. Every redirect ExitLag has ever produced in testing
targeted `127.0.0.1` — its own local proxy. Permitting an arbitrary unicast
destination would have let an authenticated peer steer this machine's outbound
connections to an off-box address, which is far more power than the runtime
needs. `--allow-remote-redirect` re-opens it for deliberate experiments and is
never passed by the production runners.

The element installed is a single exact 4-tuple with a 10-second timeout, in the
project's own table. No general nftables state can be created: there is no
message that carries a rule, a chain, or an expression.

## PID resolution

`RESOLVE_PID_REQ` reads `/proc/<pid>/exe` and `/proc/<pid>/cmdline`. Unscoped,
this is a "what is process N" oracle for every process on the machine, usable
to enumerate paths a user might otherwise not see.

It is now answered **only for PIDs the helper itself reported** to the shim.
Those are exactly the PIDs the runtime needs resolved, since the shim only ever
asks about a PID it was just handed. Anything else returns an empty path and is
logged as refused. `--allow-any-pid-resolve` exists for IPC-only test harnesses
that have no real traffic, and is never passed by the production runners.

No filesystem path is ever *accepted* from the peer — only a PID. The identity
map is read from a file named on the helper's own command line, not over IPC.

## UDP reinjection

`UDP_POST` carries a payload the helper may re-send from a raw socket. The
destination is not free: the take-and-reinject path requires the posted remote
address to be **byte-identical to the original destination**, so this cannot be
used to send traffic somewhere new. Payload length is bounded by
`UDP_POST_PAYLOAD_MAX` (65507, one UDP datagram) and by the frame limit.

Per-endpoint state is fixed-size: at most 2048 endpoints and 16 unique remote
rules each, so a peer cannot drive unbounded allocation. Endpoints expire on
socket close or 60 s idle.

## What a hostile authenticated peer still gets

Being able to redirect a connection to a chosen **loopback port** is real power:
it can capture traffic the user's own machine sends, if it can also listen on
that port. It cannot send that traffic off-box through this interface, cannot
create arbitrary firewall state, cannot read arbitrary files, and cannot
enumerate unrelated processes.

That residual is inherent to what the runtime does — ExitLag's whole purpose is
to redirect connections to a local proxy — and is bounded by the fact that only
a process able to read the owning user's token file can reach it at all.

## Tests

`tests/rpc-surface-test.sh` runs as an *authenticated* peer, because
authentication is not authorisation. It checks that an unreported PID is
refused rather than resolved, that the loopback-only redirect policy and its
override are present, and that malformed privileged requests (short resolve
request, absurd UDP length, truncated TCP reply, PID `0xFFFFFFFF`) do not kill
the helper. 4/4.
