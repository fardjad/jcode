# Swarm worker delegation features

These patches add reusable worker blueprints and runtime hook integration for
swarm-based delegation. The coordinator prompt in
`workers/blueprints/coordinator.md` guides the coordinator on when and how to
delegate token-heavy work to cheaper worker models.

## Worker blueprints

Patch `1008-personal-feature-add-worker-blueprints-with-runtime-integration.patch`
enables reusable worker blueprints with coordinator delegation. Each blueprint
declares its enabled tools, effort level, and communication policy. Workers run
on a cheaper model and return compact summaries instead of raw tool output.

Available workers:

```text
swarm_explorer      Code investigation, file reading, grep, tracing
swarm_bash-runner   Shell commands, builds, tests, script execution
swarm_fixer         Code changes with build/test validation
swarm_research      Web and documentation research
swarm_automation    Browser, UI, and Gmail workflows
```

Configure the swarm model in `~/.jcode/config.toml`:

```toml
[features]
swarm = true

[agents]
swarm_model = "openrouter-eu:gpt-4.1"
```

## Swarm context, post-tool transform, and record hooks

Patch `1011-personal-feature-add-hook-context-and-post-tool-transform.patch`
exposes swarm state and process role to all hooks via environment variables and
structured JSON, and adds synchronous post-tool transform and detached
post-tool record hooks. The transform hook lets plugins replace oversized
tool results before they enter model context. The record hook logs tool
invocations for historical lookups. Large tool I/O overflows to temp files
with TTL cleanup.

Hooks can check `JCODE_HOOK_SWARM_ENABLED` and `JCODE_HOOK_PROCESS_ROLE` to
behave differently for coordinators versus workers.

Configure a post-tool transformer in `~/.jcode/config.toml`:

```toml
[hooks]
post_tool_transform = ["~/.jcode/plugins/delegation-guard-transform"]
post_tool_transform_timeout_ms = 500
```

The `delegation-guard` plugin (see `plugins/delegation-guard/`) uses this hook
to replace oversized coordinator tool results with a compact delegation nudge,
keeping the coordinator's context small.

## Pre-tool input transformers

Patch `0001-candidate-pre-tool-input-transformers.patch` adds a generic
pre-tool transformer hook that lets plugins modify tool inputs before
execution. The `rtk-transform` plugin (see `plugins/rtk/`) uses this to
rewrite bash commands through RTK.

Configure a pre-tool transformer in `~/.jcode/config.toml`:

```toml
[hooks]
pre_tool_transform = ["~/.jcode/plugins/rtk-transform"]
```
