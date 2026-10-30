# Wine setup

ExitLag needs four things Wine does not give it out of the box. Each is
**prefix-local or project-local, reproducible from a script, and reversible**.
None requires editing the system Wine installation.

| Fix | Scope | Applied by |
|---|---|---|
| WMI `Win32_LogicalDiskRootDirectory` | project-local Wine build | `wine-wbemprox/build-local.sh` |
| Missing-import crash guards | inside the shim | `shim/nfapi/build.sh` |
| Qt/OpenGL rendering | one environment variable | the launchers |
| `nfextlag` driver surface | prefix-local | `tools/install-nfextlag-stub.sh` |

Run `tools/exitlag-doctor.sh` afterwards; it reports which of these are in
place.

## 1. WMI: `Win32_LogicalDiskRootDirectory`

ExitLag queries this WMI class during startup. Wine's `wbemprox` does not
implement it, so the query fails.

Wine resolves `wbemprox` from its **builtin module directory**, not from the
prefix, so copying a DLL into `drive_c/windows/system32/wbem/` does nothing.
That was tried and reverted; `tools/install-wbemprox-prefix-override.sh` now
refuses to run and says why, so the dead end is not rediscovered.

The working approach builds a patched Wine into a project-local staging
directory:

```sh
wine-wbemprox/build-local.sh
```

It checks out Wine 11.17 at a pinned commit under `vendor/`, applies
`wine-wbemprox/patches/0001-wbemprox-add-logicaldisk-root-directory.patch`, and
stages the result in `wine-wbemprox/runtime-staging/`. The launchers use that
staged Wine via `WINELOADER`.

**Reversible:** delete `wine-wbemprox/runtime-staging/` (and `vendor/`). The
system Wine is never written to. **Attribution:** the patch is a diff against
Wine, which is LGPL-2.1-or-later — see `THIRD_PARTY.md` and `docs/provenance-review.md`.

## 2. Missing-import crash guards

ExitLag imports 11 functions Wine does not export — 8 in `IPHLPAPI`, 3 in
`POWRPROF`. Wine resolves a missing import to a stub that raises the
non-continuable `EXCEPTION_WINE_STUB` (0x80000100), which kills the process.
This is what crashed the client when the PC Boost and DNS pages were opened.

The fix lives **in the shim**, not in Wine: at process attach it rewrites those
entries in ExitLag's import table so they return `ERROR_NOT_SUPPORTED` instead
of dying. Nothing outside the process is touched.

```sh
shim/nfapi/build.sh
cp shim/nfapi/build/nfapi.dll runtime/app-shim/
```

**Reversible:** restore the original `nfapi.dll`. **Verify:** the guard set is
re-derived from the installed binaries by `tools/check-exitlag-abi.sh`, so a
Wine or ExitLag update that introduces a new missing import is reported rather
than discovered as a crash. Detail in `docs/wine-stub-crash-guards.md`.

## 3. Qt / OpenGL rendering

The client's Qt UI renders incorrectly under this Wine build unless the scene
graph uses OpenGL:

```sh
QSG_RHI_BACKEND=opengl
```

The launchers set it. It is an environment variable only — nothing is installed,
and unsetting it is the whole reversal.

## 4. `nfextlag` driver surface

ExitLag expects a driver/service named `nfextlag` to exist and calls
`StartServiceA` on it. The stub satisfies that presence check; it does no
filtering, because all real interception happens in the Linux backend.

```sh
shim/nfextlag/build.sh
tools/install-nfextlag-stub.sh          # START_MODE=demand (default)
```

`demand` mirrors the real client's behaviour: ExitLag starts the service itself.
`START_MODE=auto` makes `services.exe` start it at prefix boot.

**Reversible:** `tools/remove-nfextlag-stub.sh`.

## Prefixes

| Prefix | Purpose |
|---|---|
| `runtime/prefix` | pristine baseline, left untouched for comparison |
| `runtime/prefix-game` | the working prefix ExitLag actually runs in |

Keeping the baseline matters: several conclusions in this project came from
diffing behaviour between a clean prefix and the working one.

## What is deliberately not done

- **The system Wine is never modified.** Every fix is project-local or
  prefix-local.
- **No winetricks components are installed speculatively.** Only fixes for
  reproduced failures.
- **ExitLag's binaries are not patched.** The only file replaced is
  `nfapi.dll`, which is the documented interception interface — the client's
  own code is untouched.
- **ExitLag's updater is not disabled.** See `docs/exitlag-updates.md`.
