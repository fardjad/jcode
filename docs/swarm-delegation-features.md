# Swarm worker delegation features

Worker blueprints define reusable roles in Markdown. For why the shipped setup
uses them, see the [efficiency rationale](llm-efficiency-rationale.md); for
installing and configuring the shipped roles, see
[worker blueprints](../customizations/workers/README.md). The native policy
dispatcher, check limits, assistance model, and deployment status are described
in [policy-driven delegation](policy-driven-delegation.md). The checkout copy
of the external guard plugin is retired. The native guard needs no plugin or
setup hook, but requires a binary built with patch `0008-worker-blueprints-and-policy-driven-dispatch.patch`. The running
installation has not been updated or verified for this behavior.

## Blueprint files

Patch
`0008-worker-blueprints-and-policy-driven-dispatch.patch`
loads direct `*.md` files from `~/.jcode/worker-blueprints/`. A missing
directory is an empty registry. Set `agents.worker_blueprints_dir` to change
the directory; `~` home expansion is supported.

An ordinary blueprint has YAML front matter and a Markdown body. The body is
the worker's durable role prompt, followed by the runtime assignment. Its
front matter supports:

- `name`: required ID matching `[a-z][a-z0-9_-]*`; `coordinator` is reserved.
- `description`: required nonblank description for the generated tool.
- `model`: optional model ID, route, `inherit`, or `coordinator`.
- `effort`: optional reasoning effort (`none`, `minimal`, `low`, `medium`,
  `high`, `xhigh`, `max`, `swarm`, or `swarm-deep`).
- `enabled-tools` and `disabled-tools`: optional YAML lists defining tool
  policy. An omitted enabled list adds no restriction; `[]` permits no tools.
- `allowed-child-blueprints`: optional list of worker IDs eligible for nested
  spawning; omission permits none.
- `communication-policy`: `none`, `report-to-parent`, `parent-and-children`,
  or `same-swarm`.
- `timeout-minutes`: optional positive integer.
- `input-schema` and `output-schema`: optional inline YAML JSON Schemas for
  assignments and reports.

For example:

```yaml
---
name: reviewer
description: Reviews changes for correctness and missing tests.
enabled-tools: [read, agentgrep, bash]
communication-policy: report-to-parent
timeout-minutes: 15
---

Review the assigned change and report findings with evidence.
```

Invalid schemas exclude the blueprint; other invalid values are diagnosed and
may fall through to defaults. Unknown keys are diagnosed and ignored.
`allowed-tools` is not accepted; use `enabled-tools`. Blueprint loading is
nonfatal: a bad worker file does not prevent valid workers from loading.

The special `coordinator.md` derives its reserved ID from its filename, not a
`name` key. It supplies a coordinator prompt segment and tool policy but never
generates `swarm_coordinator`. Its YAML front matter declares `enabled-tools`
and `disabled-tools`. A malformed file disables swarm without a partial
surface; a missing file warns but permits swarm without that prompt or policy.

The shipped coordinator permits `read`, `write`, `todo`, `skill_manage`, and
`swarm_*`, and explicitly denies generic `swarm`. Its planning-only boundary
is described in [AGENTS.md](../AGENTS.md); plan-only reads and writes are
prompt discipline, not a sandbox.

## Structured contracts

When a worker declares `input-schema`, the generated tool accepts a structured
`assignment` based on that schema for spawn and resume. Without it, the tool
accepts a free-text `task`. With `output-schema`, the worker reports through
the generated `submit_report` tool; its arguments are validated against the
schema. Missing or invalid reports receive bounded correction attempts, then
fail rather than silently succeeding. Schemas validate shape, not truth.
Workers without an output schema retain the ordinary report behavior.

The shipped tiers define their assignment fields and report structure directly
in their blueprint front matter. Reserved generated-tool control parameters
cannot be declared as assignment fields.

## Generated tools and sessions

Each valid ordinary blueprint produces a `swarm_<id>` tool. A name collision
with a built-in, MCP, or another generated tool skips that tool and is logged.
The modes are:

- **Spawn:** provide an assignment or `task`; the response includes a compact
  preview and a session ID. `working_dir`, `spawn_mode` (`visible`, `headless`,
  `inline`, or `auto`), and `timeout_minutes` are optional controls.
- **Follow-up:** provide `follow_up_session` and optional character `offset`
  and `limit` to read completed output without starting or resuming a turn.
  The default page limit is 2,000 characters.
- **Resume:** provide `resume_session` and a new assignment or `task` to
  continue a finished worker in the same session. Only the coordinator or
  immediate parent can resume a worker through its own blueprint tool.
  Active workers are rejected; `resume_session` cannot be combined with
  `follow_up_session`, `working_dir`, or `spawn_mode`.

The initial preview is limited to 2,000 characters; the complete report
remains available through follow-up reads. A resumed turn returns its new
report and the same session ID.

## Configuration and policy

Enable swarm and optionally set shared defaults:

```toml
[features]
swarm = true

[agents]
swarm_model = "provider:model-id"
swarm_effort = "medium"
swarm_timeout_minutes = 20
worker_blueprints_dir = "~/my-blueprints"
```

Per-blueprint TOML overrides replace front-matter values at that scope:

```toml
[agents.worker_blueprints.reviewer]
model = "provider:model-id"
effort = "high"
timeout-minutes = 30
enabled = ["read", "agentgrep"]
disabled = ["bash"]
allowed_child_blueprints = ["summarizer"]
```

An omitted override inherits the front matter; invalid values are diagnosed
and fall through. `enabled = []` clears the override's enabled restriction,
unlike `enabled-tools: []` in front matter. The reserved coordinator override
accepts only `enabled` and `disabled`.

Tool policy is combined with global `[tools]` restrictions: a blueprint cannot
restore a globally disabled tool. Exact names, aliases, `*`/`all`, anchored
patterns such as `swarm_*`, and the `mcp` family shorthand are supported;
disabled takes precedence. Child eligibility is separate from tool policy and
assignment authority. A TOML `allowed_child_blueprints` list replaces the
front-matter list; nested children remain subject to the native depth limit,
explicit child model requirements, and parent-work access rules.

Model precedence is per-invocation model, per-blueprint TOML override, front
matter, global `swarm_model`, then coordinator identity. Explicit `inherit` or
`coordinator` bypasses the global fallback. Effort follows the same structure;
timeout precedence is per-call, blueprint or override, then the global default.

Communication policy is enforced in routing: `none` blocks ordinary messages
but not a tool's completion result; `report-to-parent` permits parent reports;
`parent-and-children` permits parent and child communication; `same-swarm`
permits communication within the swarm.

## Tool output and command hooks

The native worker output guard bounds each result, not cumulative output from
many small reads. It saves withheld payloads in a private runtime directory for
authorized extraction. Configure it as described in
[policy-driven delegation](policy-driven-delegation.md).

The [RTK plugin](../customizations/plugins/README.md) uses upstream's
`pre_tool_transform` hook to rewrite eligible bash commands.
