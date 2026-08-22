# jcode patch catalog

`personalized` is an orphan catalog branch. It contains only catalog files:
`AGENTS.md`, `justfile`, `.gitignore`, `patches/`, `scripts/`, and `plugins/`.
Upstream source lives on local `master`; ordered mail patches describe changes
without requiring original source commits.

Validator discovers every `patches/*.patch`, checks intent, kind, dependency,
and body-section metadata, then applies all patches in lexicographic order with
`git am` to a disposable worktree based on `master`.

Commands:

```text
just help
just validate
just apply
just test <patch-file>
just candidate <patch-file>
just sync                    # selected upstream master
just sync vX.Y.Z             # selected upstream release tag
```

`just apply` creates or resets persistent `.patched-jcode` from local `master`,
applies every patch, runs declared validations, and runs `_fast-test`. Test and
candidate workflows also start clean worktrees from `master`. Failures retain
worktrees and print cleanup commands.

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

RTK plugin source is external catalog content under `plugins/`.
