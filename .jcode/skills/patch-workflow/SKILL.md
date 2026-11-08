---
name: patch-workflow
description: >-
  Use when changing, adding, removing, reordering, or validating catalog
  patches.
---

# Patch workflow

Follow `AGENTS.md` for principles and boundaries.

## Change a patch

1. Run `just create-patched-copy` to establish a clean, current copy.
2. Edit source in `.patched-jcode/`.
3. Commit or amend the relevant source commit.
4. Before snapshotting, compare the commit's full intent with the diff and the
   named tests; fix the code, tests, or prose where they disagree.
5. Run `just snapshot-patches`, then `just validate-patch-files`.
6. Run `just test-patch-file patches/<name>.patch`.

For a new patch, commit on top with a free `X-Jcode-Patch-File` name. To remove
a patch, drop its commit and run `just snapshot-patches --prune`. To reorder or
split, reorder or add commits, renumber their `X-Jcode-Patch-File` headers in
commit order, and update affected `X-Jcode-Patch-Depends-On` entries. Use
`--prune` when old filenames go away.

When repairing a stack, attribute follow-up fixes to their owning patch until
its commit can be amended; never place an earlier fix in a later patch. Keep
recovery refs or bundles before rewriting and verify exact final tree equality.
Do not reset a copy with unpersisted commits or edits for bookkeeping.

## Commit message

Use the example in `AGENTS.md`. Put these headers in the commit body after the
subject and blank line:

- `X-Jcode-Patch-Intent`: short user-visible outcome.
- `X-Jcode-Patch-Kind`: patch kind.
- `X-Jcode-Patch-Depends-On`: `none` or comma-separated earlier patch names.
- `X-Jcode-Patch-File`: the patch's own filename.

Follow with these sections in order: `Patch intent:`, `Why it exists:`,
`Upstream integration points:`, `Update guidance:`, and `Validation:`.
Wrap prose at 80 columns. Put each validation command on one unindented line;
commands must run at least one test and should cover the full intent.

Personal patches use `personal-*` kinds and `1000`-series filenames; snapshot
zeros their `From` hashes. Upstream candidates use `upstream-candidate` and
`0000`-series filenames, retain their hashes, and remain focused and independent
of personal patches.
