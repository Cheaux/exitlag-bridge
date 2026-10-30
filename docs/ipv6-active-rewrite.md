# IPv6 active TCP rewrite

**Status: mechanism validated synthetically. No ExitLag-provided IPv6 redirect
has yet been observed in a real game.**

That distinction is the point of this document. The code path works end to end
against a real IPv6 connection, real nftables state and the real privileged
helper — but the thing that would drive it in production, an ExitLag callback
returning an IPv6 target, has never been seen.

## This does not fix Tibia

Tibia's callbacks returned `mutated=[@1:0x0->0x2]` — the filtering flag and
nothing else. **No address was offered, in either family.** The IPv4-only check
was never reached, so it was never the binding limitation, and removing it
changes nothing for Tibia. See `docs/native-linux-validation.md`; that result is
unchanged by this work.

## What was added

The smallest change that reuses the existing path rather than duplicating it:

- `nft-setup.sh`: a `tcp_redirect_v6` map (`ipv6_addr . inet_service . ipv6_addr
  . inet_service : ipv6_addr . inet_service`, 10 s timeout) and one
  `dnat ip6 to ... map @tcp_redirect_v6` rule beside the existing IPv4 one.
- `nft_arm_tcp_redirect()` takes a family and selects the map and address form
  from it. One function serves both.
- The redirect decision block accepts `AF_INET` or `AF_INET6`, differing only in
  sockaddr layout (address at +4 vs +8), expected family (2 vs 23), and the
  invalid-target set.

No new message types, no parallel implementation, no change to the callback
contract. The queue already saw IPv6 SYNs; only the *apply* half was missing.

## Validation preserved, per family

| Check | IPv4 | IPv6 |
|---|---|---|
| family must match the flow | `2` | `23` (Windows `AF_INET6`) |
| port zero | rejected | rejected |
| invalid target | `0.0.0.0`, broadcast, multicast | `::`, multicast (`ff00::/8`) |
| padding-only change | rejected | rejected |
| loopback-only policy | `127/8` | `::1`, and `::ffff:127/8` |

The IPv4-mapped loopback case is included deliberately: it is the *same* local
proxy reached over a dual-stack socket, so excluding it would refuse a
legitimate target. `--allow-remote-redirect` still overrides, as before.

## Synthetic proof

`tests/ipv6-redirect-test.sh` (root; in the suite's system tier). It is not a
unit test: it runs the production helper with real netfilter, and a harness
plays the shim's role over the **authenticated** IPC socket, returning a
legitimate IPv6 mutation.

```text
IPv6 client -> helper -> (harness returns ::1) -> nft IPv6 DNAT
             -> local IPv6 listener -> reply -> reverse translation -> client
```

11/11:

- `tcp_redirect_v6` map and `dnat ip6` rule installed
- a real IPv6 TCP connection reaches the local listener
- the listener accepts it and replies
- **the client still observes the original remote endpoint** — reverse
  translation intact
- the helper logs `DNAT armed`
- four illegal targets refused by **real connections**, not source inspection:
  `::` and multicast as *invalid unicast target*, port zero as *port zero*, a
  routable global address as *non-loopback*
- teardown removes the map and all table state

Skips cleanly with an explanation if the host has no IPv6.

## UDP was inspected and deliberately left alone

UDP active steering carries addresses in the same 28-byte sockaddr, so the
representation is not the obstacle. It is excluded because its semantics are not
understood well enough to test safely: the active path requires the posted
remote address to be byte-identical to the original, and the take-and-reinject
raw-send path is IPv4-only in ways that need their own proof. `run-udp-steer.sh`
still refuses a non-IPv4 UDP destination.

Adding IPv6 UDP would mean shipping an untestable second guess. Scope stays at
what can be proven.

## What would upgrade this from synthetic to proven

One observation of ExitLag returning an `AF_INET6` remote with a real address
change for a real game. Until then this is a capability that is correct and
unused, and should be described that way.
