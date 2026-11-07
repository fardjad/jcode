# Worker blueprints

This directory versions personal worker blueprints installed locally under
`~/.jcode/worker-blueprints/`.

## Worker evaluations

`customizations/workers/` organizes worker-specific assets alongside [`blueprints/`](blueprints/):

- [`evals/scenarios/`](evals/scenarios.md): real-code scenarios with
  outcome-only prompts, independent acceptance, named metrics, harness-focused
  judge criteria, simulated-user behavior (no schema-version field), and the
  adjacent pinned [`fixtures/`](evals/README.md) with provenance, licenses, and
  acceptance details.
- `evals/config.toml`: one owner-only shared config with the live provider
  credentials (ignored, mode 600); public per-scenario overrides live in each
  scenario `config.toml`'s `settings` mapping, and retired local per-scenario
  TOML copies archive under the ignored `evals/results/migration-archives/`.
  Only the credential-free [`evals/config.toml.example`](evals/config.toml.example)
  ships in the catalog.
- `evals/results/`: ignored complete evidence bundles.

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
[`../../tools/evaluator/`](../../tools/evaluator/README.md). Bundled workers are examples
discovered dynamically at runtime, not an evaluator registry: adding, removing,
or renaming
a worker requires no runner or judge change. Only `coordinator` is reserved.

## Bundled blueprints

| Blueprint | Tool name | Purpose |
| --- | --- | --- |
| `coordinator.md` | (reserved, no tool) | Coordinator prompt and tool policy. Not a spawnable worker. |
| `routine.md` | `swarm_routine` | Low-ambiguity bounded work, including inspect/edit/check loops and localized verification. |
| `advanced.md` | `swarm_advanced` | Moderate implementation/debugging, scoped assistance to Routine, and bounded read-only Routine extraction. |
| `expert.md` | `swarm_expert` | Hard reasoning, cross-cutting diagnosis, scoped assistance to Advanced, and bounded read-only Routine extraction. |

The former `fixer`, `investigator`, and `research` worker IDs are retired.
Update coordinator assignments and personal blueprint overrides from those IDs
when upgrading. Copy-mode setup refuses updates when local edits or extra files
exist inside the managed worker-blueprints directory, preserving them rather
than silently overwriting them. Unrelated assets outside managed directories
remain untouched. Linked mode exposes this checkout directly.

### Worker tool policy

Routine, Advanced, and Expert deliberately omit `enabled-tools`, which means no
blueprint-specific tool allowlist and broad task-relevant tools. Their
`allowed-child-blueprints` front matter controls which child blueprints they
may spawn: Advanced and Expert allow only `routine`, while Routine allows none.
An omitted child list allows no children by default. An effective persisted
`allowed_child_blueprints` value in TOML overrides the blueprint front matter.
This child eligibility list never overrides explicit `disabled-tools` entries,
global tool restrictions, or assignment scope. Routine's explicit denylist
keeps it terminal; Advanced and Expert retain their explicit denials for the
generic `swarm` tool and peer and upward tier tools. The native depth bound,
child model resolution, and child access to the parent's work remain unchanged.
None of this disables the coordinator's ability to spawn workers.

The coordination and routing rules, including the planning-only coordinator
boundary, are owned by [`AGENTS.md`](AGENTS.md). As a second line of defense,
`coordinator.md` front matter grants only a purpose-based allowlist for
planning and coordination: `read` and `write` (plan files only), `todo`,
`skill_manage` (skill access only), and the generated tier-spawn tools
(`swarm_*` matches every `swarm_<id>` tool, current or future). An explicit
`swarm` deny keeps the generic tool excluded even though it would not match
that allowlist; disabled takes precedence over enabled. Any other tool,
including a newly added execution or research tool, stays unavailable to the
coordinator by default until this allowlist is explicitly widened.
`skill_manage` access does not weaken the planning-only boundary; reading or
loading a skill grants no execution permission. Restricting `read`/`write` to
plan paths is prompt-level discipline, not a sandbox.

All tiers use `report-to-parent`. Routing, the planning-only coordinator
boundary, parallel or sequential planning discovery, independent final
verification, and blocker assistance are governed by
[`AGENTS.md`](AGENTS.md).

Worker-originated spawning is governed by the effective child-blueprint
allowlist as well as tool denials and the assignment. In these shipped roles,
Advanced and Expert may use a Routine child only for a bounded read-only
extraction inside their own scope; the full nested-extraction rules, child
model resolution, and lineage budget are owned by [`AGENTS.md`](AGENTS.md).

| Tier | Capability | Suitable use |
| --- | --- | --- |
| Routine | Straightforward, low-ambiguity work | Known procedures, localized fixes, bounded inspect/edit/test loops |
| Advanced | Moderate reasoning and implementation | Debugging or narrowly scoped help to Routine |
| Expert | Hard reasoning and cross-cutting diagnosis | Complex blockers or narrowly scoped help to Advanced |

Model choices remain unspecified in shipped blueprints. Configure each tier
through ordinary per-blueprint TOML overrides if desired; existing global swarm
model fallback continues to apply.

Child blueprint eligibility is independent of tool allowlists. A blueprint's
`allowed-child-blueprints` front matter declares its default child IDs. The
persisted TOML `allowed_child_blueprints` setting for that blueprint replaces
that default; if neither is set, the worker may spawn no blueprint children.
The native depth limit and existing child model and parent-work access rules do
not change when the child allowlist is configured. For example, a custom
`reviewer` can be configured to use a custom `summarizer` child without making
either ID a built-in tier:

```toml
[agents.worker_blueprints.reviewer]
allowed_child_blueprints = ["summarizer"]
```

This setting only authorizes a child blueprint candidate. Explicit tool denies,
global restrictions, and the current assignment remain binding; it does not
grant implementation authority or change cost or security approvals.

`coordinator.md` is a special reserved blueprint. Its filename supplies the
reserved `coordinator` ID; it is never registered as a spawnable worker tool
and no `swarm_coordinator` tool is generated. Its YAML front matter must be
present and declares a purpose-based `enabled-tools` allowlist (`read`,
`write`, `todo`, `skill_manage`, `swarm_*`) plus an explicit `swarm` deny;
generated tier tools remain available because `swarm_*` matches every
`swarm_<id>` tool. Its Markdown body is the coordinator's durable system
prompt segment, injected only when swarm is enabled. Configure its effective
tool policy through the reserved TOML override.

## Install

Follow the [get started guide](../../docs/get-started.md) to install plugins,
worker blueprints, and skills with `python3 tools/scripts/setup_assets.py`,
including copy fallback and upgrade steps.

Setup includes `coordinator.md` alongside the worker blueprints.

### Tier assignment contract

The assignment, blocker-assistance, and report contracts are owned by
[`AGENTS.md`](AGENTS.md) and the shipped blueprint prompts, not duplicated
here. This guide keeps one mechanical blocked-report example for shape only.

Use a complete blocked report, not a lossy summary. This synthetic example's
assignment sets exactly two failed probes as its attempt cap; that scenario-
local value is not a reusable blueprint-wide probe count or quota. Follow the
actual assignment and stop earlier when required.

```text
BLOCKED
PARTIAL: The implementation is incomplete; the useful edits below are
preserved.
Objective/current state: make the example endpoint return the expected result;
the test environment has no configured service fixture.
Attempts and evidence (each command is an acceptance probe):
1. Hypothesis: the endpoint route is registered. Action/command:
   `python3 -m unittest tests.test_example.ApiTests.test_endpoint`.
   Check: endpoint returns HTTP 200. Result: exit status 1 (FAILED),
   AssertionError: expected 200, got 503.
2. Hypothesis: the fixture is loaded by the test runner. Action/command:
   `python3 -m unittest tests.test_example.ApiTests.test_fixture`.
   Check: the configured fixture is available. Result: exit status 1 (FAILED),
   RuntimeError: service fixture is not configured.
Files/state preserved: edits in `src/example.py` and
`tests/test_example.py` remain in the worktree and are uncommitted.
Uncompleted work: confirm the endpoint response after a fixture is available;
neither probe established the requested behavior.
ESCALATION: Please provide or authorize a test service fixture, then assign a
worker to rerun the endpoint check and finish the response assertion.
```

```coordination
{"event": "blocked", "task_id": "<assigned-task-id>", "blocker_id": "<assigned-blocker-id>", "source_session_id": null}
```

Acceptance judgment and the complete-report contract are owned by
[`AGENTS.md`](AGENTS.md) and the shipped blueprint prompts. Follow-up reads
page completed output and do not resume worker execution.

### Portable execution discipline

These contracts apply across projects. Repository-specific procedures belong
in project instructions or the dynamic assignment, not in reusable roles. The
execution, report, and escalation contracts are owned by
[`AGENTS.md`](AGENTS.md) and the shipped blueprint prompts; these are prompt
contracts, not new runtime enforcement.

### Artifact paths

For large temporary files, builds, and clones, use native `JCODE_SCRATCH_DIR`
with a unique task subdirectory. The runtime may supply this only to shell
commands and it is not necessarily worker-specific. Reports and notes use exact
absolute paths assigned by the coordinator, including project-required
locations when permitted. File tools do not expand shell variables. Do not probe
personal configuration to discover scratch or create literal placeholder
directories.

## Configure

For examples, replace `<tier>` with `routine`, `advanced`, or `expert` in
`[agents.worker_blueprints.<tier>]`. Shipped model mappings are unspecified;
choose models in your local configuration if needed. A global
`agents.swarm_model` fallback remains available.

```toml
[agents.worker_blueprints.routine]
model = "provider:model-id"

[agents.worker_blueprints.advanced]
model = "provider:model-id"

[agents.worker_blueprints.expert]
model = "provider:model-id"
```

### Coordinator policy

Configure the coordinator's own tool policy via the reserved
`[agents.worker_blueprints.coordinator]` override:

```toml
[agents.worker_blueprints.coordinator]
enabled = ["read", "agentgrep", "swarm_routine", "swarm_advanced", "swarm_expert"]
disabled = ["bash", "swarm_expert"]
```

This uses the same TOML override namespace and policy fields as worker
blueprints. The coordinator accepts only `enabled` and `disabled`; worker-only
fields like `model` and `effort` are rejected for the reserved ID. The
effective coordinator policy is combined with global `[tools]` policy using
the shared pattern-aware evaluator. The shipped front matter already denies
execution and research tools; a TOML override replaces the front-matter value
at that scope, so do not use one to re-enable delegated execution.

Only `coordinator` is reserved. All other worker IDs, including the shipped
`routine`, `advanced`, and `expert` tiers, remain dynamic and configurable.
TOML `enabled` and `disabled` overrides replace blueprint policy at that scope
and can affect the intended recursion guard; do not widen the bounded
nested-extraction graph (Advanced/Expert to read-only Routine extraction only).

## How worker blueprints work

Worker blueprints let you define reusable, specialized subagent roles in
Markdown. A blueprint supplies durable role instructions and declarative
execution policy. At runtime, every valid blueprint generates a coordinator-
visible tool named `swarm_<id>` that spawns a blueprint-backed worker, waits
for it to complete, and returns the worker's full report behind a compact
deterministic preview (2000 chars) with paginated follow-up reads. The preview
is native harness behavior, never a worker instruction to compress its report.

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

Routing, capability, and escalation policy is owned by
[`AGENTS.md`](AGENTS.md). The mechanics: generated tools return compact
deterministic previews with paginated follow-up reads that never resume worker
execution, tool availability is not assignment authority, and each worker
ends with a complete report for the coordinator's decisions.

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

`coordinator.md` requires YAML front matter and declares a purpose-based
`enabled-tools` allowlist for planning and coordination only, plus an
explicit `swarm` deny so the generic tool stays excluded even though it
would not match the allowlist. Generated capability-tier worker tools
remain available to the coordinator unless a personal tool-policy override
restricts them:

```yaml
---
enabled-tools:
  - read
  - write
  - todo
  - skill_manage
  - swarm_*
disabled-tools:
  - swarm
---
```

The allowlist admits only plan-document reads/writes, todo bookkeeping,
skill access, and the generated tier-spawn tools: `swarm_*` matches every
`swarm_<id>` tool, current or future, without naming each one. Any other
tool, including a newly added execution or research tool, stays unavailable
to the coordinator by default until this allowlist is explicitly widened.
The `swarm` deny extends the global disabled set; disabled takes precedence
over enabled, so the generic tool stays excluded as defense in depth even if
a future wildcard match were ever broadened.

`skill_manage` is intentionally included in this allowlist: the coordinator
may list, read, load, and reload skills as planning support, and skill
content grants no execution permission.

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

A worker-originated nested child resolves its model from the effective Routine
blueprint configuration. Tier contracts require the parent to refuse the
nested extraction when that model is missing or would inherit the parent's
model, rather than silently running an expensive parent model.

Effort follows the same structure. Timeout follows:

1. Per-call `timeout_minutes` parameter on the generated tool
2. Blueprint front-matter `timeout-minutes` or TOML override
3. `agents.swarm_timeout_minutes` (default: 20)

### Generated tools

Every valid effective blueprint generates a coordinator-visible tool named
`swarm_<id>`. The tool has three modes:

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

#### Resume mode

Pass `resume_session` (a session ID this tool previously returned) with a
new `task` to continue that finished worker on its existing session: the
server runs a second turn on the same session with no second spawn, and the
resumed worker keeps its original working directory, model, and persisted
blueprint policy.

```
{"resume_session": "abc123", "task": "Now verify the fix on the other
failing case and report the exit status."}
```

Exact policy, enforced by the server before any queueing or dispatch:

- Resume is scoped to the tool's own blueprint: only that blueprint's
  workers can be resumed, and only the swarm coordinator or the worker's
  trusted immediate parent may resume. The generic `swarm` tool gains no
  new mode and keeps its current behavior.
- A worker in an active status is rejected atomically: nothing is queued,
  no interrupt is sent, and the tool reports a retry-later error.
- `resume_session` cannot combine with `follow_up_session`, `working_dir`,
  or `spawn_mode`; a blank `resume_session` falls back to spawn mode.
- Success returns only the worker's new report past the resume point,
  footered with the same session ID; a turn that produces no report fails
  honestly. Page the retained report later with `follow_up_session`.

#### Tool parameters

| Parameter | Required | Description |
| --- | --- | --- |
| `task` | Yes (spawn mode); required with `resume_session` | Nonblank dynamic task. |
| `follow_up_session` | Yes (follow-up mode) | Session ID from a prior spawn. |
| `resume_session` | Yes (resume mode) | Session ID this tool returned; continues that finished worker with `task`. |
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
