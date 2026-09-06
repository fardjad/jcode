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
    - index.crates.io
    - static.crates.io
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
---
Synchronize this repo's patch catalog with upstream jcode.

Read `AGENTS.md` and `justfile` first. They define the patch catalog
workflow, commit-first process, and all `just` commands. Do not duplicate
that knowledge here.

## Steps

1. Configure the upstream remote as HTTPS (SSH will not work in CI):
   ```
   git remote set-url upstream https://github.com/1jehuang/jcode.git 2>/dev/null || git remote add upstream https://github.com/1jehuang/jcode.git
   ```

2. Fetch upstream and update local master:
   ```
   git fetch upstream master
   git branch -f master FETCH_HEAD
   ```

3. Run `just create-patched-copy` to validate and apply all patches.

4. If `just create-patched-copy` fails, you MUST fix the patches. Do not
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
   - Run `just create-patched-copy` again to verify all patches apply cleanly
   - Repeat until `just create-patched-copy` succeeds
   - Never edit `.patch` files directly; they are derived from commits

5. After `just create-patched-copy` succeeds, check for changes:
   ```
   git status --porcelain
   ```

6. If there are no changes, call `noop` confirming sync is up to date.

7. If there are changes, create a PR via `create_pull_request` with a
   title like "sync: update patches for upstream <short-sha>".

Do not use `git commit`, `git push`, or `gh` directly for GitHub writes.
Use safe-output tools for all GitHub write operations.
