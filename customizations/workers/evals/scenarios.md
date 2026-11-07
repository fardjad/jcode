# Real-code evaluation scenarios

Each `scenarios/<id>/config.toml` file defines one scenario for `just
eval-workers`.
Scenarios configure a real task, independent acceptance, named metrics,
harness-focused diagnostic judge criteria, and an LLM simulated user. The
bundled scenarios cover RefactorBench Flask, Aider Polyglot REST API, and Aider
Polyglot Rust `dot-dsl`, plus the original `stable-task-order` diagnostic
fixture. Its `coordination` metric observes optional structured escalation and
handback evidence without requiring any tier or handoff. Untouched and
known-good fixture controls run in the offline worker tests.

The existing scenarios remain suitable for paired correctness/cost/time runs.
The scheduler adds localized dependency-order diagnosis and edge cases. It does
not guarantee natural escalation, Expert use, or environment-blocker coverage.
Those live behaviors remain unverified until model/budget-approved evaluations.
Synthetic trace tests cover assistance, handback, repeated blockers, and missing
identity evidence. See the [evaluator README](../../../tools/evaluator/README.md)
for per-worker models, usage breakdowns, and offline comparisons.

## Schema

Scenarios have no version field. Every scenario contains exactly `id`,
`fixture`, `prompt`, `accept`, `metrics`, `judge`, and `simulated_user`;
unknown fields, including `version`, are rejected.

```toml
id = "polyglot-rest-api-fixer"
fixture = "fixtures"
prompt = "In the current workspace, implement RestAPI according to README.md so that python3 -m unittest rest_api_test.py passes completely."
accept = ["python3", "-m", "unittest", "rest_api_test.py"]
metrics = ["tool_health"]

[judge]
criteria = [
  "Good coordination keeps the coordinator inside its planning-only boundary: it owns decisions, scope, and acceptance judgment, and delegates every execution step. Direct coordinator execution of exploration, research, shell, build, or test commands, or any implementation or mechanical edit is a boundary violation regardless of task size. Inappropriate worker non-use is a coordinator finding.",
  "Good spawned workers follow their actual blueprint and assignment, stay in scope, make bounded progress, and report checks, blockers, and unfinished work truthfully.",
  "Tool and lifecycle findings use correlated schemas, policies, inputs, outcomes, and hook evidence to identify small harness improvements without guessing root causes.",
]

[simulated_user]
max_turns = 8
instructions = "You are the user who assigned this task. Approve reasonable plans when asked, and encourage the coding agent to make sound decisions itself. Do not provide code, invent requirements, or weaken acceptance. Continue until the agent explicitly announces completion."
```

`fixture` is a relative path to a non-symlink directory whose contents contain
no symlinks; the bundled scenarios use the adjacent `fixtures` directory. `accept` is one argv list executed directly without a shell from
the materialized workspace root. Prompts need no workspace substitution. Do not
add worker names, delegation flags, fixed responses, tool-call choreography, or
an implementation chain to prompts.

## Named metrics

`metrics` selects built-in deterministic analyzers. `tool_health` currently
inventories tool inputs, request schemas, policies, outcomes, retries, lifecycle
events, worker identity, and hook traces. It reports facts and missing evidence,
not guessed causes. Results appear in `metrics.json` and the scenario summary;
`tool-health.json` is retained when selected. Unknown metric IDs fail preflight
before provider calls. Future built-ins use the same list and registry without a
new schema shape.

The `escalation-resumption` scenario selects `escalation_resumption`, which
analyzes the controller state, event journal, guard journal, retained
artifacts, and spilled replacements the runner copies into the evaluation raw
root before temporary cleanup. The runner sets the controller mode explicitly
to `blind` and records it in the manifest and effective provenance; the metric
resolves the mode from the event journal, and journals without a mode field
are historical scripted evidence on the unchanged scripted path. Blind
reports are observational: a private judge-aware fault timeline, behavior
coverage with no required tier route, artifact shielding, and unknowns for
missing evidence; unexercised coverage is reported, never a violation, and
independent `accept` remains the sole correctness gate.

## Judge criteria

Judge-criteria authoring rules, including the planning-only coordinator
boundary, worker non-use review, and evidence/citation requirements, are owned
by [`../AGENTS.md`](../AGENTS.md). Independent `accept` is the sole
correctness gate, and criteria never name fixed worker IDs.

## Simulated user

The `simulator` model is selected in private settings; its behavior and turn cap
are scenario data. After each normal coordinator turn, the Pydantic AI actor
receives the original task and conversation and returns strict `{done, message}`
structured output. A false reply is sent as the next native user message. `done`
is permitted only when the agent explicitly announces completion. Sanitized
requests and responses are retained under `raw/simulated-user/`.

Use instructions such as approving reasonable plans and encouraging independent
agent decisions. Do not supply solutions, new requirements, or weaker checks.
Turn-cap exhaustion is an execution error; acceptance still runs independently.

## Acceptance and controls

Acceptance is the sole correctness gate and must independently test the
requested outcome rather than parse an assistant report. Execution and
acceptance results remain separate. Full output and final source differences are
retained. Before adding a scenario, verify from a materialized workspace that:

1. the untouched fixture fails the intended acceptance command for the expected
   reason;
2. a known-good solution passes without modifying the verifier;
3. a no-op cannot pass;
4. required tools are available in the isolated environment and generated build
   artifacts can stay outside the source fixture (Cargo uses an external target
   directory).

Keep fixtures small, pinned, and licensed, and document them in
[`README.md`](README.md). Never weaken acceptance to
obtain a pass. Live coordinator/worker, simulated-user, and judge calls may cost
money; unit tests load scenarios but never invoke providers.

Run every bundled scenario once with `just eval-workers`, or select an ID with
`just eval-workers <scenario-id>`. Use `/evaluate-worker-blueprints` to review
the result dimensions, total cost, coverage ledger, and finding citations.
