# jcode personalizations

This soft fork of [jcode](https://github.com/1jehuang/jcode) carries a small
catalog of patches and customizations on top of stable upstream releases.
It includes worker blueprints, plugins, skills, and tools to maintain and
evaluate them.

## Get started

Follow the [get started guide](docs/get-started.md) to install both the binary
and the customizations, configure them, and upgrade them together.

## Quick install

These commands install the binary only. For plugins, skills, and worker
blueprints, continue with the [get started guide](docs/get-started.md).

**macOS or Linux**

```bash
curl -fsSL https://raw.githubusercontent.com/fardjad/jcode/personalized/tools/scripts/install.sh | bash
```

**Windows PowerShell**

```powershell
irm https://raw.githubusercontent.com/fardjad/jcode/personalized/tools/scripts/install.ps1 | iex
```

## Repository

- [`patches/`](patches/): ordered changes to upstream jcode.
- [`customizations/`](customizations/): plugins, skills, and worker blueprints.
- [`docs/`](docs/): installation and feature documentation.
- [`tools/evaluator/`](tools/evaluator/README.md): worker evaluation client.

## Workflows

- `/patch-workflow`: change and validate patches.
- `/plan-upstream-sync`: plan and carry out an approved upstream update.
- `/evaluate-worker-blueprints`: run and review worker evaluations.
- `just help`: discover developer commands.
