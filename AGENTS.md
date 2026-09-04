# jcode patch catalog

Always spell the project name `jcode` in human-facing prose, workflow names,
and labels. Preserve existing capitalization in code identifiers, environment
variables, protocol values, patch metadata headers, and upstream source text.

`personalized` is an orphan catalog branch. It contains only catalog files:
`README.md`, `AGENTS.md`, `justfile`, `nextest.toml`, `.gitignore`, `patches/`,
`scripts/`, and `plugins/`. Upstream source lives on local `master`; ordered
mail patches describe changes without requiring original source commits.

Validator discovers every `patches/*.patch`, checks intent, kind, dependency,
and body-section metadata, then applies all patches in lexicographic order with
`git am` to a disposable worktree based on `master`.

Commands:

```text
just help
just validate-patch-files
just create-patched-copy
just snapshot-patches            # regenerate patches from .patched-jcode commits
just list-patches                # print commit-to-patch mapping
just migrate-commit-metadata     # one-time: copy patch metadata into commits
just install-patched-version
just test-patch-file <patch-file>
just create-upstream-candidate-branch-from <patch-file>
just sync                    # selected upstream master
just sync vX.Y.Z             # selected upstream release tag
```

`just create-patched-copy` creates or resets persistent `.patched-jcode` from
local `master`. If local `master` does not exist, it initializes it from
configured `upstream/master`; later runs retain that base. Use `just sync` to
refresh it and run clean-upstream compatibility learning. It then applies every
patch, each as its own commit. Use `just test-patch-file <patch-file>` for
declared validation commands and compatibility tests. Test and candidate
workflows start clean worktrees from `master`. Failures retain worktrees and
print cleanup commands.

Compatibility failures are learned only from clean upstream worktrees. Ignored
state lives at `.tools/nextest/exclusions/<base-commit>.json`, keyed by base
commit and pinned nextest version. Patched failures are not learned or hidden:
they remain test failures. Delete that directory to reset learned state.

Compatibility tests pin cargo-nextest `0.9.143`, bootstrapped on demand and
cached under ignored `.tools/nextest/<version>/<target>/`.
JUnit reports live at `target/nextest/compat/junit.xml` inside each materialized
worktree. Learned exclusions are sorted exact `test(=...)` filters and apply
only when base commit and nextest pin match.

`just sync` fetches selected upstream `master` or tag, safely updates local
`master`, tests clean upstream, then applies catalog patches. It never rebases
or requires an ancestor relationship with orphan `personalized`, and never
pushes. Run high-level workflows from clean `personalized`.

Patch application order is lexicographic. Dependencies must be declared with
`X-Jcode-Patch-Depends-On` as `none` or comma-separated earlier patch names.
Required metadata headers are `X-Jcode-Patch-Intent`,
`X-Jcode-Patch-Kind`, and `X-Jcode-Patch-Depends-On`. Each patch must contain
non-empty ordered sections: `Patch intent:`, `Why it exists:`, `Upstream
integration points:`, `Update guidance:`, and `Validation:`.

## Working in .patched-jcode (commit-first workflow)

Never edit `.patch` files directly. Patches are persistence artifacts derived
from commits. The commit message in `.patched-jcode` is the single source of
truth for both the diff and the `X-Jcode-Patch-*` metadata.

The ideal workflow for any change:

1. `just create-patched-copy` — ensures `.patched-jcode` exists, clean, and
   matches the current catalog.
2. Edit source files directly in `.patched-jcode/`.
3. Commit or amend the relevant commit there (see below for which commit).
4. `just snapshot-patches` — regenerates every `.patch` file from the commits
   above `master`. Run `just validate-patch-files` to confirm.
5. `just test-patch-file patches/<name>.patch` to run the patch's validation.

### Mapping commits to patches

Commits above `master` in `.patched-jcode` map to patch files by lexicographic
order: the oldest commit is the first patch, the next is the second, and so on.
Run `just list-patches` to see the mapping. To change an existing patch, amend
the corresponding commit. To add a new patch, add a new commit on top and run
`just snapshot-patches`; it assigns the next available numeric prefix and
derives a slug from the commit subject.

### Commit message format

Every patch commit message must contain the three `X-Jcode-Patch-*` headers and
the five body sections. `git format-patch` turns the commit message into the
patch file, so the message is the patch. Use this format:

```text
feat: short imperative subject

X-Jcode-Patch-Intent: short user-visible outcome
X-Jcode-Patch-Kind: personal-feature
X-Jcode-Patch-Depends-On: none

Patch intent:
State change in one wrapped paragraph.

Why it exists:
State problem solved and intentional non-goals.

Upstream integration points:
Name upstream files, symbols, and behavior this patch relies on.

Update guidance:
State what future updates must preserve.

Validation:
scripts/cargo_exec.sh test -p package focused_test --lib
```

Wrap every prose line at 80 columns or fewer. Do not wrap validation commands:
each command is one unindented line under `Validation:`. The `X-Jcode-Patch-*`
headers go in the commit message body, after the subject and its blank line.
Dependencies must be declared with `X-Jcode-Patch-Depends-On` as `none` or
comma-separated earlier patch names. Required metadata headers are
`X-Jcode-Patch-Intent`, `X-Jcode-Patch-Kind`, and
`X-Jcode-Patch-Depends-On`. Required ordered body sections are `Patch intent:`,
`Why it exists:`, `Upstream integration points:`, `Update guidance:`, and
`Validation:`.

### Personal vs candidate patches

Personal patches use `X-Jcode-Patch-Kind: personal-*` and a `1000`-series numeric
prefix. The snapshot tool zeroes their `From` commit hash for reproducibility.
Candidate patches use `X-Jcode-Patch-Kind: upstream-candidate` and a `0000`-series
prefix; they keep their real `From` hash. Keep candidate patches focused and
independent from personal patches.

## Patch update and commit discipline

When a change fixes behavior owned by an existing catalog patch, make the fix in
`.patched-jcode`, amend the corresponding commit, then run
`just snapshot-patches` to regenerate the patch. Never leave source-only fixes
in `.patched-jcode` while its catalog patch is stale.

Do not commit, amend, push, or create a PR unless the user explicitly asks.
Leave updated catalog files and generated worktree changes uncommitted for user
review. Do not create temporary commits merely to regenerate a patch.

## Plugins

Plugin source is external catalog content under `plugins/`. Keep each plugin in
its own directory, document its upstream source and update procedure, and avoid
mixing plugin implementation changes with mail-patch changes unless required.
