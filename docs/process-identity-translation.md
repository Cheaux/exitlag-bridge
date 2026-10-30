# Process identity translation

The native backend attributes sockets to real processes and translates only
explicit mappings to a Windows identity understood by ExitLag. It does not trust
an executable-looking string in arbitrary process arguments.

## Mapping format

Each non-comment line contains `SOURCE<TAB>WINDOWS_PATH` with a literal Tab.

```text
/opt/games/example/game\tC:\Games\Example\game.exe
wineprefix:/home/USER/Games/example-prefix\tC:\Games\Example\game.exe
```

In the example above replace `\t` with an actual Tab. The source is the real
network-owning native executable, or `wineprefix:` followed by the canonical
Wine/Proton prefix. Windows paths must identify the game selected in ExitLag.

The Linux desktop editor writes a private mapping file. The installed supervisor
checks ownership, permissions and syntax, then copies data into its protected
runtime directory. User-selected paths are data; they are not executed as root.
The firewall queue is scoped to the authorized user.

The macOS adapter instead uses exact allowlisted native executable paths and a
separate user-mode Wine socket companion for each supported WoW client. Its
root helper receives a bounded enable/disable lease, not arbitrary commands.

Historical local network traces are omitted. See
[compatibility](compatibility.md) for tested scope and [architecture](architecture.md)
for details. A mapping's presence alone does not prove real relayed gameplay.
