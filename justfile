# List orphan catalog workflow recipes.
[default]
help:
  #!/usr/bin/env bash
  set -euo pipefail

  just --list --justfile "$(git rev-parse --show-toplevel)/justfile"

# Symlink catalog plugins and worker blueprints into their default jcode paths.
ensure-personal-assets:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  jcode_home="${JCODE_HOME:-$HOME/.jcode}"

  ensure_symlink() {
    local source=$1
    local destination=$2
    mkdir -p "$(dirname "$destination")"
    if [[ -e "$destination" && ! -L "$destination" ]]; then
      printf 'refusing to replace non-symlink path: %s\n' "$destination" >&2
      exit 1
    fi
    if [[ -L "$destination" ]]; then
      rm "$destination"
    fi
    ln -s "$source" "$destination"
    printf 'linked %s -> %s\n' "$destination" "$source"
  }

  ensure_symlink "$repo_root/plugins" "$jcode_home/plugins"
  ensure_symlink "$repo_root/workers/blueprints" "$jcode_home/worker-blueprints"

  test -x "$jcode_home/plugins/rtk/rtk-transform"
  test -x "$jcode_home/plugins/delegation-guard/delegation-guard-transform"
  test -f "$jcode_home/worker-blueprints/coordinator.md"
  test -f "$jcode_home/worker-blueprints/fixer.md"
  test -f "$jcode_home/worker-blueprints/investigator.md"
  test -f "$jcode_home/worker-blueprints/research.md"
  printf 'plugins and worker blueprints are ready\n'

  # The plugins only run when [hooks] points at them.
  config="$jcode_home/config.toml"
  hooks=""
  if [[ -f "$config" ]]; then
    hooks=$(awk '/^[[:space:]]*\[/{f=($0 ~ /^[[:space:]]*\[hooks\][[:space:]]*(#.*)?$/);next} f' "$config")
  fi
  missing=0
  check_hook() {
    local key=$1
    local target=$2
    if ! grep -E "^[[:space:]]*${key}[[:space:]]*=" <<<"$hooks" | grep -F -q "$target"; then
      printf 'missing [hooks] %s entry for %s in %s\n' "$key" "$target" "$config" >&2
      missing=1
    fi
  }
  check_hook pre_tool_transform "plugins/rtk/rtk-transform"
  check_hook post_tool_transform "plugins/delegation-guard/delegation-guard-transform"
  if [[ "$missing" -ne 0 ]]; then
    printf 'hooks are not configured; run the /ensure-personal-jcode-setup skill\n' >&2
    exit 1
  fi
  printf 'rtk and delegation-guard hooks are configured\n'

# Validate catalog patch metadata and synthetic application.
validate-patch-files:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/isolate_config.py" -- \
    python3 "$repo_root/scripts/validate_patches.py"

# Check each patch applies on the pinned release with only its declared dependencies.
check-patch-isolation:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/isolate_config.py" -- \
    python3 "$repo_root/scripts/check_patch_isolation.py"

_bootstrap-nextest:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/isolate_config.py" -- \
    python3 "$repo_root/scripts/bootstrap_nextest.py"

# Create/reset persistent patched copy from VERSION.txt's upstream release.
create-patched-copy:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  base=$(python3 "$repo_root/scripts/version_base.py" resolve)
  worktree="$repo_root/.patched-jcode"

  python3 "$repo_root/scripts/isolate_config.py" -- \
    python3 "$repo_root/scripts/validate_patches.py"
  worktree=$(python3 "$repo_root/scripts/patch_worktree.py" create patched-jcode "$base" --path "$worktree")
  trap 'printf "workflow failed; worktree retained: %s\ncleanup: git worktree remove --force %q\n" "$worktree" "$worktree" >&2' ERR

  python3 "$repo_root/scripts/apply_patches.py" "$worktree"

  trap - ERR
  printf 'patches applied; worktree retained: %s\n' "$worktree"

# Apply every patch, compile the complete patched workspace, and run its
# compatibility suite. Use this after upstream changes and before publishing
# catalog updates, because patch application alone cannot detect Rust errors.
# Validate the complete patched workspace and its compatibility suite.
validate-patched-copy:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  worktree="$repo_root/.patched-jcode"

  just --justfile "$repo_root/justfile" create-patched-copy
  (
    cd "$worktree"
    python3 "$repo_root/scripts/isolate_config.py" -- cargo check --workspace
  )
  base=$(python3 "$repo_root/scripts/version_base.py" resolve)
  just --justfile "$repo_root/justfile" _fast-test "$worktree" "$base"
  printf 'patched workspace validated: %s\n' "$worktree"

# Regenerate every catalog patch and normalize personal From hashes.
snapshot-patches *args:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/isolate_config.py" -- \
    python3 "$repo_root/scripts/snapshot_patches.py" {{args}}
  python3 "$repo_root/scripts/isolate_config.py" -- \
    python3 "$repo_root/scripts/normalize_patch.py"

# Runs scripts/test_*, plugins/*/test_*, workers/tests/test_*, and evaluator pytest suite under isolation, except
# test_install_patched_version_recipe.sh, which does a full release build and
# resets .patched-jcode; run that one by hand when changing the install recipe.
# Run the catalog's own script and plugin tests.
test-catalog:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  cd "$repo_root"
  failed=()
  for test in scripts/test_* plugins/*/test_* workers/tests/test_*; do
    case "$test" in
      scripts/test_install_patched_version_recipe.sh)
        printf 'skip %s (full release build)\n' "$test"
        continue
        ;;
      *.py) runner=python3 ;;
      *.sh) runner=bash ;;
      *) continue ;;
    esac
    if python3 scripts/isolate_config.py -- "$runner" "$test" >/dev/null 2>&1; then
      printf 'ok   %s\n' "$test"
    else
      printf 'FAIL %s\n' "$test"
      failed+=("$test")
    fi
  done
  if [[ ! -x "$repo_root/evaluator/.venv/bin/pytest" ]]; then
    printf 'evaluation tests need: cd evaluator && uv sync --extra eval\n' >&2
    exit 1
  fi
  if (cd "$repo_root/evaluator" && python3 "$repo_root/scripts/isolate_config.py" -- "$repo_root/evaluator/.venv/bin/pytest" -q) >/dev/null 2>&1; then
    printf 'ok   evaluator/tests\n'
  else
    printf 'FAIL evaluator/tests\n'
    failed+=("evaluator/tests")
  fi
  if (( ${#failed[@]} )); then
    printf 'rerun to see output: python3 scripts/isolate_config.py -- <runner> %s\n' "${failed[0]}" >&2
    exit 1
  fi

# Run paid live real-code evaluations with coordinator/worker and judge calls.
# Automatically builds or reuses the exact VERSION.txt base plus every current
# catalog patch. Ignored logs/stamps live under .tools/worker-eval/builds/;
# inspect the reported build.log and build-status.json after a failed build.
# Paid live provider/judge runs; deliberately separate from test-catalog.
eval-workers scenario="":
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  scenario_args=()
  if [[ -n "{{scenario}}" ]]; then
    scenario_args=(--scenario "{{scenario}}")
  fi
  python3 "$repo_root/scripts/isolate_config.py" -- \
    python3 "$repo_root/scripts/eval_workers.py" "${scenario_args[@]}"

# Print the commit-to-patch mapping for .patched-jcode.
list-patches *args:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/isolate_config.py" -- \
    python3 "$repo_root/scripts/snapshot_patches.py" --list {{args}}

# Refresh patched copy, then install its fast release build.
install-patched-version:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  worktree="$repo_root/.patched-jcode"
  install_dir="${JCODE_INSTALL_DIR:-$HOME/.local/bin}"
  just --justfile "$repo_root/justfile" create-patched-copy
  (
    cd "$worktree"
    # rust-objcopy cannot load libLLVM on macOS, so disable release stripping.
    if [[ "$(uname -s)" == Darwin ]]; then
      export CARGO_PROFILE_RELEASE_STRIP=none
    fi
    JCODE_INSTALL_DIR="$install_dir" ./scripts/install_release.sh --fast
  )

# Apply one patch in a clean worktree and run its validation/tests.
test-patch-file patch:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  patch_name=$(basename "{{patch}}")
  name="test-${patch_name%.patch}"

  base=$(python3 "$repo_root/scripts/version_base.py" resolve)
  python3 "$repo_root/scripts/isolate_config.py" -- \
    python3 "$repo_root/scripts/validate_patches.py"
  worktree=$(python3 "$repo_root/scripts/patch_worktree.py" create "$name" "$base")
  trap 'printf "workflow failed; worktree retained: %s\ncleanup: git worktree remove --force %q\n" "$worktree" "$worktree" >&2' ERR

  python3 "$repo_root/scripts/apply_patches.py" "$worktree" "$patch_name"
  python3 "$repo_root/scripts/validate_patch_commands.py" "$worktree" "$patch_name" --base "$base"
  just --justfile "$repo_root/justfile" _fast-test "$worktree" "$base"

  trap - ERR
  printf 'patch passed; worktree retained: %s\n' "$worktree"

# Run test-patch-file for every patch, resumably, with per-patch logs.
test-all-patches *args:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/isolate_config.py" -- \
    python3 "$repo_root/scripts/run_all_patch_tests.py" {{args}}

# Create an upstream-candidate branch from one candidate patch.
create-upstream-candidate-branch-from patch:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  patch_name=$(basename "{{patch}}")
  slug=${patch_name%.patch}
  slug=${slug#*-}
  slug=${slug#candidate-}
  name="candidate-${patch_name%.patch}"
  branch="upstream-candidate/$slug"

  base=$(python3 "$repo_root/scripts/version_base.py" resolve)
  python3 "$repo_root/scripts/isolate_config.py" -- \
    python3 "$repo_root/scripts/validate_patches.py"
  worktree=$(python3 "$repo_root/scripts/patch_worktree.py" create "$name" "$base")
  trap 'printf "workflow failed; worktree retained: %s\ncleanup: git worktree remove --force %q\n" "$worktree" "$worktree" >&2' ERR

  python3 "$repo_root/scripts/apply_patches.py" "$worktree" "$patch_name" --require-kind upstream-candidate
  python3 "$repo_root/scripts/validate_patch_commands.py" "$worktree" "$patch_name" --base "$base"
  python3 "$repo_root/scripts/candidate_branch.py" "$worktree" "$branch"
  python3 "$repo_root/scripts/patch_worktree.py" cleanup "$name" --path "$worktree"

  trap - ERR
  printf 'candidate ready: %s\n' "$branch"

# Learn compatibility exclusions from a clean worktree at the pinned release.
# This is intentionally separate from patched validation so patch failures
# cannot be learned or hidden.
# Learn exclusions only from clean upstream compatibility test runs.
learn-upstream-exclusions:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  base=$(python3 "$repo_root/scripts/version_base.py" resolve)
  worktree=$(python3 "$repo_root/scripts/patch_worktree.py" create clean-upstream "$base")
  trap 'printf "clean-upstream learning failed; worktree retained: %s\ncleanup: git worktree remove --force %q\n" "$worktree" "$worktree" >&2' ERR

  just --justfile "$repo_root/justfile" _learn-tests "$worktree" "$base"
  python3 "$repo_root/scripts/validate_patch_commands.py" learn "$worktree" --base "$base"
  python3 "$repo_root/scripts/patch_worktree.py" cleanup clean-upstream --path "$worktree"
  trap - ERR
  printf 'learned clean-upstream exclusions for: %s\n' "$base"

# Fetches the tag, refuses a moved tag when given the reviewed commit hash, and
# writes VERSION.txt. It does not validate patches: fix them on the new base,
# finish with learn-upstream-exclusions and validate-patched-copy, and commit
# the pin with the patch changes.
# Pin the catalog to a canonical jcode stable release.
set-version release expected="":
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  release="{{release}}"
  expected="{{expected}}"
  current_branch=$(git branch --show-current)

  if [[ "$current_branch" != personalized ]]; then
    printf 'set-version must run on personalized; current branch: %s\n' "$current_branch" >&2
    exit 1
  fi
  if [[ -n "$(git status --porcelain)" ]]; then
    printf 'set-version requires clean personalized checkout\n' >&2
    exit 1
  fi
  [[ "$release" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    printf 'set-version requires a stable vX.Y.Z release tag\n' >&2
    exit 1
  }
  # Always refetch so a tag that moved since review is detected.
  selected_base=$(python3 "$repo_root/scripts/version_base.py" resolve "$release" --fetch)
  printf 'canonical release: %s\nrelease commit: %s\n' "$release" "$selected_base"
  if [[ -n "$expected" ]]; then
    [[ "$expected" =~ ^[0-9a-f]{7,40}$ && "$selected_base" == "$expected"* ]] || {
      printf 'release commit %s does not match reviewed hash %s\n' "$selected_base" "$expected" >&2
      exit 1
    }
  fi
  python3 "$repo_root/scripts/version_base.py" write "$release"
  printf 'VERSION.txt now pins %s; revert with: git checkout -- VERSION.txt\n' "$release"

# Push catalog and candidate branches to origin.
push:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  current_branch=$(git branch --show-current)
  if [[ "$current_branch" != "personalized" ]]; then
    printf 'push must run on personalized; current branch: %s\n' "$current_branch" >&2
    exit 1
  fi
  refs=(
    refs/heads/personalized:refs/heads/personalized
  )
  while IFS= read -r ref; do
    branch=${ref#refs/heads/}
    refs+=("$ref:refs/heads/$branch")
  done < <(git for-each-ref --format='%(refname)' refs/heads/upstream-candidate/)

  git -C "$repo_root" push origin "${refs[@]}"

# Learn clean-upstream compatibility failures into ignored, base-specific state.
_learn-tests worktree base:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/run_nextest.py" learn "{{worktree}}" --base "{{base}}"

# Run compatibility suite using learned exclusions for base.
_fast-test worktree base:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/run_nextest.py" test "{{worktree}}" --base "{{base}}"
