# Worker blueprints

This directory versions personal worker blueprints installed locally under
`~/.jcode/worker-blueprints/`.

## Bundled blueprints

| Blueprint | Tool name | Purpose |
| --- | --- | --- |
| `explorer.md` | `swarm_explorer` | Read-only codebase navigation. Answers "where is X?" questions. |
| `bash-runner.md` | `swarm_bash-runner` | Runs shell commands and returns concise output. |
| `fixer.md` | `swarm_fixer` | Fast implementation specialist for small, well-scoped tasks. |
| `research.md` | `swarm_research` | Web and documentation research without file modification. |

All four use a cheap model configured centrally via TOML overrides in
`~/.jcode/config.toml`, not in the blueprint front matter. This makes it
easy to change the model for all workers in one place.

## Install

Symlink the bundled blueprints into the global workers directory:

```bash
mkdir -p ~/.jcode/workers
for f in workers/blueprints/*.md; do
  ln -sf "$(pwd)/$f" "$HOME/.jcode/workers/$(basename $f)"
done
```

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
allowed-tools:
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
   have write tools. Implementation agents should not have internet access.
   Each blueprint's `allowed-tools` list enforces this boundary.

4. **Workers implement, they do not plan.** A fixer receives clear
   instructions and executes. It does not research, spawn subagents, or
   second-guess the task. This keeps the worker fast and cheap.

### Blueprint file format

Each file in `~/.jcode/worker-blueprints/` is a Markdown file with YAML front matter.
The filename is only a source filename; the `name` key is the stable
blueprint ID.

#### Front-matter keys

| Key | Required | Type | Description |
| --- | --- | --- | --- |
| `name` | Yes | String matching `[a-z][a-z0-9_-]*` | Blueprint ID. Produces tool `swarm_<id>`. |
| `description` | Yes | Nonblank string | Used in the generated tool description. |
| `model` | No | Nonblank string | Model string, including bare IDs, explicit routes, `inherit`, or `coordinator`. |
| `effort` | No | Enum: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `swarm`, `swarm-deep` | Reasoning effort. |
| `allowed-tools` | No | YAML array of strings | Restrictive per-session tool allowlist. `[]` means no tools. Omission means no restriction. |
| `communication-policy` | No | Enum: `none`, `report-to-parent`, `parent-and-children`, `same-swarm` | Controls what messages the worker can send. |
| `timeout-minutes` | No | Positive integer | How long the generated tool waits for the worker to complete. |

Unknown keys are diagnosed and ignored. Invalid values are diagnosed and
fall through to the default.

#### Body

The body after the closing `---` delimiter is the worker's durable role
instructions. These are applied first, followed by the runtime task.

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

Override model and effort per blueprint in config:

```toml
[agents.worker_blueprints.reviewer]
model = "openrouter-eu:gpt-5.6-luna"
effort = "high"
timeout-minutes = 30
```

TOML overrides replace front-matter values. Invalid overrides are diagnosed
and fall through to the source value. Only `model`, `effort`, and
`timeout-minutes` are overridable. Source-owned fields (name, description,
allowed-tools, communication-policy, role instructions) remain owned by the
Markdown file.

#### Global defaults

```toml
[agents]
swarm_model = "openrouter-eu:gpt-5.6-terra"
swarm_effort = "medium"
swarm_timeout_minutes = 20
```

#### Coordinator policy

Restrict the coordinator's own tools without affecting spawned workers or
internal agents:

```toml
[agents.coordinator]
allowed_tools = ["read", "agentgrep", "swarm_reviewer", "swarm_fixer"]
```

The effective coordinator policy is the intersection of this allowlist and
the global `[tools]` policy. Applied only to user-facing root sessions.

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
skipped and logged.

### Tool policy

Blueprint `allowed-tools` are applied as a restrictive per-session policy,
intersected with the global `[tools]` policy. A blueprint can further
restrict globally allowed tools but can never restore a globally disabled
tool. An empty `allowed-tools: []` means the worker has no tools. Omission
means no blueprint-specific restriction.

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
