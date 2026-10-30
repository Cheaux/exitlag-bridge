# Technical provenance review

Review date: 2026-10-30. This is a technical source audit, not legal clearance.

## Evidence available

| Component | Observable implementation | Remaining scope |
|---|---|---|
| `backend/exitlag-netd.c` | Native libmnl/netfilter backend and the project's EXL1 IPC protocol | System libmnl retains its LGPL license. |
| `shim/nfapi/nfapi_stub.cpp` | Project-specific IPC, callback dispatch and Wine compatibility logic; no vendor SDK include or linked vendor implementation | API names, packed structures and vtable ordering reproduce an external interface. Their complete original derivation is not recorded. |
| `shim/nfextlag/nfextlag_stub.c` | Import-free presence stub returning success, without a filtering implementation | Compatibility version metadata does not make this an official vendor driver. |
| `wine-wbemprox/patches/` | Explicit patch against pinned upstream Wine source | LGPL notices and corresponding patched source accompany binary releases. |
| Embedded Python | Unmodified pinned upstream runtime | Upstream notices are retained in `LICENSES/python-standalone/`. |

The rule logging code explicitly references `nfdriver_data.h` version 1.7.7.1
and a 643-byte packed rule structure. That reference means import/export inspection alone is insufficient evidence
for the blanket derivation claim. It does not establish that an SDK
implementation was copied. Available source history
starts from an imported snapshot; it does not reconstruct earlier authoring.
No copied vendor SDK source package or header file is present, but that observation
alone does not establish independent derivation of every declaration.

Audited implementation fingerprints:

```text
shim/nfapi/nfapi_stub.cpp
9da1a973d4498acdbdbfcabbb5cf443d8c74919071f2afed4b5ba85eb654d22d
shim/nfextlag/nfextlag_stub.c
68ada2c8293a90f36293f4be73f0dc44fb8df17139ad90f291997f8034e00ccd
backend/exitlag-netd.c
69d2be7f1a9896fcbe1bdb5376ff06a1460cda22b7492387fa00132e49d0bf02
```

## External terms checked

- [NetFilter SDK license](https://www.netfiltersdk.com/license.html): different
  conditions apply to executables and substantially derived SDK source. The
  applicability of that source condition to this shim has not been established.
- [ExitLag website/service terms](https://www.exitlag.com/terms-of-service?language=english):
  do not establish permission to redistribute the proprietary client or modify
  every component. They are not a substitute for reviewing any installer/client
  agreement accepted by the user. No separate publisher EULA file was identified
  in the installed application; third-party notices are not publisher approval.

The project distributes no official ExitLag binary or account. Users supply their
own installer and valid account. That avoids redistributing the client but does
not automatically answer the interface provenance question.

## Distribution scope

The commercial NetFilter SDK is not a build/runtime dependency of the bridge's
native filtering implementation and is not included in project downloads. The
official ExitLag installer is obtained separately by each user and may contain
its own vendor components. Our installed replacement `nfapi.dll` imports only
standard Windows/Wine system libraries; `nfextlag.sys` is a project-built stub.
Linux's open-source netfilter/nftables/NFQUEUE is a different project from the
commercial SDK vendor.

This release does not claim that matching an API automatically requires an SDK
license or vendor permission. No SDK implementation was identified in the audited
source. The remaining declaration-origin gap is documented for transparency;
this experimental release is not a legal opinion or vendor endorsement.

## Further provenance evidence

Obtain the earlier authoring record showing how the ABI declarations were
established, and review the relevant terms against it; or obtain appropriate
written clarification from the rights holder / qualified reviewer. A review
should identify the exact source revision and scope. Do not record approval on
assumption, and do not treat cosmetic rewrites as proof of independent origin.

No external inquiry has been sent and no legal opinion or vendor permission has
been obtained by this audit. Such permission has not been established as a
requirement merely because the implementation matches this interface.
