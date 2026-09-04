# List orphan catalog workflow recipes.
help:
  #!/usr/bin/env bash
  set -euo pipefail

  just --list --justfile "$(git rev-parse --show-toplevel)/justfile"

# Validate catalog patch metadata and synthetic application.
validate-patch-files:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/validate_patches.py"

_bootstrap-nextest:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/bootstrap_nextest.py"

# Create/reset persistent patched copy from local master.
create-patched-copy:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  if ! git show-ref --verify --quiet refs/heads/master; then
    git remote get-url upstream >/dev/null 2>&1 || {
      printf 'local master missing and upstream remote is not configured\n' >&2
      exit 1
    }
    printf 'initializing local master from upstream/master\n'
    git fetch upstream master
    git branch master FETCH_HEAD
  fi
  base=$(git rev-parse --verify master^{commit})
  worktree="$repo_root/.patched-jcode"

  python3 "$repo_root/scripts/validate_patches.py"
  worktree=$(python3 "$repo_root/scripts/patch_worktree.py" create patched-jcode "$base" --path "$worktree")
  trap 'printf "workflow failed; worktree retained: %s\ncleanup: git worktree remove --force %q\n" "$worktree" "$worktree" >&2' ERR

  python3 "$repo_root/scripts/apply_patches.py" "$worktree"

  trap - ERR
  printf 'patches applied; worktree retained: %s\n' "$worktree"

# Regenerate every catalog patch from .patched-jcode commits.
snapshot-patches:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/snapshot_patches.py"

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
  just --justfile "$repo_root/justfile" create-patched-copy
  (
    cd "$worktree"
    ./scripts/install_release.sh --fast
  )

# Apply one patch in a clean worktree and run its validation/tests.
test-patch-file patch:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  patch_name=$(basename "{{patch}}")
  name="test-${patch_name%.patch}"

  python3 "$repo_root/scripts/validate_patches.py"
  worktree=$(python3 "$repo_root/scripts/patch_worktree.py" create "$name" master)
  trap 'printf "workflow failed; worktree retained: %s\ncleanup: git worktree remove --force %q\n" "$worktree" "$worktree" >&2' ERR

  python3 "$repo_root/scripts/apply_patches.py" "$worktree" "$patch_name"
  python3 "$repo_root/scripts/validate_patch_commands.py" "$worktree" "$patch_name"
  base=$(git rev-parse master^{commit})
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

  python3 "$repo_root/scripts/validate_patches.py"
  worktree=$(python3 "$repo_root/scripts/patch_worktree.py" create "$name" master)
  trap 'printf "workflow failed; worktree retained: %s\ncleanup: git worktree remove --force %q\n" "$worktree" "$worktree" >&2' ERR

  python3 "$repo_root/scripts/apply_patches.py" "$worktree" "$patch_name" --require-kind upstream-candidate
  python3 "$repo_root/scripts/validate_patch_commands.py" "$worktree" "$patch_name"
  python3 "$repo_root/scripts/candidate_branch.py" "$worktree" "$branch"
  python3 "$repo_root/scripts/patch_worktree.py" cleanup "$name" --path "$worktree"

  trap - ERR
  printf 'candidate ready: %s\n' "$branch"

# Sync local master from upstream, learn exclusions, and create patched copy.
sync release="master":
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  release="{{release}}"
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
    printf 'refusing to reset master: it is checked out in another worktree\n' >&2
    exit 1
  fi

  if [[ "$release" == master ]]; then
    git fetch upstream master
    selected_ref=upstream/master
  else
    git check-ref-format --allow-onelevel "refs/tags/$release" >/dev/null || {
      printf 'invalid upstream tag name: %s\n' "$release" >&2
      exit 1
    }
    git fetch upstream "refs/tags/$release:refs/tags/$release"
    selected_ref="refs/tags/$release"
  fi

  selected_base=$(git rev-parse "$selected_ref^{commit}")
  printf 'selected ref: %s\nselected base: %s\n' "$selected_ref" "$selected_base"
  git branch -f master "$selected_base"

  upstream_worktree=$(python3 "$repo_root/scripts/patch_worktree.py" create sync-upstream master)
  if ! just --justfile "$repo_root/justfile" _learn-tests "$upstream_worktree" "$selected_base"; then
    printf 'clean upstream fast tests failed; worktree retained: %s\n' "$upstream_worktree" >&2
    exit 1
  fi
  python3 "$repo_root/scripts/patch_worktree.py" cleanup sync-upstream --path "$upstream_worktree"
  just --justfile "$repo_root/justfile" create-patched-copy

# Push catalog, synchronized upstream base, and candidate branches to origin.
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
