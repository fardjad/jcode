# jcode personalizations

This is my personal catalog for changes applied on top of
[jcode](https://github.com/1jehuang/jcode). It has its own orphan history and
does not contain jcode source code.

[`master`](https://github.com/fardjad/jcode/tree/master) holds the selected
upstream jcode base. This branch holds only the
personalization catalog. Applying the catalog creates `.patched-jcode/`: a
generated worktree containing `master` plus the selected source patches.

## Contents

- [`patches/`](patches/) contains ordered mail patches for personal source changes. Patches
  declare their purpose, dependencies, update guidance, and focused validation.
- [`plugins/`](plugins/) contains personal plugins and their documentation. Plugins are
  catalog content, not source patches; install or configure them separately.
- [`scripts/`](scripts/) contains small Python primitives used by the workflow.
- [`justfile`](justfile) composes those primitives into normal workflows.
- [`nextest.toml`](nextest.toml) defines compatibility JUnit reporting.
- [`AGENTS.md`](AGENTS.md) defines catalog conventions for human and agent maintenance.

## Common workflow

Run `just help` to list available commands.

```bash
# Select and validate current upstream master, then apply personal patches.
just sync

# Use a stable upstream release instead.
just sync vX.Y.Z

# Apply catalog patches to .patched-jcode.
just create-patched-copy

# Refresh patched copy, then install its fast release build.
just install-patched-version

# Validate catalog metadata and patch applicability without the full workflow.
just validate-patch-files

# Create or verify an upstream-candidate branch from a candidate patch.
just create-upstream-candidate-branch-from 0001-candidate-example.patch

```

`just sync` and `just create-patched-copy` retain failed worktrees for
inspection. They never
push remotes. The generated `.patched-jcode/` directory is ignored.

Compatibility tests pin cargo-nextest `0.9.143`; cache lives under
`.tools/nextest/<version>/<target>/`. Clean upstream runs learn failed test
names into ignored, base-specific JSON at
`.tools/nextest/exclusions/<base-commit>.json`. Patched runs load exclusions
only for matching base and pin, using exact `test(=...)` filters; patched
failures remain failures. Each run writes JUnit report to
`<materialized-worktree>/target/nextest/compat/junit.xml`. Remove
`.tools/nextest/exclusions/` to reset learned state.

## Patch maintenance

Patch files are the source of truth for source-level customizations. When an
upstream change causes a patch conflict or test failure, update the affected
patch and its repair brief, then rerun the relevant `just` workflow. Do not edit
jcode source directly in this catalog checkout.
