#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
justfile="$repo_root/justfile"
pin_recipe="$(sed -n '/^set-version release/,/^# Push catalog/p' "$justfile")"
push_recipe="$(sed -n '/^push:/,/^# Learn clean-upstream/p' "$justfile")"

# `set -e` ignores failures of `!`-negated commands, so negative assertions
# must fail explicitly.
refute() {
  if "$@"; then
    printf 'unexpected match: %s\n' "$*" >&2
    exit 1
  fi
}

test -n "$pin_recipe"

# Retired recipes stay absent, and pinned-base workflows take no overrides.
normalize_prefix=normalize
normalize_suffix=-patch
migrate_prefix=migrate-commit
migrate_suffix=-metadata
refute grep -Eq "^${normalize_prefix}${normalize_suffix}([[:space:]]|:)" "$justfile"
refute grep -Eq "^${migrate_prefix}${migrate_suffix}([[:space:]]|:)" "$justfile"
for recipe in create-patched-copy validate-patch-files validate-patched-copy learn-upstream-exclusions; do
  grep -Eq "^${recipe}:$" "$justfile"
  refute grep -Eq "^${recipe}[[:space:]]" "$justfile"
done

# set-version must take an explicit stable tag, always refetch it through the
# canonical URL helper, check an optional reviewed hash, and only then write
# VERSION.txt. It must not pick a release on its own or validate patches.
grep -Fq 'set-version release expected=""' <<<"$pin_recipe"
grep -Fq 'set-version requires a stable vX.Y.Z release tag' <<<"$pin_recipe"
grep -Fq 'set-version requires clean personalized checkout' <<<"$pin_recipe"
grep -Fq 'version_base.py" resolve "$release" --fetch' <<<"$pin_recipe"
grep -Fq 'does not match reviewed hash' <<<"$pin_recipe"
grep -Fq 'version_base.py" write "$release"' <<<"$pin_recipe"
refute grep -Fq 'version_base.py" latest' <<<"$pin_recipe"
refute grep -Fq 'validate-patched-copy' <<<"$pin_recipe"
check_line=$(grep -n 'does not match reviewed hash' <<<"$pin_recipe" | cut -d: -f1)
write_line=$(grep -n 'version_base.py" write' <<<"$pin_recipe" | cut -d: -f1)
test "$check_line" -lt "$write_line"
refute grep -Eq 'git (fetch|ls-remote).* (upstream|origin)( |$)' <<<"$pin_recipe"
grep -Fq 'UPSTREAM_URL = "https://github.com/1jehuang/jcode.git"' \
  "$repo_root/scripts/version_base.py"
grep -Fq '"fetch", "--no-tags", UPSTREAM_URL' \
  "$repo_root/scripts/version_base.py"

# just sync is retired; set-version and the review skill replace it.
refute grep -Eq '^sync( |:)' "$justfile"
refute grep -Fq 'just sync' "$repo_root/README.md" "$repo_root/AGENTS.md" \
  "$repo_root/.jcode/skills/plan-upstream-sync/SKILL.md"

# VERSION.txt is the only version pin: no recipe or workflow may create, move,
# or push a master branch.
refute grep -Fq 'master' <<<"$pin_recipe"
refute grep -Fq 'master' <<<"$push_recipe"
grep -Fq 'refs/heads/personalized:refs/heads/personalized' <<<"$push_recipe"
refute grep -Fq 'master' "$repo_root/.github/workflows/release-patched.yml"

python3 "$repo_root/scripts/test_version_base.py"
echo "canonical stable-release pin recipe test passed"
