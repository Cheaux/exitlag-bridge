# Security review: the privileged IPC surface

`exitlag-netd` runs as root (it needs `CAP_NET_ADMIN` for NFQUEUE and nftables)
and applies destination rewrites that arrive over its IPC socket. Its input is
therefore untrusted by definition, and this reviews that boundary.

## The finding: an unauthenticated root-backed control socket

The helper listens on `127.0.0.1:51900`. Loopback is **not** a trust boundary:
every local account can reach that port. Before this review, any local process
could connect, complete the handshake, and be treated as the shim.

Demonstrated with the unprivileged `nobody` account:

```text
as 'nobody': CONNECTED and got 12 bytes of HELLO_ACK back
helper log: shim connected
            hello: shim protocol=2 exitlag_windows_pid=4242
```

Impact: since the helper installs whatever complete IPv4 address+port mutation
its peer returns as an nftables DNAT element, an unprivileged local process
could have redirected this machine's outbound TCP connections to an address of
its choosing, using a root process to do it. A new connection also silently
displaced the existing one, so the attack worked even while ExitLag was
legitimately connected.

## The fix: authenticate the peer's uid

Only root and the desktop account that owns the ExitLag process may connect.
Anything else is refused at `accept()` and logged.

`SO_PEERCRED` is unavailable here — it is an AF_UNIX facility, and Wine's
`ws2_32` has no AF_UNIX, which is the reason this socket is TCP at all. A first
attempt to use it silently refused *every* connection, including legitimate
ones, because it returns `(uid_t)-1` on a TCP socket. The negative tests caught
that before it shipped.

The uid is instead read from the kernel's own accounting: the peer's socket
appears in `/proc/net/tcp{,6}` as the row whose local address is the peer's and
whose remote address is ours, and column 8 of that row is its owning uid.

The permitted uid defaults to `SUDO_UID` (the user who invoked the runner) and
can be set explicitly with `--peer-uid`. If neither is knowable the helper still
starts but says so plainly and permits only root, rather than silently falling
back to accepting everyone.

```text
IPC peer policy: uid 0 and uid 1000 may connect
shim peer uid=1000 accepted          <- the real Wine shim
REFUSED shim connection from uid=65534 (allowed: 0 or 1000).
  A process outside the owning account tried to drive the backend.
```

## Closing the same-user gap: per-session token

The uid check stops other accounts. It cannot distinguish the real shim from any
other process running **as the same user**, which was the remaining
impersonation gap.

The handshake now also requires proof of a per-session secret:

- 32 bytes from `/dev/urandom`, generated fresh on every helper start
- written to one file, `0600`, owned by the permitted uid — created with those
  permissions before any content is written, then renamed into place, so it is
  never briefly world-readable and never observed half-written
- presented in `HELLO` and compared in **constant time**
- verified *before* any routing message is served; a peer that fails is
  disconnected having reached no privileged code path
- never logged, not even a prefix, by either side
- removed on exit, and a new one generated next start, so a captured token is
  useless afterwards

Being able to read that file is the proof, and only the owning user can. The uid
check remains as defence in depth: both must pass.

The protocol version moved 2 -> 3 for the extended handshake, so an old client
is rejected as a version mismatch rather than silently treated as unauthenticated.

`EXITLAG_NETD_TOKEN` tells the shim where to read it; the launchers set it.

### What is still not protected

A process running as the same user that can **read the token file** can still
impersonate the shim — file permissions are the boundary, so anything able to
read the user's files generally (or ptrace their processes) can defeat it. That
is a substantially higher bar than before, when connecting to a port was
sufficient, but it is not a cryptographic guarantee against a same-user
attacker, and it is recorded rather than implied.

## Input validation## Input validation

Every message is length- and framing-checked before use: magic must match, the
declared length must not exceed `EXL_MAX_PAYLOAD` (128 KiB), payload reads are
bounded by a timeout, and a reply whose tag does not match the outstanding
request is discarded rather than acted on. A malformed frame drops the
connection and fails open rather than aborting.

Timeouts bound every wait: 300 ms for a shim reply (after which the packet is
accepted unchanged), 2 s for the handshake, 5 s on the shim's own socket, and a
2 s heartbeat that detects a peer which has stopped answering.

## Other boundaries reviewed

**nftables ownership.** All state lives in one dedicated `inet exitlag` table.
The runner refuses to delete a table it cannot prove it owns, and a recycled
helper PID belonging to an unrelated process is never signalled.

**Path validation.** Identity-map sources are canonicalised with `realpath()`
at load and matched exactly; there is no prefix, substring, or fuzzy matching
anywhere in that path, so a crafted executable name cannot widen attribution.
A malformed map fails helper startup instead of loading partially.

**Privilege split.** The shim runs unprivileged inside Wine as the user. Only
the helper is privileged, and it never executes anything the shim sends — the
shim's replies are consumed as data (addresses, ports, payload bytes), never as
commands or paths.

## Negative tests

`tests/ipc-security-test.sh` (runs with `--no-queue`, so netfilter is never
touched) covers authentication (wrong uid; correct uid with a wrong token; with an
all-zero token; with the correct token; correct token with a wrong protocol
version), token lifecycle (removed on exit, rotated on restart, a replayed
token from the previous session refused, the current one accepted), ten
malformed-authentication cases including a routing message sent before
authenticating, malformed traffic from an *authenticated* peer, liveness
afterwards, and a check that the token never appears in the log. 23/23.

## Hostile input after authentication

`tests/ipc-security-test.sh` attacks the handshake. `tests/ipc-fuzz-test.sh`
attacks everything *after* one succeeds — the case where an attacker already
holds the session token, which is the residual risk recorded above.

30 cases, all surviving and refusing, grouped as:

- **framing and length** — 3-byte header, declared lengths of 4 GiB, 1 GiB and
  one byte over the 128 KiB maximum, header with no body, sized type with an
  empty body, bad magic mid-session
- **message types** — unknown type, type 0 with a payload, a helper-to-shim type
  sent inbound, a second HELLO mid-session
- **sockaddr and address validation** — a 12-byte sockaddr, family 0 and
  0xFFFF, IPv4 `0.0.0.0:0`, multicast and broadcast, IPv6 `::`, IPv6 multicast
  and IPv6 port zero
- **resolve-pid abuse** — pid 0, pid `0xFFFFFFFF`, a pid never reported by the
  helper, and a truncated request body
- **stream abuse** — 1 MiB of random bytes, 2000 tiny malformed frames, and a
  valid header whose body never fully arrives
- **lifecycle** — repeated disconnects halfway through a request, then a
  reconnect

Afterwards a legitimate authenticated session still works, and no nftables state
exists — the suite runs `--no-queue`, so netfilter is never touched.

### Sanitizers

The same corpus was run against a build with `-fsanitize=address,undefined`.
**30/30 with no sanitizer reports**: no memory errors and no undefined
behaviour. Surviving hostile input is weaker evidence than surviving it while
instrumented, since a process can continue happily after corrupting itself.

`EXITLAG_NETD_BIN` selects the binary, so the sanitized build reuses the corpus
rather than duplicating it.

Two compiler warnings appear only in the sanitizer build and were checked rather
than silenced: a `vfprintf` "null format string" (a varargs false positive; every
caller passes a literal) and an `snprintf` truncation warning on the identity-map
target, which is already length-guarded above the call.
