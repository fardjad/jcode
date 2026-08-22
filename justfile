# List orphan catalog workflow recipes.
help:
  #!/usr/bin/env bash
  set -euo pipefail

  just --list --justfile "$(git rev-parse --show-toplevel)/justfile"

validate:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  python3 "$repo_root/scripts/validate_patches.py"

apply:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  worktree="$repo_root/.patched-jcode"

  python3 "$repo_root/scripts/validate_patches.py"
  worktree=$(python3 "$repo_root/scripts/patch_worktree.py" create patched-jcode master --path "$worktree")
  trap 'printf "workflow failed; worktree retained: %s\ncleanup: git worktree remove --force %q\n" "$worktree" "$worktree" >&2' ERR

  python3 "$repo_root/scripts/apply_patches.py" "$worktree"
  python3 "$repo_root/scripts/validate_patch_commands.py" "$worktree"
  just --justfile "$repo_root/justfile" _fast-test "$worktree"

  trap - ERR
  printf 'all patches passed; worktree retained: %s\n' "$worktree"

test patch:
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
  just --justfile "$repo_root/justfile" _fast-test "$worktree"

  trap - ERR
  printf 'patch passed; worktree retained: %s\n' "$worktree"

candidate patch:
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
  if ! just --justfile "$repo_root/justfile" _fast-test "$upstream_worktree"; then
    printf 'clean upstream fast tests failed; worktree retained: %s\n' "$upstream_worktree" >&2
    exit 1
  fi
  python3 "$repo_root/scripts/patch_worktree.py" cleanup sync-upstream --path "$upstream_worktree"
  just --justfile "$repo_root/justfile" apply

# Run fast suite with v0.79.1 upstream compatibility exclusions; remove when fixed upstream.
_fast-test worktree:
  #!/usr/bin/env bash
  set -euo pipefail

  repo_root=$(git rev-parse --show-toplevel)
  worktree_path="{{worktree}}"
  [[ "$worktree_path" = /* ]] || worktree_path="$repo_root/$worktree_path"

  (cd "$worktree_path" && scripts/test_fast.sh -- \
    --skip auto_poke_followup_targets_below_threshold_todos \
    --skip cli_auth_status_doctor_and_login_lifecycle_uses_fresh_sandbox \
    --skip auth_integration_registry_matches_cli_choice_runtime_wiring \
    --skip login_provider_choice_table_round_trips_catalog_providers \
    --skip auto_provider_noninteractive_skips_untrusted_external_auth_instead_of_blocking \
    --skip test_init_provider_jcode_delegates_runtime_profile_to_wrapper)
