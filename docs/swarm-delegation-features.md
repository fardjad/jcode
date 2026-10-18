# Swarm worker delegation features

These patches add reusable worker blueprints and runtime hook integration for
swarm-based delegation. The coordinator prompt in
`workers/blueprints/coordinator.md` guides the coordinator on when and how to
delegate token-heavy work to workers configured with task-appropriate models.

## Worker blueprints

Patch `1003-personal-feature-add-worker-blueprints-with-runtime-integration.patch`
enables reusable worker blueprints with coordinator delegation. Each blueprint
declares its enabled tools, effort level, and communication policy. A blueprint
can pin a model that may cost less than the coordinator's model. Workers return
compact summaries instead of raw tool output.

Available workers:

```text
swarm_fixer         One literal mechanical change in coordinator-defined scope
swarm_investigator  Read-only investigation, shell commands, grep, file search,
                    file reading, grep, tracing
swarm_research      Web and documentation research
```

Configure the swarm model in `~/.jcode/config.toml`:

```toml
[features]
swarm = true

[agents]
swarm_model = "openrouter-eu:gpt-4.1"
```

## Swarm context and post-tool transform hook

Patch `0002-candidate-add-hook-context-and-post-tool-transform.patch`
exposes swarm state and process role to hooks via environment variables and
structured JSON, and adds a synchronous post-tool transform hook. The hook lets
plugins replace oversized tool results before they enter model context. Large
hook payloads spill to temp files with TTL cleanup.

Hooks can check `JCODE_HOOK_SWARM_ENABLED` and `JCODE_HOOK_PROCESS_ROLE` to
behave differently for coordinators versus workers.

Configure a post-tool transformer in `~/.jcode/config.toml`:

```toml
[hooks]
post_tool_transform = ["~/.jcode/plugins/delegation-guard/delegation-guard-transform"]
post_tool_transform_timeout_ms = 500
```

The `delegation-guard` plugin (see `plugins/delegation-guard/`) uses this hook
to replace oversized coordinator tool results with a compact delegation nudge,
keeping the coordinator's context small.

## Pre-tool input transformers

Upstream jcode (since v0.89, merged from the former candidate patch
`0001-candidate-pre-tool-input-transformers.patch`) provides a generic
pre-tool transformer hook that lets plugins modify tool inputs before
execution. The `rtk-transform` plugin (see `plugins/rtk/`) uses this to
rewrite bash commands through RTK.

Configure a pre-tool transformer in `~/.jcode/config.toml`:

```toml
[hooks]
pre_tool_transform = ["~/.jcode/plugins/rtk/rtk-transform"]
```
