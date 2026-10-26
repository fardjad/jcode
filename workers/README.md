# Worker blueprints

This directory versions personal worker blueprints installed locally under
`~/.jcode/worker-blueprints/`.

## Worker evaluations

`workers/` organizes worker-specific assets alongside [`blueprints/`](blueprints/):

- [`scenarios/`](scenarios/README.md): three real-code scenarios with
  outcome-only prompts, independent acceptance, named metrics, harness-focused
  judge criteria, and simulated-user behavior (no schema-version field).
- [`fixtures/`](fixtures/README.md): pinned benchmark codebases, provenance,
  licenses, and acceptance details.
- [`tests/`](tests/): generic static invariants for the discovered blueprints.
- `.eval-settings.example.toml` / `.eval-settings.toml`: owner-only live provider
  and coordinator/swarm/judge/simulator model settings; the private file is
  ignored.
- `results/`: ignored complete evidence bundles.

Run `just eval-workers` for all scenarios or `just eval-workers <scenario-id>`
for one. These are paid live coordinator/worker, simulated-user, and judge runs
and are separate from `just test-catalog`. Use the project-local
`/evaluate-worker-blueprints` skill for the run/review procedure.

The recipe automatically builds or reuses binaries for the exact resolved
`VERSION.txt` base plus every current ordered catalog patch. Valid fingerprints,
success stamps, source identity, and binary hashes control reuse; an arbitrary
existing debug binary is not accepted. Build logs/stamps live under
`.tools/worker-eval/`, and each run's `manifest.json` records build/reuse
provenance alongside binary, blueprint, plugin, and scenario identities.

Generic runner code and the harness API client live under
[`../tools/evaluator/`](../tools/evaluator/README.md). Bundled workers are examples
discovered dynamically at runtime, not an evaluator registry: adding, removing,
or renaming
a worker requires no runner or judge change. Only `coordinator` is reserved.

## Bundled blueprints

| Blueprint | Tool name | Purpose |
| --- | --- | --- |
| `coordinator.md` | (reserved, no tool) | Coordinator prompt and tool policy. Not a spawnable worker. |
| `fixer.md` | `swarm_fixer` | One mechanical objective with bounded edits or a fix loop and a coordinator-defined result contract. |
| `investigator.md` | `swarm_investigator` | Read-only investigation and shell execution. |
| `research.md` | `swarm_research` | Web and documentation research without file modification. |

### Built-in tool ownership

Every worker-executable built-in tool is assigned to at least one worker.
Coordinator orchestration tools, including `todo`, `initiative`, and
`schedule`, intentionally remain with the coordinator and are not delegated.
Generated `swarm_<id>` tools are coordinator controls and are not included in
this ownership list.

| Worker | Owned tools |
| --- | --- |
| `swarm_fixer` | `read`, `agentgrep`, `bash`, `bg`, `edit`, `apply_patch`, `write` |
| `swarm_investigator` | `agentgrep`, `bash`, `bg`, `conversation_search`, `jcode_docs`, `memory`, `mcp`, `read`, `session_search`, `skill_manage` |
| `swarm_research` | `jcode_docs`, `mcp`, `read`, `skill_manage`, `webfetch`, `websearch` |

The investigator and research workers also enable `mcp` and `skill_manage`;
`jcode_docs` is enabled for the investigator and research workers. MCP server-tool discovery and
invocation use the native `mcp` actions `search` and `call`, not separate
`mcp_search` or `mcp_call` tool names. These capabilities are limited to
explicitly requested, task-scoped use. Workers must obtain explicit approval
before connecting untrusted servers or taking consequential remote actions,
and must not use MCP for arbitrary browsing or open-ended internet research.

The `open` tool is not enabled for any bundled worker. These assignments do
not change the existing internet-use boundary: research remains the web and
jcode documentation role, while the investigator's shell use is limited to
coordinator-specified, read-only investigation.

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

Follow the [get started guide](../docs/get-started.md) to install plugins and
worker blueprints with `python3 tools/scripts/setup_assets.py`, including copy
fallback and upgrade steps.

Setup includes `coordinator.md` alongside the worker blueprints.

### Fixer delegation contract

`swarm_fixer` is intentionally not an implementation or planning specialist.
Delegate one mechanical objective: exact edits in named files, or a bounded
fix-until-green loop with allowed files and an exact acceptance command. State
the required return facts. Fixer may make local, failure-directed corrections
inside that contract, but must escalate rather than choose designs, change
unstated behavior, or broaden scope.

### Portable execution discipline

These contracts apply across projects. Repository-specific procedures belong
in project instructions or the dynamic assignment, not in reusable roles.

- Give each worker one outcome or question, named inputs and allowed actions,
  an acceptance check or answer predicate, and a return contract.
- Count progress as an edit completed, a check observed, a question answered,
  or a blocker narrowed. Retry only with changed inputs or new evidence.
  Stop at success, a precise blocker, or no new evidence.
- Return status first, then results, evidence, and unfinished work. Preserve
  partial changes and live command identifiers. Writable roles use an assigned,
  resolved scratch notes path for recoverable handoffs. Research has no write
  tools and returns its evidence in the final report.
- The coordinator owns decisions, dependencies, verification, shared-resource
  scheduling, and result bookkeeping. Avoid duplicate assignments and shared
  mutable caches or output paths during parallel work. Inspect existing runs
  before rerunning them after a timeout.
- An escalation does not automatically trigger another delegation. Supply a
  concrete prerequisite or take over. After one revised assignment stalls on
  the same blocker, handle the judgment directly or report the constraint.

These are prompt contracts, not new runtime enforcement. Tool permissions and
communication restrictions remain unchanged.

### Artifact paths

For large temporary files, builds, and clones, use native `JCODE_SCRATCH_DIR`
with a unique task subdirectory. The runtime may supply this only to shell
commands and it is not necessarily worker-specific. Reports and notes use
exact absolute paths assigned by the coordinator, including project-required
locations when permitted. File tools do not expand shell variables. Do not
probe personal configuration to discover scratch or create literal placeholder
directories. Research returns evidence in its final report and writes no files.

## Configure

Set the model for all blueprints in `~/.jcode/config.toml`:

```toml
[agents.worker_blueprints.investigator]
model = "openrouter-eu:gpt-5.6-luna"

model = "openrouter-eu:gpt-5.6-luna"

model = "openrouter-eu:gpt-5.6-luna"

[agents.worker_blueprints.research]
model = "openrouter-eu:gpt-5.6-luna"
```

### Coordinator policy

Configure the coordinator's own tool policy via the reserved
`[agents.worker_blueprints.coordinator]` override:

```toml
[agents.worker_blueprints.coordinator]
enabled = ["read", "agentgrep", "swarm_investigator", ""]
disabled = ["bash", "swarm_investigator"]
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

3. **Separate read-only from write-capable.** Exploration roles do not edit
   implementation or configuration. Investigator may write explicitly assigned
   reports and run authorized checks that produce artifacts. Research has no
   shell or write tools. Fixer has no MCP or skills. Only Investigator and
   Research enable MCP and skills, for explicitly requested, task-scoped use.
   No role may use another capability to bypass its restrictions.

4. **Workers execute bounded assignments, they do not own the plan.** Fixer
   performs mechanical edits, Investigator answers local evidence questions,
   and Research retrieves sources. The coordinator owns design, scope, and
   acceptance decisions. Workers do not spawn subagents or broaden tasks.

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
`enabled-tools` and `disabled-tools` keys. Its Markdown body is the coordinator
prompt segment, and its front-matter tool policy can be overridden through
`[agents.worker_blueprints.coordinator]` in TOML.

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

`coordinator.md` requires YAML front matter. The bundled file leaves
`enabled-tools` empty and disables the generic `swarm` tool so the coordinator
uses the generated `swarm_<id>` blueprint tools:

```yaml
---
enabled-tools: []
disabled-tools:
  - swarm
---
```

Its nonblank Markdown body is loaded as the coordinator prompt segment. The
runtime applies coordinator tool policy from `coordinator.md` front matter,
overridden when present by the `enabled` and `disabled` keys in the reserved
TOML override shown above.

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
fields (`model`, `effort`, `timeout-minutes`) are rejected for it.

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

For the coordinator, configure `enabled-tools` and `disabled-tools` in
`coordinator.md` front matter or `enabled` and `disabled` in the reserved TOML
override. These are combined with global `[tools]` policy.

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
