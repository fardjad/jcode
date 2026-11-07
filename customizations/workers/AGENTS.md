# Worker rules

This file is the single authoritative rule source for worker coordination,
worker blueprint and scenario authoring, and worker evaluations. It is the
authoring and evaluation source of truth; it is not the source of truth for
patch intent, which the owning source commit message and catalog patch hold,
and not the native prompt source, which the shipped blueprints, scenarios,
and system instructions hold. These rules apply to worker blueprints,
real-code scenarios, fixtures, and worker-specific evaluation configuration.
Generic runner code belongs in `tools/evaluator/`; worker-specific assets
stay in this directory. Follow the root catalog rules for patch ownership,
isolation, and upstream learning. Use the project-local
`/evaluate-worker-blueprints` skill to run and review live evaluations.

## Central goal and routing

- The goal of the swarm is correct code and correct findings with minimized
  total cost, latency, context, and coordination, not the cheapest model
  label: judge a routing choice by the total expected cost of a correct
  result, including retries, context growth, and coordination overhead.
- Route by capability need with no forced pipeline, worker count, discovery
  stage, or numeric tier quotas. Routine is the primary owner of suitable
  bounded low-ambiguity work. Advanced handles scoped blockers: diagnosis,
  narrowly scoped assistance, a minimal verified repair, and handback.
  Expert handles hard blockers. Resume a worker in the same session for
  handback when continuation beats a fresh spawn, and delegate a bounded
  read-only Routine extraction when that reduces total cost.
- Tiers are capability roles, not a discovery chain: a worker may inspect,
  research, edit, and run checks within a bounded assignment, so do not
  require a separate discovery worker before an implementation worker.
- The coordinator is planning-only. It owns design, scope, resource
  ownership, and acceptance judgment; it reads and authors plan documents,
  reviews returned reports, manages todos, and delegates. All execution is
  delegated to workers with bounded objectives: multi-file search, mechanical
  investigation, multi-file edits, fix-until-green loops, and even trivial
  single-step edits. Planning discovery may use several bounded workers in
  parallel and several sequential rounds with evidence-based follow-ups, with
  no fixed pipeline or count. Final verification is delegated to a separate
  verification worker independent of the implementation when acceptance
  evidence matters. The judge decides from actual blueprints and turns rather
  than a fixed worker registry or forced-delegation rule.
- Do not read source files to summarize them for a worker; the coordinator
  does not explore code directly. Name inputs in the assignment and let the
  worker inspect them. Review the smallest sufficient evidence in returned
  reports.
- The coordinator may read skill instructions through `skill_manage` (`list`,
  `read`, `load`, `reload`, `reload_all`) while remaining planning-only. That
  skill access is planning support, not execution, and skill content grants
  no execution permission: never treat a skill as authorizing coordinator
  execution, broader scope, or approval bypass.
- Enforce the coordinator's planning-only boundary with purpose-based tool
  access: an explicit allowlist naming only what reading/authoring plan
  documents, delegating bounded work, monitoring and resuming delegated
  tasks, reviewing returned reports and skill instructions, and tracking
  shared-resource ownership require, rather than a blanket deny-by-name list
  of execution and research tools. A newly added tool must stay unavailable
  to the coordinator until explicitly added to that allowlist. The generic
  `swarm` tool stays explicitly excluded regardless of allowlist wording,
  and this does not change role-based tier delegation, which remains
  governed by the existing blueprint and child-allowlist rules.
- Do not impose arbitrary word, character, or token caps on final reports
  or require lossy compaction or summarization solely to reduce context,
  whether in worker blueprints or coordinator assignments. Preserve all
  material findings, evidence, changes, checks and exit statuses, failures,
  and unresolved issues. Native output guards, full retained reports, and
  deterministic paging handle inline boundaries. An optional overview
  supplements, never replaces, the complete report.

## Worker-originated delegation

- Judge worker-originated delegation against the scoped spawn graph.
  Advanced and Expert may delegate only a bounded read-only Routine
  extraction or summarization inside their own scope, with named inputs,
  requested facts, a bounded extraction scope, complete evidence for that
  scope, and time and attempt caps. Bound the input scope and requested
  facts, not an arbitrary return length; a scoped summary need not
  reproduce the full source but must preserve all material facts requested.
  The child reports to its immediate parent and gains nothing for it.
  Nested implementation, spawning by Routine or by nested children, or any
  scope, approval, resource, or security gain is a boundary violation.
  Unknown or newly registered spawn tools are unauthorized for workers.
- Shipped blueprints declare child eligibility with
  `allowed-child-blueprints`: Advanced and Expert allow only Routine, and
  Routine allows none. The native effective persisted TOML
  `allowed_child_blueprints` value overrides front matter, and an omitted
  list defaults to no children. This allowlist does not override explicit
  tool denials; native depth, child model resolution, and parent-work
  access remain unchanged.

## Change discipline and consistency review

- When a user goal or preference changes, update this central file first,
  then the affected authored outputs, tests, and evaluations. When a change
  is uncertain, ask the user; never infer approval for a semantic change.
- When worker roles or permissions change, check all authoritative
  instructions for obsolete mandatory role/tool splits. Explicitly note
  material role or permission changes; do not change approval, cost, or
  security rules without specific approval.
- Keep written worker prompts concise and effective: state the active goal,
  do not duplicate native jcode system instructions, and avoid
  contradictions and bloat, while retaining the essential role, security,
  and complete-report material facts. No arbitrary caps or lossy
  compaction.
- Run a consistency review across the central goal, native jcode system
  instructions, blueprints, and evaluations. Identify concrete conflicts,
  then notify the user with the behavior, evidence, and options for a
  native-instruction decision. Never silently override native instructions
  or change approval, cost, or security semantics without specific
  approval.
- Static contract tests check known regressions in approved wording, not
  whether a model will follow instructions in practice; runtime enforcement
  is owned by native tests and live evaluations.

## Authored outputs and portability

- The blueprints under `customizations/workers/blueprints/` are reusable
  harness roles, used in other projects as well as this catalog. Keep
  catalog paths, patch ownership, release pins, sync procedures, isolation
  commands, and installer recipes out of those blueprints. Supply this
  repository's context through project instructions and scoped assignments.
  Runtime tool guidance and transferable execution discipline belong in
  blueprints, not catalog-specific procedures.
- Treat blueprint Markdown and scenario or agent-facing prompts as authored
  outputs governed by the central goal, authoring, and evaluation rules,
  not as documents that define those rules. Keep authored outputs
  self-contained and portable: never reference or inject catalog
  policy-file paths or authoring and evaluation procedures into runtime
  agents. Author-facing repository instructions, docs, and evaluation
  skills may reference this central rule file instead of duplicating it.
  Tests should check authored outputs against these rules. When a goal or
  rule changes, update the central rules first, then the affected
  blueprints and evaluations.

## Scenario and judge authoring

- Write scenario prompts as user-visible outcomes and acceptance checks,
  not scripts for tools, workers, delegation verdicts, or an implementation
  chain. If discovery is part of the goal, do not reveal implementation file
  lists or call sites. Scenarios do not contain `expect_delegation`, worker
  names, or a version field. Select built-in metrics explicitly with
  `metrics`; unknown IDs must fail preflight.
- Write judge criteria as explicit definitions of good controllable harness
  behavior: coordinator planning, bounded assignments, spawned-worker
  discipline, and tool/plugin/runtime behavior. Do not score task
  correctness or model intelligence; independent `accept` is the sole
  correctness gate. Review available-worker non-use: inappropriate
  non-use is a coordinator finding, but an unrun worker's own behavior
  remains unobserved. Criteria cannot weaken evidence or citations. The
  coordinator is planning-only: treat direct coordinator execution
  (exploration, research, shell/build/test commands, or implementation and
  mechanical edits) as a boundary violation regardless of task size, treat
  bounded parallel or sequential worker discovery as legitimate, expect a
  separate verification worker to execute final checks when acceptance
  evidence matters, and require no fixed pipeline, worker count, discovery
  stage, or escalation. Do not name fixed worker IDs in criteria.
- Configure simulated-user behavior and `max_turns` in each scenario;
  configure only its model in private settings. The Pydantic AI actor must
  return strict structured replies and stop only when the agent announces
  completion. Do not replace it with fixed responses. Retain its sanitized wire
  bodies when they are necessary to interpret the simulation or substantiate a
  finding, mapping them to an evaluation purpose; run acceptance after
  completion, error, timeout, or turn-cap exhaustion.
- Keep correctness (`acceptance`), delegation/harness findings (`review`),
  tool health (`metrics`), and `cost_usd` separate. A correct edit made
  entirely by the coordinator can pass acceptance while still revealing a
  coordination problem. Exact tool counts are native regression assertions,
  not a general measure of good coordination.

## Evaluation methodology

- Evaluate the agent blind and uncoached: route an unmodified agent against
  the frozen scenario inputs. Label any deterministic script-integration
  comparison separately; never present it as an agent-route result.
- The final `accept` command is the sole correctness gate. The independent
  judge reports coordination and harness findings from native evidence:
  native traces, journals, hook payloads, and wire bodies, never
  fabricated or inferred behavior.
- Use the actual configured models for every role and record model routing.
  Report cost only as provider-reported `cost_usd`; mark unknown subtotals
  as unknown instead of estimating. Report efficiency with measured
  overhead and cached tokens, and never report a claim that was not
  measured.
- Workers not selected in a run are `not exercised`; their own behavior is
  unobserved, and non-selection is not a failure unless the observed need
  makes the coordinator's routing a finding.
- Run one paid live review of authorized changes; do not launch repeated
  paid runs merely to seek a clean stochastic result. Security and approval
  rules are unchanged by evaluation needs.

## Fixtures and controls

- Keep fixtures small, pinned, licensed, and documented with provenance.
  Prefer local unit or AST verifiers over container-only acceptance for
  routine runs.
- Before trusting a scenario, validate both controls: the untouched baseline
  must fail the intended verifier and a known-good solution must pass it.
  Run the verifier from the materialized workspace root so relative paths
  match actual evaluation execution.
- Never weaken a verifier or fixture test to obtain a pass. A no-op solution
  must not become a false positive because the acceptance command is wrong.
  Preserve fixture provenance and licenses when scenarios move.

## Configuration, caching, and current binaries

- Evaluate actual blueprints and production RTK/delegation-guard hooks.
  Snapshot and record identities for the binary, full patch stack, blueprint,
  plugin, scenario, and redacted effective configuration with every run. Use
  content hashes and source references where sufficient instead of
  duplicating large source artifacts.
- `just eval-workers [scenario]` makes paid live coordinator/worker,
  simulated-user, and judge calls. Its build gate fingerprints resolved
  `VERSION.txt`, every ordered catalog patch, targets, profile, and
  toolchain. It reuses binaries only with a valid success stamp and matching
  hashes. Never substitute an unproven existing debug binary.
- Simulator and judge calls use Pydantic AI with `Caching()`, stable
  scenario/role/config cache identities, and disabled retries. Do not claim
  prompt caching works or saves money without inspecting measured
  `cache_read_tokens` and `cost_usd` on a live probe first.
- Source-only edits in `.patched-jcode` are not implicit evaluation inputs.
  Include intended native fixes in the owning source commit and regenerated
  catalog patch first. Never reset `.patched-jcode` or another developer
  working copy to prepare an evaluation.
- Do not rebuild, reset, or rebase a source tree while an evaluation uses it.
  Give each output run a fresh directory and each result ledger/shared
  mutable build cache one active runner. Build logs and stamps are under
  `.tools/worker-eval/`; run evidence is under ignored
  `customizations/workers/evals/results/`.
- Make required toolchain executables available to isolated subprocesses
  without user-specific PATH shims or configuration. Do not inherit personal
  jcode/XDG configuration or provider routing. Preserve only explicitly
  allowed toolchain locations and credentials.

## Purpose-backed evidence, projection, and independent dimensions

- Worker evaluations serve the explicit workflow goal: devise a worker setup
  that makes jcode efficient, build patches/plugins that support that setup
  or fix upstream bugs, and improve workers while verifying setup
  effectiveness. Use evaluations for actionable blueprint/worker-instruction
  improvements, detecting jcode/native-patch/plugin/evaluator defects with
  reproducible evidence, and assessing configured-setup effectiveness against
  the user's explicit workflow. The last purpose assesses the setup, not raw
  model intelligence. Do not rank or benchmark raw model task performance or
  collect data unrelated to these purposes. A task may exercise the configured
  workflow using its acceptance checks; correctness remains the existing
  `accept` safeguard, not a model-performance score.
- Map every collected artifact and field to an actionable worker-instruction
  issue, a reproducible runtime defect, or a configured-setup effectiveness
  observable. Collect the minimum sufficient diagnostic evidence, including
  native identities and precise citations, exact commands and exit statuses,
  relevant configuration identities and source hashes, and timing, usage, or
  provider-reported cost when relevant. Record unknowns as unknown, without
  inference or estimates.
- Make a concise, human-reviewable findings report the primary surface, with
  necessary supporting evidence retained and retrievable for its purpose.
  Preserve all material requested facts and evidence needed to validate or
  reproduce findings; a report or LLM summary must not be a lossy substitute.
  Do not collect every transcript, raw body, or large artifact by default or
  require indefinite retention. This policy does not authorize blanket
  deletion, weaker acceptance, or silently dropping evidence needed to verify
  or reproduce a finding.
- Native worker identity comes from `worker_blueprint_policy.name` and parent
  metadata, not animal labels, Python enums, or a fixed worker list. Workers
  not selected in a run are `not exercised`; their own behavior is
  unobserved, but the judge must still identify inappropriate coordinator
  non-use.
- Never issue a clean review after truncated evidence, an unresolved evidence
  gap, or missing worker output. Read the coverage ledger and verify
  citations. Judge-call budget exhaustion, invalid citations, and incomplete
  pages mean `incomplete`, not a clean review.
- Keep `execution`, `acceptance`, `evidence`, named `metrics`, `review`, and
  `cost_usd` independent. A passing acceptance command never erases runtime,
  delegation, or tool-health findings.
- `tool_start` and `tool_done` are not proof of worker completion or success.
  Verify native terminal state and final report, then independently run the
  acceptance command when the workspace is usable, including after a
  coordinator timeout or abnormal stop.
- A timeout is not proof of task failure or process termination. Inspect the
  original process, logs, current transcript, and task state before
  retrying. A slow final coordinator response can time out after the worker
  already passed.

## Tool health and runtime contracts

- Inventory tool input, the actual request-specific schema, effective session
  policy, native result/error, retries, worker identity, and hook traces
  before proposing a cause. Point-in-time `ListTools` is only a labeled
  fallback. Interleaved hook calls without native call IDs remain ambiguous;
  do not guess.
- Treat judge findings as cited hypotheses. Reproduce suspected runtime,
  metadata, or plugin defects with focused tests before patching. Uncertain
  diagnoses remain `unknown`; improvement suggestions do not edit inputs
  automatically.
- A newly spawned worker with a startup message must be `running` before the
  background turn is launched. Initial `ready` means registered, not
  completed; waits and context reads must observe the actual terminal
  result.
- Strict-schema payloads routinely include empty arrays and null fields.
  Treat unused empty mode fields as absent, and ensure every advertised
  `update` or `add` item property is accepted by the runtime contract. Test
  schema and runtime behavior together.
- Do not respond to a repeated deterministic tool error by changing
  incidental payload fields and retrying. Inspect the actual request, schema,
  and error first. Planning or bookkeeping errors must not become an endless
  objective.
- Regression tests for lifecycle races must assert intermediate `running`
  state, not only the final successful result.

## Patch and learning discipline

- Amend the source commit that owns behavior, then snapshot and validate the
  regenerated patch at its current hash and pinned base. Do not park a fix
  in a later unrelated patch or leave a source-only fix unsnapshotted.
- Reproduce a suspected upstream failure on clean pinned upstream before
  using the official learning workflow. Never learn or hide a patched-only
  failure. Review learned exclusion changes instead of treating them as
  proof that the patch passed.
- Read full error tails and actual tool inputs before changing code. A
  passing model report is not acceptance evidence; an unrelated failed
  compatibility test is not automatically a regression from the current
  patch. Do not launch repeated paid runs merely to seek a clean stochastic
  result.
