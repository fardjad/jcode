#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
recipe="$(sed -n '/^ensure-personal-assets:/,/^# Validate catalog patch metadata/p' "$repo_root/justfile")"

test "$(grep -F -c 'ensure_symlink "$repo_root/plugins" "$jcode_home/plugins"' <<<"$recipe")" -eq 1
test "$(grep -F -c 'ensure_symlink "$repo_root/workers/blueprints" "$jcode_home/worker-blueprints"' <<<"$recipe")" -eq 1
test "$(grep -F -c 'uv lock --check --directory "$repo_root/plugins/delegation-efficiency"' <<<"$recipe")" -eq 1
test "$(grep -F -c 'uv sync' <<<"$recipe")" -eq 0

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
home="$tmp/home"
mkdir -p "$home"

HOME="$home" JCODE_HOME="$home/.jcode" \
  just --justfile "$repo_root/justfile" ensure-personal-assets

test -L "$home/.jcode/plugins"
test "$(readlink "$home/.jcode/plugins")" = "$repo_root/plugins"
test -L "$home/.jcode/worker-blueprints"
test "$(readlink "$home/.jcode/worker-blueprints")" = "$repo_root/workers/blueprints"
test -x "$home/.jcode/plugins/delegation-efficiency/record.py"
test -f "$home/.jcode/worker-blueprints/coordinator.md"

# A second run confirms the command is idempotent.
HOME="$home" JCODE_HOME="$home/.jcode" \
  just --justfile "$repo_root/justfile" ensure-personal-assets

echo "ensure-personal-assets recipe test passed"
