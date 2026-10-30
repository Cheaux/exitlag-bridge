# External-process routing

The Linux backend can map the real executable owning a socket, or a dedicated
Wine/Proton prefix, to the Windows game identity recognized by the official
ExitLag client. Mappings are exact; an unmapped process keeps its ordinary
identity. Never use a process name or dashboard entry alone as routing evidence.

Configure mappings in the desktop launcher's **Configure games** screen. See
[installer instructions](../installers/README.md) and
[architecture](architecture.md) for the runtime and privilege boundaries.

A prefix mapping covers its whole prefix. Separate prefixes are preferable when
multiple games would otherwise share one wineserver. Apply routes in ExitLag
before the game creates a new connection; established sockets cannot be moved.

Historical machine-specific traces are omitted. To report a result, include the
actual gameplay stage, real bidirectional relay traffic, restarts and cleanup,
with personal paths, local addresses, account details and raw captures removed.
