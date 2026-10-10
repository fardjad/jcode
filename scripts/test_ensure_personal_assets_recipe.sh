#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
recipe="$(sed -n '/^ensure-personal-assets:/,/^# Validate catalog patch metadata/p' "$repo_root/justfile")"

test "$(grep -F -c 'ensure_symlink "$repo_root/plugins" "$jcode_home/plugins"' <<<"$recipe")" -eq 1
test "$(grep -F -c 'ensure_symlink "$repo_root/workers/blueprints" "$jcode_home/worker-blueprints"' <<<"$recipe")" -eq 1
test "$(grep -F -c 'uv lock --check' <<<"$recipe" || true)" -eq 0
test "$(grep -F -c 'uv sync' <<<"$recipe" || true)" -eq 0

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
home="$tmp/home"
mkdir -p "$home"

# Without hook configuration the recipe links assets but fails the hook check.
if HOME="$home" JCODE_HOME="$home/.jcode" \
  just --justfile "$repo_root/justfile" ensure-personal-assets 2>"$tmp/err"; then
  echo "expected failure without [hooks] configuration" >&2
  exit 1
fi
grep -F -q 'post_tool_transform' "$tmp/err"
grep -F -q 'pre_tool_transform' "$tmp/err"

# Only the guard configured: still fails, naming the missing RTK hook only.
cat >"$home/.jcode/config.toml" <<'EOF'
[hooks]
post_tool_transform = ["~/.jcode/plugins/delegation-guard/delegation-guard-transform"]

[other]
pre_tool_transform = ["~/.jcode/plugins/rtk/rtk-transform"]
EOF
if HOME="$home" JCODE_HOME="$home/.jcode" \
  just --justfile "$repo_root/justfile" ensure-personal-assets 2>"$tmp/err"; then
  echo "expected failure when pre_tool_transform is outside [hooks]" >&2
  exit 1
fi
grep -F -q 'pre_tool_transform' "$tmp/err"
if grep -F -q 'missing [hooks] post_tool_transform' "$tmp/err"; then
  echo "post_tool_transform was configured but reported missing" >&2
  exit 1
fi

cat >"$home/.jcode/config.toml" <<'EOF'
[hooks]
pre_tool_transform = ["~/.jcode/plugins/rtk/rtk-transform"]
post_tool_transform = ["~/.jcode/plugins/delegation-guard/delegation-guard-transform"]
post_tool_transform_timeout_ms = 500
EOF

HOME="$home" JCODE_HOME="$home/.jcode" \
  just --justfile "$repo_root/justfile" ensure-personal-assets

test -L "$home/.jcode/plugins"
test "$(readlink "$home/.jcode/plugins")" = "$repo_root/plugins"
test -L "$home/.jcode/worker-blueprints"
test "$(readlink "$home/.jcode/worker-blueprints")" = "$repo_root/workers/blueprints"
test -x "$home/.jcode/plugins/rtk/rtk-transform"
test -x "$home/.jcode/plugins/delegation-guard/delegation-guard-transform"
test -f "$home/.jcode/worker-blueprints/coordinator.md"
test -f "$home/.jcode/worker-blueprints/fixer.md"

# A second run confirms the command is idempotent.
HOME="$home" JCODE_HOME="$home/.jcode" \
  just --justfile "$repo_root/justfile" ensure-personal-assets

echo "ensure-personal-assets recipe test passed"
