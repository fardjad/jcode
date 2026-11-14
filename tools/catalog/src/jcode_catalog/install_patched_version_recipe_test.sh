#!/usr/bin/env bash
# Explicit expensive acceptance check. Never included in the catalog suite.
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "$repo_root"
bun tools/scripts/isolate_config.ts -- bash -euo pipefail -c '
export JCODE_INSTALL_DIR="$HOME/bin"
export JCODE_RELEASE_PROFILE=release
export JCODE_SKIP_SERVER_RELOAD=1
bun tools/catalog/src/cli.ts install
test -f "$JCODE_INSTALL_DIR/jcode"
test ! -e "$JCODE_HOME/config.toml"
'
