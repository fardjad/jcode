# jcode engine for GitHub Agentic Workflows (gh-aw)

This directory contains reusable engine definitions that let you use
[jcode](https://github.com/fardjad/jcode) as an AI engine in
[GitHub Agentic Workflows](https://github.com/github/gh-aw).

## Files

| File | Engine ID | Description |
|------|-----------|-------------|
| `jcode.md` | `jcode` | OpenRouter EU provider (recommended). One secret. |
| `jcode-byok.md` | `jcode-byok` | Generic BYOK. Any OpenAI-compatible endpoint. Two secrets. |

## Quick start

### 1. Copy the engine definition

Copy `jcode.md` (or `jcode-byok.md`) into your repo at
`.github/workflows/shared/jcode.md`.

### 2. Create a workflow

Create `.github/workflows/my-workflow.md`:

```yaml
---
on: issues
engine: jcode
imports:
  - shared/jcode.md
model: openrouter-eu/anthropic/claude-sonnet-4-5
network:
  allowed:
    - defaults
    - .openrouter.ai
permissions:
  contents: read
  issues: write
---
Triage this issue and apply an appropriate label.
```

### 3. Add the API key secret

For the `jcode` (OpenRouter EU) engine:
```
gh aw secrets set OPENROUTER_EU_API_KEY --value "sk-or-..."
```

For the `jcode-byok` (generic BYOK) engine:
```
gh aw secrets set JCODE_MODEL_BASE_URL --value "https://api.openai.com/v1"
gh aw secrets set JCODE_MODEL_API_KEY --value "sk-..."
```

### 4. Compile and run

```
gh aw compile .github/workflows/my-workflow.md
```

## Engine variants

### jcode (OpenRouter EU) — recommended

Uses jcode's built-in `openrouter-eu` provider. The base URL
(`https://eu.openrouter.ai/api/v1`) is hardcoded in jcode, so you only
need one secret: `OPENROUTER_EU_API_KEY`.

The config locks jcode to the openrouter-eu provider with an
`allowed_providers` whitelist, preventing accidental use of other
providers.

```yaml
engine: jcode
imports:
  - shared/jcode.md
model: openrouter-eu/anthropic/claude-sonnet-4-5
network:
  allowed:
    - defaults
    - .openrouter.ai
```

### jcode-byok (Generic BYOK)

Points jcode at any OpenAI-compatible API endpoint. Requires two secrets:
`JCODE_MODEL_BASE_URL` and `JCODE_MODEL_API_KEY`.

```yaml
engine: jcode-byok
imports:
  - shared/jcode-byok.md
model: anthropic/claude-sonnet-4-5
network:
  allowed:
    - defaults
    - .anthropic.com
```

## Binary installation

Both engine definitions install jcode from
`https://github.com/fardjad/jcode/releases`. Pin a specific version with
`engine.version`:

```yaml
engine:
  id: jcode
  version: "v0.81.7"
```

Available release artifacts:
- `jcode-linux-x86_64.tar.gz`
- `jcode-linux-aarch64.tar.gz`
- `jcode-macos-x86_64.tar.gz`
- `jcode-macos-aarch64.tar.gz`
- `jcode-windows-x86_64.zip`
- `jcode-windows-aarch64.zip`

## Network configuration

The gh-aw firewall blocks all outbound traffic by default. You must
allow-list your LLM provider's domain in `network.allowed`:

| Pattern | Matches |
|---------|---------|
| `api.openai.com` | Exact host only |
| `.openrouter.ai` | `openrouter.ai` plus all subdomains (e.g. `eu.openrouter.ai`) |
| `*.openrouter.ai` | Subdomains only (not the bare domain) |
| `*` | All traffic (not recommended; rejected in strict mode) |

For OpenRouter EU, use `.openrouter.ai` to cover both `openrouter.ai` and
`eu.openrouter.ai`.

The `provider-domains` entries in the engine definition add the matching
domain automatically when the model uses a `provider/` prefix (e.g.
`openrouter-eu/anthropic/claude-sonnet-4-5` adds `eu.openrouter.ai`).
Matching is exact, so always add the domain in `network.allowed` as well.

## Skills

jcode auto-discovers `SKILL.md` files from these project-local
directories:

- `.jcode/skills/<name>/SKILL.md`
- `.agents/skills/<name>/SKILL.md`
- `.claude/skills/<name>/SKILL.md` (compatibility)

Just commit skill directories to your repo and they will be available to
the agent at runtime. No engine definition changes needed.

Each `SKILL.md` has frontmatter with `name`, `description`, and
`allowed-tools`, followed by markdown instructions:

```markdown
---
name: code-review
description: Use when reviewing code for quality, security, and best practices.
allowed-tools: bash, read, grep, agentgrep
---

# Code Review

Review the code for...
```

## Hooks

jcode supports external command integrations via the `[hooks]` config
section. The `[hooks]` block is enabled by default in both engine
definitions and points at `~/.jcode/plugins/rtk-transform`. The engine's
pre-agent-steps install rtk and the hook script automatically:

```toml
[hooks]
pre_tool_transform = "~/.jcode/plugins/rtk-transform"
pre_tool_transform_timeout_ms = 500
pre_tool_timeout_ms = 5000
```

Or set them via environment variables in the execution block:

```yaml
execution:
  env:
    JCODE_HOOK_PRE_TOOL_TRANSFORM: "~/.jcode/plugins/rtk-transform"
```

### rtk plugin example

To use the [rtk](https://github.com/rtk-ai/rtk) command rewriter,
install it in a pre-agent-step and configure the hook:

```yaml
pre-agent-steps:
  - name: Install rtk
    run: |
      curl -fsSL https://github.com/rtk-ai/rtk/releases/latest/download/rtk-x86_64-unknown-linux-musl.tar.gz \
        | tar xz -C /usr/local/bin rtk
  - name: Install rtk-transform hook
    run: |
      mkdir -p ~/.jcode/plugins
      cat > ~/.jcode/plugins/rtk-transform << 'EOF'
      #!/usr/bin/env python3
      import json, os, subprocess, sys
      tool_input = json.load(sys.stdin)
      if os.environ.get("JCODE_HOOK_TOOL_NAME") != "bash":
          sys.exit(0)
      command = tool_input.get("command", "")
      if not command:
          sys.exit(0)
      result = subprocess.run(["rtk", "rewrite", command], capture_output=True, text=True, timeout=0.4)
      rewritten = result.stdout.strip()
      if rewritten and rewritten != command:
          tool_input["command"] = rewritten
          json.dump(tool_input, sys.stdout, separators=(",", ":"))
      EOF
      chmod +x ~/.jcode/plugins/rtk-transform
```

Available hooks:

| Hook | Env var | Description |
|------|---------|-------------|
| `pre_tool_transform` | `JCODE_HOOK_PRE_TOOL_TRANSFORM` | Synchronous tool-input transformer |
| `pre_tool` | `JCODE_HOOK_PRE_TOOL` | Gate hook (exit 0 allows, exit 2 blocks) |
| `turn_start` | `JCODE_HOOK_TURN_START` | Observer: turn begins |
| `turn_end` | `JCODE_HOOK_TURN_END` | Observer: turn completes |
| `session_start` | `JCODE_HOOK_SESSION_START` | Observer: session becomes active |
| `session_end` | `JCODE_HOOK_SESSION_END` | Observer: session closes |

## Model selection

Set the model in your workflow frontmatter:

```yaml
model: openrouter-eu/anthropic/claude-sonnet-4-5
```

The `model-env-var: JCODE_MODEL` and `model-flag: --model` settings pass the
workflow's model to jcode in both the environment and the CLI invocation.
jcode also accepts `--model` as a CLI flag.

## AGENTS.md

jcode reads `AGENTS.md` from the repo root for custom instructions. This
file is listed in the engine's `manifest.files` so gh-aw protects it
from untrusted pull request modifications.

## Cross-repo imports

Instead of copying the engine definition, you can import it directly
from a published repo:

```yaml
engine: jcode
imports:
  - fardjad/jcode/.github/workflows/shared/jcode.md@v0.81.7
```

Pin to a tag or SHA to control when you pick up new versions.
