# Contributing

This is an unofficial compatibility project. Game recognition in ExitLag's UI is not sufficient evidence of routing. Report the exact scope tested: launch, login, character selection, actual gameplay, real bidirectional relay traffic, restart, or cleanup.

Compatibility reports should include OS and CPU architecture, game/build and launch method, ExitLag and Wine versions, transport, result, and a short reproducible sequence. Use the game-compatibility issue template. Describe untested behavior explicitly; a skipped test is not a pass.

Submit code and profile changes through pull requests for maintainer review. Include the relevant local tests and explain any change to process attribution, authentication, routing scope, privileges or cleanup. Do not broaden interception or claim game support solely to make a dashboard connection appear.

## Privacy

Never commit account data, passwords, tokens, Wine prefixes, game account files, private keys, screenshots, raw logs or diagnostic dumps. Keep runtime state outside the checkout. Examples must use placeholders. Review staged files and commit metadata before pushing. Use a GitHub noreply author and committer email; the source privacy check rejects other email metadata. Redact diagnostics before attaching them to an issue; do not upload a whole prefix or account directory.

## Validation

- Linux: see `docs/testing.md` and `docs/test-matrix.md`; unit checks can run without an ExitLag account.
- macOS: see `macos/README.md`; loopback tests do not modify the firewall. The native launcher is in `macos/app/`.
- Live tests require the contributor's own official ExitLag client and account. The project never supplies vendor account access or bypasses subscription checks.

Contributions to first-party code use GPL-3.0-only. The interface review documented in `THIRD_PARTY.md` remains a separate release item.
