#!/usr/bin/env bash
set -euo pipefail

cat >&2 <<'EOF'
This deployment method is intentionally disabled. Wine resolves wbemprox from its
builtin module directory, so copying a PE DLL into the prefix does not override it.
The original prefix DLL was restored after that negative test.

Use ../wine-wbemprox/build-local.sh, then ../research/wine-experiments/run-with-local-wbemprox.sh.
That launcher selects a project-local Wine module directory and does not modify the
system Wine installation.
EOF
exit 1
