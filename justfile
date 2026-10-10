# List orphan catalog workflow recipes.
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
validate-patch-files base="":
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  if [[ -n "{{base}}" ]]; then
    python3 "$repo_root/scripts/isolate_config.py" -- \
      python3 "$repo_root/scripts/validate_patches.py" --base "{{base}}"
  else
    python3 "$repo_root/scripts/isolate_config.py" -- \
      python3 "$repo_root/scripts/validate_patches.py"
  fi

_bootstrap-nextest:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/isolate_config.py" -- \
    python3 "$repo_root/scripts/bootstrap_nextest.py"

# Create/reset persistent patched copy from VERSION.txt's upstream release.
create-patched-copy base="":
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  base="{{base}}"
  if [[ -z "$base" ]]; then
    base=$(python3 "$repo_root/scripts/version_base.py" resolve)
  fi
  worktree="$repo_root/.patched-jcode"

  python3 "$repo_root/scripts/isolate_config.py" -- \
    python3 "$repo_root/scripts/validate_patches.py" --base "$base"
  worktree=$(python3 "$repo_root/scripts/patch_worktree.py" create patched-jcode "$base" --path "$worktree")
  trap 'printf "workflow failed; worktree retained: %s\ncleanup: git worktree remove --force %q\n" "$worktree" "$worktree" >&2' ERR

  python3 "$repo_root/scripts/apply_patches.py" "$worktree"

  trap - ERR
  printf 'patches applied; worktree retained: %s\n' "$worktree"

# Apply every patch, compile the complete patched workspace, and run its
# compatibility suite. Use this after upstream changes and before publishing
# catalog updates, because patch application alone cannot detect Rust errors.
validate-patched-copy base="":
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  worktree="$repo_root/.patched-jcode"

  just --justfile "$repo_root/justfile" create-patched-copy "{{base}}"
  (
    cd "$worktree"
    python3 "$repo_root/scripts/isolate_config.py" -- cargo check --workspace
  )
  base="{{base}}"
  if [[ -z "$base" ]]; then
    base=$(python3 "$repo_root/scripts/version_base.py" resolve)
  fi
  just --justfile "$repo_root/justfile" _fast-test "$worktree" "$base"
  printf 'patched workspace validated: %s\n' "$worktree"

# Regenerate every catalog patch and normalize personal From hashes.
snapshot-patches:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/snapshot_patches.py"
  python3 "$repo_root/scripts/normalize_patch.py"

# Zero personal patch From hashes to avoid backing-commit-only diffs.
normalize-patch:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/normalize_patch.py"

# Print the commit-to-patch mapping for .patched-jcode.
list-patches:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/snapshot_patches.py" --list

# One-time: copy patch metadata into .patched-jcode commit messages.
migrate-commit-metadata:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/migrate_commit_metadata.py"

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
    python3 "$repo_root/scripts/validate_patches.py" --base "$base"
  worktree=$(python3 "$repo_root/scripts/patch_worktree.py" create "$name" "$base")
  trap 'printf "workflow failed; worktree retained: %s\ncleanup: git worktree remove --force %q\n" "$worktree" "$worktree" >&2' ERR

  python3 "$repo_root/scripts/apply_patches.py" "$worktree" "$patch_name"
  python3 "$repo_root/scripts/validate_patch_commands.py" "$worktree" "$patch_name"
  just --justfile "$repo_root/justfile" _fast-test "$worktree" "$base"

  trap - ERR
  printf 'patch passed; worktree retained: %s\n' "$worktree"

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
    python3 "$repo_root/scripts/validate_patches.py" --base "$base"
  worktree=$(python3 "$repo_root/scripts/patch_worktree.py" create "$name" "$base")
  trap 'printf "workflow failed; worktree retained: %s\ncleanup: git worktree remove --force %q\n" "$worktree" "$worktree" >&2' ERR

  python3 "$repo_root/scripts/apply_patches.py" "$worktree" "$patch_name" --require-kind upstream-candidate
  python3 "$repo_root/scripts/validate_patch_commands.py" "$worktree" "$patch_name"
  python3 "$repo_root/scripts/candidate_branch.py" "$worktree" "$branch"
  python3 "$repo_root/scripts/patch_worktree.py" cleanup "$name" --path "$worktree"

  trap - ERR
  printf 'candidate ready: %s\n' "$branch"

# Learn compatibility exclusions from a clean worktree at the pinned release.
# This is intentionally separate from patched validation so patch failures
# cannot be learned or hidden.
learn-upstream-exclusions base="":
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  base="{{base}}"
  if [[ -z "$base" ]]; then
    base=$(python3 "$repo_root/scripts/version_base.py" resolve)
  fi
  worktree=$(python3 "$repo_root/scripts/patch_worktree.py" create sync-upstream "$base")
  trap 'printf "clean-upstream learning failed; worktree retained: %s\ncleanup: git worktree remove --force %q\n" "$worktree" "$worktree" >&2' ERR

  just --justfile "$repo_root/justfile" _learn-tests "$worktree" "$base"
  python3 "$repo_root/scripts/patch_worktree.py" cleanup sync-upstream --path "$worktree"
  trap - ERR
  printf 'learned clean-upstream exclusions for: %s\n' "$base"

# Sync to the newest canonical jcode stable release by default, or select a
# specific stable vX.Y.Z tag. VERSION.txt changes only after validation passes.
sync release="":
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  requested_release="{{release}}"
  current_branch=$(git branch --show-current)

  if [[ "$current_branch" != personalized ]]; then
    printf 'sync must run on personalized; current branch: %s\n' "$current_branch" >&2
    exit 1
  fi
  if [[ -n "$(git status --porcelain)" ]]; then
    printf 'sync requires clean personalized checkout\n' >&2
    exit 1
  fi
  if git worktree list --porcelain | grep -Fxq 'branch refs/heads/master'; then
    printf 'refusing to update master: it is checked out in another worktree\n' >&2
    exit 1
  fi
  if [[ -z "$requested_release" ]]; then
    release=$(python3 "$repo_root/scripts/version_base.py" latest)
    printf 'selected latest canonical jcode release tag: %s\n' "$release"
  else
    release="$requested_release"
  fi

  [[ "$release" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
    printf 'sync requires a stable vX.Y.Z release tag\n' >&2
    exit 1
  }
  selected_base=$(python3 "$repo_root/scripts/version_base.py" resolve "$release" --fetch)
  printf 'selected canonical release: %s\nselected base: %s\n' "$release" "$selected_base"

  original_version=$(python3 "$repo_root/scripts/version_base.py" pinned)
  original_master=$(git show-ref --verify --hash refs/heads/master 2>/dev/null || true)
  rollback_sync() {
    python3 "$repo_root/scripts/version_base.py" write "$original_version" || true
    if [[ -n "$original_master" ]]; then
      git branch -f master "$original_master" || true
    else
      git branch -D master >/dev/null 2>&1 || true
    fi
  }
  trap rollback_sync ERR

  just --justfile "$repo_root/justfile" validate-patch-files "$selected_base"
  just --justfile "$repo_root/justfile" learn-upstream-exclusions "$selected_base"
  just --justfile "$repo_root/justfile" validate-patched-copy "$selected_base"

  git branch -f master "$selected_base"
  python3 "$repo_root/scripts/version_base.py" write "$release"
  trap - ERR

# Push catalog, pinned local release base, and candidate branches to origin.
push:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  current_branch=$(git branch --show-current)
  if [[ "$current_branch" != "personalized" ]]; then
    printf 'push must run on personalized; current branch: %s\n' "$current_branch" >&2
    exit 1
  fi
  git show-ref --verify --quiet refs/heads/master || {
    printf 'local master missing; run just sync first\n' >&2
    exit 1
  }

  refs=(
    refs/heads/personalized:refs/heads/personalized
    refs/heads/master:refs/heads/master
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
