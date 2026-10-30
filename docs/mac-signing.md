# Mac signing status

Current installer packages are unsigned and not notarized. The native app has an
ad-hoc signature. Signing is optional for sharing a clearly labelled experimental
source project, but Developer ID signing and notarization are needed for the
normal identified-developer distribution experience under default Gatekeeper.

See [Apple's Developer ID documentation](https://developer.apple.com/developer-id/).
No suitable signing identity is available in the development keychain, and this
project does not supply an Apple Developer account or certificate.

A signed distribution needs an authorized Developer ID Application identity for
the native app and bundled Mach-O components, a Developer ID Installer identity
for the packages, hardened-runtime validation, submission using `notarytool`, and
stapled tickets verified on a clean Mac. Private keys and notarization credentials
must stay outside the source tree. The developer identity in a distributed
certificate is public; choose the intended publishing identity before signing.

An unsigned alpha must say so plainly. Do not claim Apple validation and do not
instruct users to disable Gatekeeper globally. Treat signing and testing as a
separate release step when the proper identity is available.

## Prepared build controls

`macos/app/build.sh` accepts these optional environment variables:

```sh
export EXITLAG_APP_SIGN_IDENTITY='Developer ID Application: YOUR PUBLISHER (TEAMID)'
export EXITLAG_INSTALLER_SIGN_IDENTITY='Developer ID Installer: YOUR PUBLISHER (TEAMID)'
export EXITLAG_NOTARY_PROFILE='bridge-notary'
bash macos/app/build.sh
```

Configure the keychain profile yourself using Apple's credential workflow; do not
put credentials in this file or shell history. Both signing identities are
required together. Without them the build remains an explicitly unsigned alpha.
The staged Mach-O executables/modules are signed before the app is sealed and
the package is built. An optional notary profile submits each package, requires
Apple's Accepted status, staples the ticket and validates it. Rejection fails the
build; it cannot be reported as successful notarization.

The signing traversal and ad-hoc refusal safeguards have tests. A local ad-hoc
smoke test verified nested component and app signatures. Production Developer ID
signing and Apple's notary service remain untested because no identity/profile
is available here. The code being prepared does not imply Apple approval.
