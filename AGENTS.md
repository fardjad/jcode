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
patch. Use `just test-patch-file <patch-file>` for declared
validation
commands and compatibility tests. Test and candidate workflows start clean
worktrees from `master`. Failures retain worktrees and print cleanup commands.

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

## Authoring patch mail headers

Every `patches/*.patch` must have exactly one copy of each required
`X-Jcode-Patch-*` header and exactly one copy of each required body section.
Place headers after `Subject:`. Place body sections after the first blank line
and before the mail-patch `---` separator, in required order.

Wrap every prose line in the mail header/body at 80 columns or fewer. Do not
wrap validation commands: each command is one unindented line under
`Validation:`. Never append generated `format-patch` output to an existing
patch. Replace the patch file, then add metadata/body sections once; otherwise
duplicate sections result. The validator rejects duplicate required headers and
body sections; do not rely on validation alone to catch this after generation.

Before committing a catalog patch, inspect its header through the `---`
separator, run `just validate-patch-files`, then run
`just test-patch-file patches/<name>.patch`.

## Patch update and commit discipline

When a change fixes behavior owned by an existing catalog patch, regenerate or
otherwise update that patch in the same work item before reporting the fix.
Never leave source-only fixes in `.patched-jcode` while its catalog patch is
stale.

Do not commit, amend, push, or create a PR unless the user explicitly asks.
Leave updated catalog files and generated worktree changes uncommitted for user
review. Do not create temporary commits merely to regenerate a patch.

Template for body inserted after generated mail headers:

```text
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
---
```

## Plugins

Plugin source is external catalog content under `plugins/`. Keep each plugin in
its own directory, document its upstream source and update procedure, and avoid
mixing plugin implementation changes with mail-patch changes unless required.
