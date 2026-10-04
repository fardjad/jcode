#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
justfile="$repo_root/justfile"
sync_recipe="$(sed -n '/^sync release=/,/^# Push catalog/p' "$justfile")"

# Sync must resolve and fetch stable tags through the canonical URL helper,
# reject moving refs, and update VERSION.txt only after patch validation.
grep -Fq 'version_base.py" latest' <<<"$sync_recipe"
grep -Fq 'version_base.py" resolve "$release" --fetch' <<<"$sync_recipe"
grep -Fq 'sync requires a stable vX.Y.Z release tag' <<<"$sync_recipe"
grep -Fq 'validate-patched-copy' <<<"$sync_recipe"
grep -Fq 'version_base.py" write "$release"' <<<"$sync_recipe"
! grep -Eq 'git (fetch|ls-remote).* (upstream|origin)( |$)' <<<"$sync_recipe"
grep -Fq 'UPSTREAM_URL = "https://github.com/1jehuang/jcode.git"' \
  "$repo_root/scripts/version_base.py"
grep -Fq '"fetch", "--no-tags", UPSTREAM_URL' \
  "$repo_root/scripts/version_base.py"
grep -Fq 'version_base.py" resolve' "$repo_root/justfile"
! grep -Fq 'just sync master' "$repo_root/README.md"

python3 "$repo_root/scripts/test_version_base.py"
echo "canonical stable-release sync recipe test passed"
