#!/usr/bin/env bash
# Explicit expensive acceptance check. Never included in the catalog suite.
set -euo pipefail
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "$repo_root"
python3 tools/scripts/isolate_config.py -- uv run --project tools/catalog python -c '
import os, subprocess
from pathlib import Path
os.environ["JCODE_INSTALL_DIR"] = str(Path(os.environ["HOME"]) / "bin")
os.environ["JCODE_RELEASE_PROFILE"] = "release"
os.environ["JCODE_SKIP_SERVER_RELOAD"] = "1"
subprocess.run(["jcode-catalog", "install"], check=True)
assert (Path(os.environ["JCODE_INSTALL_DIR"]) / "jcode").is_file()
assert not (Path(os.environ["JCODE_HOME"]) / "config.toml").exists()
'
