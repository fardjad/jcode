#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
recipe="$(sed -n '/^install-patched-version:/,/^# Apply one patch/p' "$repo_root/justfile")"

test "$(grep -F -c 'install_dir="${JCODE_INSTALL_DIR:-$HOME/.local/bin}"' <<<"$recipe")" -eq 1
test "$(grep -F -c 'JCODE_INSTALL_DIR="$install_dir" ./scripts/install_release.sh --fast' <<<"$recipe")" -eq 1
test "$(grep -F -c 'isolate_config.py' <<<"$recipe")" -eq 0
test "$(grep -F -c 'config.toml' <<<"$recipe")" -eq 0

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cat > "$tmp/fake-install-release.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "${JCODE_INSTALL_DIR:?}" > "${DESTINATION_LOG:?}"
EOF
chmod +x "$tmp/fake-install-release.sh"

# Execute the recipe's destination expression with a disposable HOME. The
# resulting path must remain under that HOME rather than a temporary wrapper
# HOME or the repository.
(
  export HOME="$tmp/home"
  unset JCODE_INSTALL_DIR
  install_dir="${JCODE_INSTALL_DIR:-$HOME/.local/bin}"
  DESTINATION_LOG="$tmp/default-destination" \
    JCODE_INSTALL_DIR="$install_dir" "$tmp/fake-install-release.sh" --fast
)
test "$(cat "$tmp/default-destination")" = "$tmp/home/.local/bin"

custom_destination="$tmp/custom-bin"
(
  export HOME="$tmp/home"
  export JCODE_INSTALL_DIR="$custom_destination"
  install_dir="${JCODE_INSTALL_DIR:-$HOME/.local/bin}"
  DESTINATION_LOG="$tmp/custom-destination" \
    JCODE_INSTALL_DIR="$install_dir" "$tmp/fake-install-release.sh" --fast
)
test "$(cat "$tmp/custom-destination")" = "$custom_destination"

echo "install-patched-version destination regression test passed"
