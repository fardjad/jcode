---
on: workflow_dispatch
engine:
  id: jcode
imports:
  - shared/jcode.md
model: gpt-5.6-terra
network:
  allowed:
    - defaults
    - eu.openrouter.ai
    - rust
permissions:
  contents: read
  pull-requests: read
max-turns: 80
safe-outputs:
  create-pull-request:
    max: 1
  noop:
    report-as-issue: false
checkout:
  fetch-depth: 0
  fetch:
    - master
jobs:
  sync-master:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - uses: actions/checkout@v7
        with:
          fetch-depth: 0
          persist-credentials: true
      - name: Fetch upstream and push master
        run: |
          git remote add upstream https://github.com/1jehuang/jcode.git
          git fetch upstream master
          git push origin FETCH_HEAD:refs/heads/master --force
pre-agent-steps:
  - name: Install just
    run: |
      ARCH=$(uname -m)
      case "$ARCH" in
        x86_64)  ARCH="x86_64" ;;
        aarch64) ARCH="aarch64" ;;
      esac
      JUST_VERSION=$(curl -fsSI https://github.com/casey/just/releases/latest | grep -i '^location:' | sed 's|.*/tag/||' | tr -d '\r\n')
      curl -fsSL "https://github.com/casey/just/releases/download/${JUST_VERSION}/just-${JUST_VERSION}-${ARCH}-unknown-linux-musl.tar.gz" \
        | tar xz -C /usr/local/bin just
  - name: Verify just installation
    run: just --version
  - name: Configure upstream remote and update master
    run: |
      git remote set-url upstream https://github.com/1jehuang/jcode.git 2>/dev/null || git remote add upstream https://github.com/1jehuang/jcode.git
      git fetch upstream master
      git branch -f master FETCH_HEAD
---
Synchronize this repo's patch catalog with upstream jcode.

Read `AGENTS.md` and `justfile` first. They define the patch catalog
workflow, commit-first process, and all `just` commands. Do not duplicate
that knowledge here.

## Steps

1. Run `just learn-upstream-exclusions` before touching any patches. Local
   `master` is already updated to upstream by the pre-agent steps. This runs
   the compatibility suite in a clean upstream-only worktree and refreshes
   the ignored, base-specific exclusions. Never learn exclusions from a
   patched worktree, because that could hide a catalog regression.

2. Run `just validate-patched-copy`. This applies every patch, runs
   `cargo check --workspace` against the complete patched worktree, and runs
   the compatibility suite using the exclusions learned in step 1. Patch
   application alone is not sufficient: it
   cannot detect a patch that applies cleanly but no longer compiles.

3. If `just validate-patched-copy` fails, you MUST fix the patches. Do not
   just report the failure and call noop. Follow the commit-first workflow
   described in `AGENTS.md`:
   - The worktree at `.patched-jcode/` is retained even on failure
   - Enter `.patched-jcode/` and fix the source files directly so the
     change compiles and works against the new upstream base
   - Amend the corresponding commit (use `just list-patches` to find the
     commit-to-patch mapping if needed)
   - Run `just snapshot-patches` to regenerate all `.patch` files from the
     amended commits
   - Run `just validate-patch-files` to confirm metadata is valid
   - Run `just validate-patched-copy` again to verify patch application,
     compilation, and compatibility tests
   - Repeat until `just validate-patched-copy` succeeds
   - Never edit `.patch` files directly; they are derived from commits

4. After `just validate-patched-copy` succeeds, check for changes:
   ```
   git status --porcelain
   ```

5. If there are no changes, call `noop` confirming sync is up to date.

6. If there are changes, create a PR via `create_pull_request` with a
   title like "sync: update patches for upstream <short-sha>".

Do not use `git commit`, `git push`, or `gh` directly for GitHub writes.
Use safe-output tools for all GitHub write operations.
