# Worker blueprints

This directory versions personal worker blueprints installed locally under
`~/.jcode/worker-blueprints/`.

## Bundled blueprints

| Blueprint | Tool name | Purpose |
| --- | --- | --- |
| `coordinator.md` | (reserved, no tool) | Coordinator prompt and tool policy. Not a spawnable worker. |
| `explorer.md` | `swarm_explorer` | Read-only codebase navigation. Answers "where is X?" questions. |
| `bash-runner.md` | `swarm_bash-runner` | Runs shell commands and returns concise output. |
| `fixer.md` | `swarm_fixer` | Fast implementation specialist for small, well-scoped tasks. |
| `research.md` | `swarm_research` | Web and documentation research without file modification. |
| `automation.md` | `swarm_automation` | Browser, UI, and Gmail workflows. |

### Built-in tool ownership

Every worker-executable built-in tool is assigned to at least one worker.
Coordinator orchestration tools, including `todo`, `initiative`, and
`schedule`, intentionally remain with the coordinator and are not delegated.
Generated `swarm_<id>` tools are coordinator controls and are not included in
this ownership list.

| Worker | Owned tools |
| --- | --- |
| `swarm_explorer` | `agentgrep`, `conversation_search`, `ls`, `memory`, `read`, `session_search` |
| `swarm_bash-runner` | `bash`, `bg`, `read` |
| `swarm_fixer` | `agentgrep`, `apply_patch`, `batch`, `bash`, `bg`, `debug_socket`, `edit`, `ls`, `multiedit`, `patch`, `read`, `selfdev`, `write` |
| `swarm_research` | `jcode_docs`, `read`, `webfetch`, `websearch` |
| `swarm_automation` | `browser`, `gmail`, `macos_computer_use`, `open`, `side_panel` |

All ordinary workers also have `mcp`, `mcp_search`, `mcp_call`, `skill_manage`,
and `jcode_docs`. These tools are available only for explicitly requested,
task-scoped capabilities. Workers must discover MCP tools before calling them,
must obtain explicit approval before connecting untrusted servers or taking
consequential remote actions, and must not use MCP for arbitrary browsing or
open-ended internet research.

The automation worker owns `open` because it supports user-facing UI and
artifact workflows. These assignments do not change the existing internet-use
boundary: research remains the web and documentation role, while other workers
may use network-capable tools only for their explicitly requested, task-scoped
capability.

Ordinary worker blueprints may set a `model` and `effort` in front matter.
Use TOML overrides in `~/.jcode/config.toml` to centrally replace those values
without editing every blueprint.

`coordinator.md` is a special reserved blueprint. Its filename supplies the
reserved `coordinator` ID; it is never registered as a spawnable worker tool
and no `swarm_coordinator` tool is generated. Its YAML front matter must be
present and explicitly declares empty tool lists. Its Markdown body is the
coordinator's durable system prompt segment, injected only when swarm is
enabled. Configure its effective tool policy through the reserved TOML
override.

## Install

Symlink the bundled blueprints into the default blueprint directory:

```bash
mkdir -p ~/.jcode/worker-blueprints
for f in workers/blueprints/*.md; do
  ln -sf "$(pwd)/$f" "$HOME/.jcode/worker-blueprints/$(basename "$f")"
done
```

This installs `coordinator.md` alongside worker blueprints using the same
mechanism. No separate prompt-overlay installer is needed.

## Configure

Set the model for all blueprints in `~/.jcode/config.toml`:

```toml
[agents.worker_blueprints.explorer]
model = "openrouter-eu:gpt-5.6-luna"

[agents.worker_blueprints.bash-runner]
model = "openrouter-eu:gpt-5.6-luna"

[agents.worker_blueprints.fixer]
model = "openrouter-eu:gpt-5.6-luna"

[agents.worker_blueprints.research]
model = "openrouter-eu:gpt-5.6-luna"
```

### Coordinator policy

Configure the coordinator's own tool policy via the reserved
`[agents.worker_blueprints.coordinator]` override:

```toml
[agents.worker_blueprints.coordinator]
enabled = ["read", "agentgrep", "swarm_explorer", "swarm_fixer"]
disabled = ["bash", "swarm_automation"]
```

This uses the same TOML override namespace and policy fields as worker
blueprints. The coordinator accepts only `enabled` and `disabled`; worker-only
fields like `model` and `effort` are rejected for the reserved ID. The
effective coordinator policy is combined with global `[tools]` policy using
the shared pattern-aware evaluator.

---

## How worker blueprints work

Worker blueprints let you define reusable, specialized subagent roles in
Markdown. A blueprint supplies durable role instructions and declarative
execution policy. At runtime, every valid blueprint generates a
coordinator-visible tool named `swarm_<id>` that spawns a blueprint-backed
worker, waits for it to complete, and returns a compact summary with
paginated follow-up reads.

### Quick start

Create a Markdown file in `~/.jcode/worker-blueprints/`:

```markdown
---
name: reviewer
description: Reviews changes for correctness, regressions, and missing tests.
model: openai-api:gpt-5.5
effort: medium
enabled-tools:
  - read
  - agentgrep
  - bash
  - apply_patch
communication-policy: report-to-parent
timeout-minutes: 15
---

You are a rigorous code reviewer. Prioritize correctness and concrete evidence.

Review the assigned change, identify defects and missing tests, and report
findings with file and line evidence.
```

After saving, the coordinator sees a new tool `swarm_reviewer` on its next
session. No restart needed.

### Design principles

1. **Keep workers narrow and cheap.** Pin a cheap model for routine work,
   restrict its tool access, control its communication, and cap its runtime.
   The coordinator does the expensive thinking; workers do the cheap
   execution.

2. **Bounded results by default.** The generated tool returns a compact
   summary (2000 chars) so the coordinator does not burn tokens reading the
   worker's full output. A deterministic follow-up read (no LLM) lets the
   coordinator page through more output in 2000-char chunks when needed.

3. **Separate read-only from write-capable.** Exploration agents should not
   have write tools. Non-research workers must not use available browser,
   Gmail, MCP, or shell capabilities for arbitrary or open-ended internet
   access. Task-specific exceptions are limited to automation's explicitly
   requested UI or email workflow and a shell worker's explicitly requested
   specific network operation. MCP is available to every ordinary worker only
   for explicitly requested, task-scoped capabilities. Research remains the web
   and documentation research role. Each blueprint's `enabled-tools` list
   enforces the capability boundary.

4. **Workers implement, they do not plan.** A fixer receives clear
   instructions and executes. It does not research, spawn subagents, or
   second-guess the task. This keeps the worker fast and cheap.

5. **Escalate capability gaps once.** A worker that lacks a needed specialty,
   tool, permission, source, or decision does not guess or widen its scope.
   It returns one concise `ESCALATION` line naming the gap, precise question,
   evidence, and recommended specialist, followed by any useful partial
   result. The coordinator decides whether and how to delegate the next step.

### Blueprint file format

Each file in `~/.jcode/worker-blueprints/` is a Markdown file with YAML front
matter. The filename is only a source filename; the `name` key is the stable
blueprint ID.

The special `coordinator.md` file is an exception: its filename supplies the
reserved `coordinator` ID. It requires YAML front matter with the supported
`enabled-tools` and `disabled-tools` keys. The bundled file leaves both lists
empty; its Markdown body is the coordinator prompt segment. Coordinator tool
policy is configured only through `[agents.worker_blueprints.coordinator]` in
TOML, not through front matter.

#### Front-matter keys (ordinary worker blueprints)

| Key | Required | Type | Description |
| --- | --- | --- | --- |
| `name` | Yes | String matching `[a-z][a-z0-9_-]*` | Blueprint ID. Produces tool `swarm_<id>`. The reserved ID `coordinator` is rejected. |
| `description` | Yes | Nonblank string | Used in the generated tool description. |
| `model` | No | Nonblank string | Model string, including bare IDs, explicit routes, `inherit`, or `coordinator`. |
| `effort` | No | Enum: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `swarm`, `swarm-deep` | Reasoning effort. |
| `enabled-tools` | No | YAML array of strings | Restrictive per-session tool allowlist. `[]` means no tools. Omission means no restriction. |
| `disabled-tools` | No | YAML array of strings | Per-session tool denylist. `["*"]` disables every tool. Omission means no deny restriction. |
| `communication-policy` | No | Enum: `none`, `report-to-parent`, `parent-and-children`, `same-swarm` | Controls what messages the worker can send. |
| `timeout-minutes` | No | Positive integer | How long the generated tool waits for the worker to complete. |

Unknown keys are diagnosed and ignored. Invalid values are diagnosed and
fall through to the default. The old `allowed-tools` key is no longer
accepted; use `enabled-tools` instead.

#### coordinator.md front matter

`coordinator.md` requires YAML front matter. The bundled file explicitly
leaves both supported fields empty:

```yaml
---
enabled-tools: []
disabled-tools: []
---
```

Its nonblank Markdown body is loaded as the coordinator prompt segment. The
current runtime applies coordinator tool policy from the `enabled` and
`disabled` keys in the reserved TOML override shown above.

#### Body

The body after the closing `---` delimiter is the worker's durable role
instructions (or, for `coordinator.md`, the coordinator's prompt segment).
These are applied first, followed by the runtime task.

### Configuration

#### Blueprint directory

Default: `~/.jcode/worker-blueprints/`. Change with:

```toml
[agents]
worker_blueprints_dir = "~/my-blueprints"
```

Supports `~`, `~/...`, and `~\...` expansion. Only direct `*.md` files are
loaded (no recursion). A missing directory is an empty registry, not an
error.

#### Per-blueprint overrides

Override model, effort, timeout, and tool policy per blueprint in config:

```toml
[agents.worker_blueprints.reviewer]
model = "openrouter-eu:gpt-5.6-luna"
effort = "high"
timeout-minutes = 30
enabled = ["read", "agentgrep"]
disabled = ["bash"]
```

TOML overrides replace front-matter values. A present `enabled = []` clears
the source enabled restriction (unrestricted at this scope). An omitted
override inherits the source value. Invalid overrides are diagnosed and fall
through to the source value.

The reserved `coordinator` ID accepts only `enabled` and `disabled`; worker-only
fields (`model`, `effort`, `timeout-minutes`) are rejected for it. Coordinator
front matter does not set tool policy.

#### Global defaults

```toml
[agents]
swarm_model = "openrouter-eu:gpt-5.6-terra"
swarm_effort = "medium"
swarm_timeout_minutes = 20
```

### Tool policy

Worker blueprints' `enabled-tools` and `disabled-tools` are applied as a
restrictive per-session policy, combined with the global `[tools]` policy. A
blueprint can further restrict globally allowed tools but can never restore a
globally disabled tool. An empty `enabled-tools: []` means the worker has no
tools. Omission means no blueprint-specific restriction.

For the coordinator, configure `enabled` and `disabled` in the reserved TOML
override. These are combined with global `[tools]` policy. `coordinator.md`
front matter does not affect tool policy.

Tool names support exact names, aliases, `"*"`/`"all"` wildcards, and anchored
whole-name patterns such as `swarm_*` and `mcp_*`. The `mcp` family shorthand
is also preserved. Disabled takes precedence over enabled.

### Model and effort precedence

When a blueprint-backed worker is spawned, model is resolved in this order:

1. Explicit per-invocation model (if the API supports it)
2. `agents.worker_blueprints.<id>.model` (TOML override)
3. Blueprint front-matter `model`
4. `agents.swarm_model`
5. Coordinator model, provider key, and route inheritance

An explicit `inherit` or `coordinator` at a higher level bypasses
`agents.swarm_model` and selects coordinator identity.

Effort follows the same structure. Timeout follows:

1. Per-call `timeout_minutes` parameter on the generated tool
2. Blueprint front-matter `timeout-minutes` or TOML override
3. `agents.swarm_timeout_minutes` (default: 20)

### Generated tools

Every valid effective blueprint generates a coordinator-visible tool named
`swarm_<id>`. The tool has two modes:

#### Spawn mode

Pass `task` to spawn a worker, wait for completion, and get a compact
summary (2000 chars) plus the worker's session ID.

If the output is truncated, the result includes:

```
[... truncated: showing 2000 of 8500 chars. Use follow_up_session with offset/limit to read more.]

Worker session: abc123
(Pass follow_up_session: "abc123" with offset/limit to read more output.)
```

#### Follow-up mode

Pass `follow_up_session` (the session ID from a prior call) with `offset`
and `limit` to read a paginated chunk of the worker's full output
deterministically. No LLM, no spawn. Default limit: 2000 chars.

```
Output from session abc123 (chars 2000-4000 of 8500):

... content ...

[... 4500 more chars. Pass offset=4000 to continue reading.]
```

#### Tool parameters

| Parameter | Required | Description |
| --- | --- | --- |
| `task` | Yes (spawn mode) | Nonblank dynamic task. |
| `follow_up_session` | Yes (follow-up mode) | Session ID from a prior spawn. |
| `offset` | No | Character offset for follow-up reads. Default: 0. |
| `limit` | No | Max chars for follow-up reads. Default: 2000. |
| `working_dir` | No | Working directory for the spawned worker. |
| `spawn_mode` | No | `visible`, `headless`, `inline`, or `auto`. |
| `timeout_minutes` | No | Per-call timeout override. |

#### Tool-name collisions

Generated tools never overwrite existing tool names. If a collision is
detected (built-in, MCP, or another generated tool), the generated tool is
skipped and logged. The reserved `coordinator` ID never generates a tool.

### Communication policy

Enforced in message-routing and subscription paths, not through prompts.

| Policy | Behavior |
| --- | --- |
| `none` | Blocks ordinary worker direct, channel, and broadcast messages. Does not suppress the completion result of a `swarm_<id>` invocation (that is a tool result, not a routed message). |
| `report-to-parent` | Worker reports back to its spawning coordinator. |
| `parent-and-children` | Worker communicates with its parent and children. |
| `same-swarm` | Worker communicates with all members of its swarm. |

### Diagnostics

Blueprint loading is nonfatal. Bad files are excluded; valid blueprints
remain usable. Diagnostics are structured and actionable, identifying the
source path, exact problem, and what was excluded or what fallback applies.
A malformed `coordinator.md` leaves swarm disabled with no partial surface;
a missing `coordinator.md` warns once per load attempt but permits swarm with
no coordinator-specific prompt or policy.
