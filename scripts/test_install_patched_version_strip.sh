#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
recipe="$(sed -n '/^install-patched-version:/,/^# Apply one patch/p' "$repo_root/justfile")"

test "$(grep -F -c 'just --justfile "$repo_root/justfile" install-patched-version' <<<"$recipe")" -eq 0
test "$(grep -F -c 'JCODE_INSTALL_DIR="$install_dir" ./scripts/install_release.sh --fast' <<<"$recipe")" -eq 1
test "$(grep -F -c 'export CARGO_PROFILE_RELEASE_STRIP=none' <<<"$recipe")" -eq 1
test "$(grep -F -c 'isolate_config.py' <<<"$recipe")" -eq 0
test "$(grep -F -c 'config.toml' <<<"$recipe")" -eq 0
test "$(grep -F -c 'JCODE_PLUGIN_DIR' <<<"$recipe")" -eq 0
test "$(grep -F -c 'ensure-personal-assets' <<<"$recipe")" -eq 0

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
fixture="$tmp/repo"
stub_bin="$tmp/bin"
mkdir -p "$fixture/.patched-jcode/scripts" "$stub_bin"

cat >"$tmp/recipe.sh" <<EOF
$(sed -n '/^  set -euo pipefail$/,/^# Apply one patch/{ /^# Apply one patch/d; s/^  //; p; }' <<<"$recipe")
EOF

cat >"$stub_bin/just" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >>"$JCODE_TEST_JUST_LOG"
EOF
cat >"$stub_bin/git" <<'EOF'
#!/usr/bin/env bash
if [[ "$*" == "rev-parse --show-toplevel" ]]; then
  printf '%s\n' "$JCODE_TEST_REPO_ROOT"
else
  exit 2
fi
EOF
cat >"$stub_bin/uname" <<'EOF'
#!/usr/bin/env bash
[[ "$*" == "-s" ]] || exit 2
printf '%s\n' "$JCODE_TEST_UNAME"
EOF
cat >"$fixture/.patched-jcode/scripts/install_release.sh" <<'EOF'
#!/usr/bin/env bash
[[ "$*" == "--fast" ]] || exit 2
printf '%s\n' "${CARGO_PROFILE_RELEASE_STRIP-__UNSET__}" >"$JCODE_TEST_INSTALL_LOG"
EOF
chmod +x "$stub_bin/just" "$stub_bin/git" "$stub_bin/uname" \
  "$fixture/.patched-jcode/scripts/install_release.sh"

run_case() {
  local system="$1" caller_value="$2" expected_installer_value="$3"
  local install_log="$tmp/install-$system-${caller_value//\//_}.log"
  : >"$install_log"
  (
    export PATH="$stub_bin:$PATH"
    export JCODE_TEST_REPO_ROOT="$fixture"
    export JCODE_TEST_UNAME="$system"
    export JCODE_TEST_INSTALL_LOG="$install_log"
    export JCODE_TEST_JUST_LOG="$tmp/just.log"
    export HOME="$tmp/home"
    export JCODE_INSTALL_DIR="$tmp/install"
    mkdir -p "$HOME"
    if [[ "$caller_value" == __UNSET__ ]]; then
      unset CARGO_PROFILE_RELEASE_STRIP
    else
      export CARGO_PROFILE_RELEASE_STRIP="$caller_value"
    fi

    # Source the extracted recipe to observe whether its inner subshell leaks.
    source "$tmp/recipe.sh"
    test "${CARGO_PROFILE_RELEASE_STRIP-__UNSET__}" = "$caller_value"
  )
  test "$(cat "$install_log")" = "$expected_installer_value"
  grep -F -q 'create-patched-copy' "$tmp/just.log"
}

run_case Darwin __UNSET__ none
run_case Darwin symbols none
run_case Darwin debuginfo none
run_case Linux symbols symbols
run_case Linux __UNSET__ __UNSET__

echo "install-patched-version recipe workaround test passed"
