#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
recipe="$(sed -n '/^install-patched-version:/,/^# Apply one patch/p' "$repo_root/justfile")"

test "$(grep -F -c 'just --justfile "$repo_root/justfile" install-patched-version' <<<"$recipe")" -eq 0
test "$(grep -F -c 'JCODE_INSTALL_DIR="$install_dir" ./scripts/install_release.sh --fast' <<<"$recipe")" -eq 1
test "$(grep -F -c 'isolate_config.py' <<<"$recipe")" -eq 0
test "$(grep -F -c 'config.toml' <<<"$recipe")" -eq 0
test "$(grep -F -c 'plugins/delegation-efficiency/install.sh' <<<"$recipe")" -eq 1
test "$(grep -F -c 'JCODE_PLUGIN_DIR:-$HOME/.jcode/plugins/delegation-efficiency' <<<"$recipe")" -eq 1
test "$(grep -F -c 'plugins/delegation-guard/delegation-guard-transform' <<<"$recipe")" -eq 1
test "$(grep -F -c 'JCODE_DELEGATION_GUARD_PLUGIN:-$HOME/.jcode/plugins/delegation-guard-transform' <<<"$recipe")" -eq 1

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
home="$tmp/home"
install_dir="$tmp/bin"
plugin_dir="$home/.jcode/plugins/delegation-efficiency"
guard_plugin="$home/.jcode/plugins/delegation-guard-transform"
state_dir="$tmp/state"
outside="$tmp/outside"
mkdir -p "$home" "$outside"

# Execute the real recipe with disposable configuration and install targets.
# The release build may use the invoking cargo/rustup caches, but all jcode and
# XDG paths remain inside this temporary HOME.
(
  export HOME="$home"
  unset JCODE_HOME XDG_CONFIG_HOME XDG_CACHE_HOME XDG_DATA_HOME XDG_STATE_HOME
  export JCODE_INSTALL_DIR="$install_dir"
  export JCODE_PLUGIN_DIR="$plugin_dir"
  export JCODE_RELEASE_PROFILE=release
  export JCODE_SKIP_SERVER_RELOAD=1
  just --justfile "$repo_root/justfile" install-patched-version
)

test -x "$install_dir/jcode"
test -x "$plugin_dir/record.py"
test -x "$plugin_dir/report.py"
test -x "$guard_plugin"
test ! -f "$home/.jcode/config.toml"

guard_output="$(env -i HOME="$home" PATH="$PATH" \
  JCODE_HOOK_SWARM_ENABLED=1 JCODE_HOOK_PROCESS_ROLE=coordinator \
  "$guard_plugin" <<<'{"output_bytes":8193,"tool_result_file":"/tmp/full-result"}')"
python3 - "$guard_output" <<'PY'
import json
import sys
output = json.loads(sys.argv[1])
assert "8193 bytes" in output["tool_result"]
assert "/tmp/full-result" in output["tool_result"]
PY

envelope='{"envelope":"jcode.delegation-efficiency.v1","schema_version":"1.0","semantics_version":"part1-runtime-1","event":"tool_result","event_id":"installed-round-trip","occurred_at_unix_ms":1700000000000,"session_id":"installed-session","tool_invocation_id":"installed-tool","tool_name":"read","evidence_level":"measured","tokenizer_identity":"chars_div_4","tokenizer_status":"approximate","evidence":"measured","attribution_status":"not_applicable","tool_latency_ms":1}'
(
  cd "$outside"
  env -i HOME="$home" PATH="$PATH" DELEGATION_EFFICIENCY_STATE_DIR="$state_dir" \
    "$plugin_dir/record.py" <<<"$envelope" | grep -F '"status":"inserted"'
  env -i HOME="$home" PATH="$PATH" DELEGATION_EFFICIENCY_STATE_DIR="$state_dir" \
    "$plugin_dir/report.py" --rollup > "$tmp/report.json"
)
python3 - "$state_dir" "$tmp/report.json" <<'PY'
import json
import pathlib
import sqlite3
import sys
state = pathlib.Path(sys.argv[1])
report = json.loads(pathlib.Path(sys.argv[2]).read_text())
assert report["scope"] == "aggregate"
db = sqlite3.connect(state / "delegation-efficiency.sqlite3")
assert db.execute("select count(*) from events where event_id = 'installed-round-trip'").fetchone()[0] == 1
PY

echo "install-patched-version isolated end-to-end test passed"
