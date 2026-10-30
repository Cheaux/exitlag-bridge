# Third-party components and provenance

Everything in this repository is first-party unless listed below. This records
what is derived from someone else's work, what obligation that carries, and —
where redistribution status is not certain — says so instead of guessing.

## Summary

| Component | Where | Licence | Status |
|---|---|---|---|
| Wine (`wbemprox` patch) | `wine-wbemprox/patches/` | LGPL-2.1-or-later | distributable with notice |
| NetFilter SDK `nfapi` ABI | `shim/nfapi/nfapi_stub.cpp`, `shim/nfapi/*.def` | unknown | **uncertain — see below** |
| libmnl | linked by `backend/exitlag-netd` | LGPL-2.1-or-later | not redistributed; system library |
| ExitLag client | not present | proprietary | **never redistributed** |
| Standalone CPython | embedded in the Mac helper | PSF-2.0 and upstream dependency licenses | upstream notices in `LICENSES/python-standalone/` |

## Wine — `wbemprox` patch

`wine-wbemprox/patches/0001-wbemprox-add-logicaldisk-root-directory.patch` is a
diff against Wine, which is licensed **LGPL-2.1-or-later**. A patch is a
derivative of the code it modifies, so the notice in `LICENSES/LGPL-2.1.txt`
applies to it.

No full Wine source tree is committed here. `installers/build_runtime.py` fetches
pinned upstream source, applies the patch and builds the replacement WMI DLL.
It also generates a corresponding-source archive that must accompany binary
releases. The full Wine runtimes are downloaded from upstream during ordinary-user
setup; no Wine runtime is uploaded from a developer's configured prefix.

Upstream: <https://gitlab.winehq.org/wine/wine>

## NetFilter SDK — the `nfapi` interface

**This is the one item whose redistribution status is not settled, and it should
be reviewed by someone qualified before this project is published.**

What is present:

- structure layouts (`_NF_RULE_EX`, `_NF_UDP_OPTIONS`, `_NF_FLOWCTL_DATA`,
  `_NF_FLOWCTL_STAT`, `_NF_PORT_RANGE`, `NF_EventHandler`)
- the decorated C++ export names of the `nfapi` functions
- an event-handler vtable ordering

The checkout contains no SDK source package or vendor header file and does not
link a vendor SDK implementation. The shim contains its own loopback IPC,
callback dispatch, compatibility guards and native-backend integration.

The original derivation of the interface declarations is **not fully evidenced**.
In particular, `rule_line` cites `nfdriver_data.h` version 1.7.7.1 and the
643-byte packed rule layout. Import/export tables alone do not establish the
origin of every structure field. Earlier authoring records were not supplied.
The absence of a copied SDK file is not proof that every declaration was derived
independently, and this document does not grant legal clearance.

The vendor's [license](https://www.netfiltersdk.com/license.html) distinguishes
executable redistribution from substantially derived source redistribution.
Whether that restriction applies to this interoperability shim remains an open
review question. Renaming declarations or generating equivalent text would not
by itself resolve their provenance. See [the technical provenance review](docs/provenance-review.md)
for concrete evidence and the question that remains.

Vendor: NetFilter SDK — <https://netfiltersdk.com/>

## libmnl

`backend/exitlag-netd` links against **libmnl** (LGPL-2.1-or-later) to speak
netlink to NFQUEUE. No libmnl source or binary is included; it is a system
dependency the user installs. Dynamic linking against an LGPL library imposes no
obligation on this project's own source.

Upstream: <https://netfilter.org/projects/libmnl/>

## ExitLag

The official ExitLag client is **proprietary and is never included, mirrored, or
redistributed by this project**, in whole or in part. No ExitLag binary,
resource, icon, string table or data file is present in this repository.

Users must obtain the client from ExitLag and hold their own account and
subscription. This project is not affiliated with or endorsed by ExitLag.

## Toolchain

Build-time only, not redistributed: `clang-cl`, `lld-link`, `llvm-dlltool`,
`llvm-readobj`, `llvm-undname` (LLVM, Apache-2.0 with LLVM exceptions), a C
compiler, GNU make, `nftables`, and Python 3 for the test harnesses. Zig 0.17.0 is
a verified build-only dependency; the toolchain is not shipped in installers.

## Embedded Python

The Mac helper embeds unmodified CPython 3.13.18 from Astral's
`python-build-standalone` release 20261026. Runtime archives are pinned by SHA-256
in `installers/bridge_setup/catalog.json`. Python's own license and bundled pip
notices remain in the runtime. The complete upstream notice collection is
retained in `LICENSES/python-standalone/`, including notices for native runtime
dependencies. Its provenance file records the exact upstream source commit.
These components retain their upstream terms and are not relicensed as GPL.

## First-party code

First-party code under `backend/`, `shim/`, `tools/`, `tests/`, `macos/` and `installers/` is GPL-3.0-only. See `LICENSE`. Wine-derived code and third-party runtime components retain their own licenses.
