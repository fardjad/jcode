---
name: ensure-catalog-plugins
summary: Check and configure global jcode hooks for the catalog's symlinked plugins.
description: Inspect ~/.jcode/config.toml and, when required, safely update the [hooks] settings for the RTK transformer, delegation guard, and delegation-efficiency observer after verifying the catalog plugin symlink.
allowed-tools: bash, read, write
---

# Ensure catalog plugins are enabled

Use this skill when the user asks to enable, repair, check, or synchronize the
catalog plugins in their global jcode configuration.

## Scope and authorization

This skill is explicitly authorized to inspect and modify only the user's
`~/.jcode/config.toml` and the plugin paths beneath `~/.jcode/plugins/`. It may
run the catalog's `just ensure-personal-assets` recipe from the catalog
repository root to create or refresh the plugin and worker-blueprint symlinks.
Do not change provider, model, tool, agent, display, MCP, or unrelated hook
settings. Do not edit repository configuration files. Do not replace a real
directory at `~/.jcode/plugins` or `~/.jcode/worker-blueprints`; stop and tell
the user to resolve that conflict.

## Required configuration

The complete plugin directory must be symlinked first:

```text
~/.jcode/plugins -> <catalog-root>/plugins
```

The required `[hooks]` values are:

```toml
[hooks]
pre_tool_transform = ["~/.jcode/plugins/rtk/rtk-transform"]
post_tool_transform = ["~/.jcode/plugins/delegation-guard/delegation-guard-transform"]
post_tool_transform_timeout_ms = 500
delegation_efficiency = "~/.jcode/plugins/delegation-efficiency/record.py"
```

Preserve unrelated keys in `[hooks]`. The transformer settings deliberately use
single-element arrays because jcode supports ordered transformer chains.

## Procedure

1. Identify the catalog repository root and run:

   ```bash
   just ensure-personal-assets
   ```

   The recipe is idempotent and establishes both catalog symlinks. If it refuses
   to replace a real plugin or worker-blueprint directory, stop without changing
   configuration and tell the user to resolve that conflict.
2. Resolve and inspect `~/.jcode/plugins`. Confirm it is a symlink and that all
   three configured executable files exist and are executable:
   - `rtk/rtk-transform`
   - `delegation-guard/delegation-guard-transform`
   - `delegation-efficiency/record.py`
3. If the symlink is missing, broken, or a real directory after the recipe,
   stop without modifying config and tell the user to resolve the conflict.
4. Read `~/.jcode/config.toml`. If it is absent, create it with only the
   required `[hooks]` table. If it exists, parse it before changing anything.
5. Update only the four required hook keys. Preserve all other TOML values and
   comments where practical. Before writing an existing config, create a
   timestamped sibling backup ending in `.bak`.
6. Re-read and parse the written TOML. Verify the four values exactly and verify
   each configured executable path.
7. Report whether the config was already correct or which hook keys changed,
   plus the backup path if one was created.

## Safe editing guidance

Use a TOML-aware editor or a small deterministic script that updates only the
`[hooks]` section. Never use a broad regex replacement that can alter another
table. If safe preservation of an unusual TOML layout is not possible, stop and
show the proposed minimal edit rather than overwriting the file.

## Readiness check

No plugin installation, `uv sync`, virtual environment, or compilation is
needed. The delegation-efficiency plugin has no third-party dependencies. Its
lockfile can be checked with:

```bash
uv lock --check --directory ~/.jcode/plugins/delegation-efficiency
```

The catalog's `just ensure-personal-assets` recipe already performs the
symlink and readiness checks, so prefer it before modifying configuration.
