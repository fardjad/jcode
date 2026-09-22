#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
recipe="$(sed -n '/^install-patched-version:/,/^# Apply one patch/p' "$repo_root/justfile")"

test "$(grep -F -c 'just --justfile "$repo_root/justfile" install-patched-version' <<<"$recipe")" -eq 0
test "$(grep -F -c 'JCODE_INSTALL_DIR="$install_dir" ./scripts/install_release.sh --fast' <<<"$recipe")" -eq 1
test "$(grep -F -c 'isolate_config.py' <<<"$recipe")" -eq 0
test "$(grep -F -c 'config.toml' <<<"$recipe")" -eq 0
test "$(grep -F -c 'JCODE_PLUGIN_DIR' <<<"$recipe")" -eq 0
test "$(grep -F -c 'ensure-personal-assets' <<<"$recipe")" -eq 0

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
home="$tmp/home"
install_dir="$tmp/bin"
rustup_home="${RUSTUP_HOME:-$HOME/.rustup}"
cargo_home="${CARGO_HOME:-$HOME/.cargo}"
mkdir -p "$home"

# Execute the real recipe with disposable configuration and install targets.
# The release build may use the invoking cargo/rustup caches, but all jcode and
# XDG paths remain inside this temporary HOME.
(
  export HOME="$home"
  unset JCODE_HOME XDG_CONFIG_HOME XDG_CACHE_HOME XDG_DATA_HOME XDG_STATE_HOME
  export RUSTUP_HOME="$rustup_home"
  export CARGO_HOME="$cargo_home"
  export JCODE_INSTALL_DIR="$install_dir"
  export JCODE_RELEASE_PROFILE=release
  export JCODE_SKIP_SERVER_RELOAD=1
  just --justfile "$repo_root/justfile" install-patched-version
)

test -x "$install_dir/jcode"
test ! -f "$home/.jcode/config.toml"

echo "install-patched-version isolated end-to-end test passed"
