# The four stages, and which of them we control

Most confusion about this project's results comes from collapsing four distinct
things into "does it work". They fail independently, and only one of them is
ours.

```text
1. DETECTION          does ExitLag know the game is running?
        |                 ExitLag's own Process32NextW over its Wine prefix.
        |                 Cannot see native Linux, Proton, or other prefixes.
        v
2. INSPECTION         does traffic reach ExitLag as events it can meter?
        |                 Our backend: attribution, identity translation, the
        |                 nfapi callbacks. THIS IS OURS.
        v
3. DECISION           does ExitLag ask for a redirect?
        |                 Entirely ExitLag's logic. It may return no address at
        |                 all, and often does.
        v
4. APPLICATION        does the redirect get applied to real packets?
                          Our backend: nftables DNAT, reverse translation,
                          fail-open. THIS IS OURS.
```

Stages 2 and 4 are this project. Stages 1 and 3 belong to ExitLag, and every
failure observed so far has been in one of those.

A game can reach stage 2 and stop: ExitLag meters it, shows a session, and never
asks for a redirect. That is not a partial success of routing — it is
recognition without steering, and the UI looks identical either way.

## Per-game evidence

Stated as measured. Hypotheses are marked as such.

| Game | 1. Detection | 2. Inspection | 3. ExitLag decision | 4. Applied | Session survives |
|---|---|---|---|---|---|
| **WoW** (Wine prefix) | yes | yes | redirect offered | **yes** | **yes** |
| **Dota 2** (native) | no — invisible to ExitLag | **yes**, metered in its UI | **no address offered** | n/a | n/a |
| **Minecraft** (native) | no | yes | redirect offered | **yes** | **no** — ExitLag closes it |
| **Tibia** (native) | no | yes | **no address offered** | n/a | n/a |

### WoW
Real TCP and UDP steering proven with live game traffic: destination rewritten
to ExitLag's local proxy, sockets established through play, and UDP
take-and-reinject with 20/20 payload hashes matching and no packet loss.

### Dota 2 (native Linux)
Recognition and metering proven — ExitLag's UI showed the session, the correct
game server and a live ping, for a process it cannot see, without the game being
restarted. It never requested a redirect: `mutated=none` on all 12
`udpConnectRequest` returns, zero `nf_udpPostSend`, zero remote-address rules,
while processing 8515 inbound datagrams.

Relay distance was tested and ruled out: switching the region to North America,
verified applied, produced the same result. Dota uses Steam Datagram Relay, so
the endpoints are Valve relays rather than game servers. **That transport is the
most consistent explanation, but it is inference from ExitLag's behaviour, not a
fact about its internals.**

### Minecraft (native Linux)
The only case where a native Linux game was steered end to end: identity
translated, `DNAT armed`, 803 KB carried through ExitLag's Miami relay. Six
connections across four servers were all steered and all disconnected.

A packet capture on both legs shows **ExitLag closing the session itself** 4.493 s
in — it FINs both relay legs and then the local proxy within 200 µs, unprovoked,
while the relays are still delivering data; one was reset for sending after the
FIN. No RST from any peer, no retransmissions, no zero-window. Reverse NAT was
verified correct in the same capture.

A single-variable A/B settles whose fault it is: with ExitLag routing Minecraft
the session dies on every server; with it toggled off — backend still running,
failing open — the game works normally.

**Whether Windows behaves the same is unknown and needs a Windows ExitLag
install.** A ~4.5 s lifetime with healthy peers has the shape of a timer, but
the mechanism is not observable from outside a closed binary and is not claimed.

### Tibia (native Linux)
Discovery mattered here: `~/Games/Tibia/Tibia` is a launcher, and the game is
`~/.local/share/CipSoft GmbH/Tibia/packages/Tibia/bin/client`. Mapping the
launcher translated only its HTTPS.

With the real client mapped and in-world, every flow is IPv6 to a CDN on `:443` —
no `7171`/`7172` at all. ExitLag returned the filtering flag and no address.
Two constraints would prevent steering and only one is reached: **ExitLag
offering no address is the binding one**; this project's IPv4-only rewrite (since
lifted) was never consulted.

### IPv6 TCP rewrite
Mechanism proven synthetically through the production path — real helper, real
nftables, real IPv6 connection, reverse translation intact, illegal targets
refused. **No ExitLag-provided IPv6 redirect has been observed in any real
game.** It is a capability that is correct and, so far, unused.

## What this means for the project

Every failure has landed in stage 1 or stage 3 — ExitLag's detection and
ExitLag's decision. Stages 2 and 4 have not failed once across four games and
three transports.

That is the strongest available statement about a compatibility layer, and it is
also the limit of what this project can fix from here.
