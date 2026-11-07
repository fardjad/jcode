# Swarm worker delegation features

These patches add reusable worker blueprints and runtime hook integration for
swarm-based delegation. The coordinator prompt in
`customizations/workers/blueprints/coordinator.md` guides when and how to delegate
bounded tasks to capability tiers with appropriate configured models.

## Worker blueprints

Patch `1003-personal-feature-worker-blueprints-and-session-lifecycle-fixes.patch`
enables reusable Markdown blueprints and coordinator delegation. The shipped
blueprints provide three capability tiers: `swarm_routine` for low-ambiguity
bounded work, `swarm_advanced` for moderate implementation/debugging and scoped
Routine assistance, and `swarm_expert` for hard reasoning and scoped Advanced
assistance. The coordinator selects a suitable tier; all can complete bounded
inspect/edit/check assignments when authorized. Models are unspecified in the
shipped blueprints, and global model restrictions still apply.

The old `swarm_fixer`, `swarm_investigator`, and `swarm_research` IDs are retired.
Update personal overrides and assignments when upgrading. Coordination policy
(the planning-only coordinator, routing, and the report contract) is owned by
`customizations/workers/AGENTS.md`. Its blueprint front matter denies the
generic execution and research tools while the empty enabled list keeps the
generated tier tools, read, `skill_manage` skill access, and write available.
Workers report to the parent. The shipped blueprints use
child-blueprint allowlists: Advanced and Expert allow only Routine, while
Routine allows no children. The generic native policy also honors explicit
tool denials, and an allowed-child list cannot override a disabled tool. See
`customizations/workers/README.md` for the effective persisted TOML override,
default-deny behavior when no list is configured, the unchanged depth/model/
parent-work access rules, and the bounded assignment, blocker assistance, and
handback contract.

Child-blueprint allowlists are generic IDs, not a fixed tier registry. For
example, a custom `reviewer` may allow a custom `summarizer` child through its
persisted TOML configuration:

```toml
[agents.worker_blueprints.reviewer]
allowed_child_blueprints = ["summarizer"]
```

That setting controls child eligibility only. Tool denials and global
restrictions still apply, and it does not change task authority, cost, or
security approvals.

Configure a shared fallback model in `~/.jcode/config.toml` if desired. The
shipped Routine, Advanced, and Expert model mappings are unspecified.

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

The `delegation-guard` plugin (see `customizations/plugins/delegation-guard/`) uses this hook
to replace oversized coordinator tool results with a compact delegation nudge,
keeping the coordinator's context small.

## Pre-tool input transformers

Upstream jcode (since v0.89, merged from the former candidate patch
`0001-candidate-pre-tool-input-transformers.patch`) provides a generic
pre-tool transformer hook that lets plugins modify tool inputs before
execution. The `rtk-transform` plugin (see `customizations/plugins/rtk/`) uses this to
rewrite bash commands through RTK.

Configure a pre-tool transformer in `~/.jcode/config.toml`:

```toml
[hooks]
pre_tool_transform = ["~/.jcode/plugins/rtk/rtk-transform"]
```
