# Real-code evaluation scenarios

Each top-level `*.yaml` file defines one scenario for `just eval-workers`.
Scenarios configure a real task, independent acceptance, named metrics,
harness-focused diagnostic judge criteria, and an LLM simulated user. The
bundled scenarios cover RefactorBench Flask, Aider Polyglot REST API, and Aider
Polyglot Rust `dot-dsl`.

## Schema

Scenarios have no version field. Every scenario contains exactly `id`,
`fixture`, `prompt`, `accept`, `metrics`, `judge`, and `simulated_user`;
unknown fields, including `version`, are rejected.

```yaml
id: polyglot-rest-api-fixer
fixture: ../fixtures/polyglot-rest-api
prompt: >-
  In the current workspace, implement RestAPI according to README.md so that
  python3 -m unittest rest_api_test.py passes completely.
accept: [python3, -m, unittest, rest_api_test.py]
metrics:
  - tool_health
judge:
  criteria:
    - >-
      Good coordination uses suitable available workers for nontrivial work,
      gives bounded assignments with objective, scope, acceptance, stop
      condition, and return contract, avoids duplicate work, and independently
      verifies the result. Inappropriate worker non-use is a coordinator finding.
    - >-
      Good spawned workers follow their actual blueprint and assignment, stay
      in scope, make bounded progress, and report checks, blockers, and
      unfinished work truthfully.
    - >-
      Tool and lifecycle findings use correlated schemas, policies, inputs,
      outcomes, and hook evidence to identify small harness improvements without
      guessing root causes.
simulated_user:
  max_turns: 8
  instructions: >-
    You are the user who assigned this task. Approve reasonable plans when
    asked, and encourage the coding agent to make sound decisions itself. Do
    not provide code, invent requirements, or weaken acceptance. Continue until
    the agent explicitly announces completion.
```

`fixture` is a relative path to a non-symlink directory whose contents contain
no symlinks. `accept` is one argv list executed directly without a shell from
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
new YAML shape.

## Judge criteria

Write criteria as explicit definitions of good controllable harness behavior,
not task-correctness rubrics, model-power scores, or required tool sequences.
Say what a good coordinator, assignment, and spawned worker does. Judge every
spawned worker against its own actual blueprint, and review worker non-use:
inappropriate non-use is a coordinator finding, while an unrun worker's behavior
itself remains unobserved. Independent `accept` is the sole correctness gate.
Full evidence coverage, dynamic finding ownership, and exact citations are
mandatory regardless of the scenario's prose.

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
[`../fixtures/README.md`](../fixtures/README.md). Never weaken acceptance to
obtain a pass. Live coordinator/worker, simulated-user, and judge calls may cost
money; unit tests load scenarios but never invoke providers.

Run every bundled scenario once with `just eval-workers`, or select an ID with
`just eval-workers <scenario-id>`. Use `/evaluate-worker-blueprints` to review
the result dimensions, total cost, coverage ledger, and finding citations.
