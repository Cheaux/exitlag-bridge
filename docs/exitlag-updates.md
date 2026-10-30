# Surviving ExitLag updates

ExitLag auto-updates, and this project is not told when it does. The shim
reproduces a private ABI (`nfapi`) against a specific build, so an update can
change what the client expects without any visible symptom until it crashes or
silently stops being steered.

**The updater is not disabled.** Blocking it would leave users on an old client
against a live service, and ExitLag's own auth and relay code is the part we
deliberately do not touch. Instead, updates are detected and verified.

## The flow

```text
launch
  |
  +-- build matches the recorded known-good?  -- yes --> run
  |
  no
  |
  +-- re-derive the ABI this build actually needs
        |
        +-- still compatible? -- yes --> WARN loudly, run,
        |                                invite the user to record it
        |
        no --> REFUSE to start, explain exactly what changed
```

Implemented in `tools/run-exitlag-production.sh`. The research launchers do not
gate, so deliberate work on a new build is still possible.

## What is tracked

**`tools/check-exitlag-version.sh`** records the identity of the build the
runtime was verified against: product version, SHA-256, size, and how many of
the guarded imports are present. Exit 0 unchanged, 1 drift, 2 no baseline.
Record a build as known-good with `--record`.

This is identity, not compatibility. A hash tells you *something* changed, not
whether it matters.

**`tools/check-exitlag-abi.sh`** answers whether it matters, by re-deriving the
two things the shim actually depends on from the binaries present right now:

1. **Every `nfapi` symbol ExitLag imports is exported by the shim.** The
   decorated import list is read from ExitLag's import table and compared with
   the shim's export table. A symbol ExitLag imports that the shim lacks is
   fatal: Wine resolves it to a stub that raises a non-continuable exception.

2. **Every import Wine does not provide is covered by a crash guard.** For each
   DLL ExitLag imports from that Wine also ships, the imports are diffed against
   Wine's own exports; anything missing must appear in the shim's guard table.
   A new uncovered one is a crash waiting for the user to open the wrong page.

This is why a bare hash pin would be wrong: the hash changes on every update,
while the ABI usually does not. Checking the ABI lets a routine update through
and stops a breaking one.

Current state of the installed build:

```text
== nfapi ABI ==
  ExitLag imports : 18 symbols
  shim exports    : 18 symbols
  OK: the shim provides every nfapi symbol this build imports

== Wine missing-import crash guards ==
  OK: every import Wine does not provide is covered by a crash guard
      (16 DLLs checked, 11 guards installed)
```

## What is not checked automatically

- **Behaviour changes behind an unchanged ABI.** If ExitLag keeps the same
  exports but changes what it does with them — a different callback contract, a
  new filtering-rule shape — no static check will see it. The regression tests
  are what catch that, and they need a real client.
- **WMI and GUI assumptions.** The client needs
  `Win32_LogicalDiskRootDirectory`, which Wine does not implement, and renders
  with `QSG_RHI_BACKEND=opengl`. A UI rewrite could change either. These are
  covered by `tools/exitlag-doctor.sh` reporting the environment, not by a
  version check.
- **`libexitlag.dll`.** Its version is recorded as part of the build identity
  but the shim does not link against it, so there is nothing to verify.
- **The legacy NDIS path.** `ndisapi.dll` / `ndextlag.sys` are not used; the
  redirection method must stay Automatic/WFP.

## When a check fails

Drift reported, ABI still fine — nothing is wrong. Run it, and once it behaves,
`tools/check-exitlag-version.sh --record`.

ABI incompatible — the report names the exact symbols. For a missing `nfapi`
export, add it to `shim/nfapi/nfapi_stub.cpp` and rebuild; the export must match
the decorated name exactly. For an unguarded import, add it to the guard table
(`docs/wine-stub-crash-guards.md` describes the mechanism). Then re-run the
regression suite before recording the new build.
