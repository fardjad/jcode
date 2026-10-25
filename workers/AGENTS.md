# Worker evaluation rules

These rules apply to worker blueprints, real-code scenarios, fixtures, and
worker-specific evaluation configuration. Generic runner code belongs in
`evaluator/`; worker-specific assets stay here. Follow the root catalog rules
for patch ownership, isolation, and upstream learning. Use the project-local
`/evaluate-worker-blueprints` skill to run and review live evaluations.

## Outcomes before orchestration

- Write scenario prompts as user-visible outcomes and acceptance checks, not
  scripts for tools, workers, delegation verdicts, or an implementation chain.
  If discovery is part of the goal, do not reveal implementation file lists or
  call sites. Scenarios do not contain `expect_delegation`, worker names, or a
  version field. Select built-in metrics explicitly with `metrics`; unknown IDs
  must fail preflight.
- Write judge criteria as explicit definitions of good controllable harness
  behavior: coordinator planning, bounded assignments, spawned-worker discipline,
  and tool/plugin/runtime behavior. Do not score task correctness or model
  intelligence; independent `accept` is the sole correctness gate. Review
  available-worker non-use: inappropriate non-use is a coordinator finding, but
  an unrun worker's own behavior remains unobserved. Criteria cannot weaken
  evidence or citations.
- Configure simulated-user behavior and `max_turns` in each scenario; configure
  only its model in private settings. The Pydantic AI actor must return strict
  structured replies and stop only when the agent announces completion. Do not
  replace it with fixed responses. Preserve its sanitized wire bodies and run
  acceptance after completion, error, timeout, or turn-cap exhaustion.
- Keep correctness (`acceptance`), delegation/harness findings (`review`), tool
  health (`metrics`), and `cost_usd` separate. A correct edit made entirely by
  the coordinator can pass acceptance while still revealing a coordination
  problem. Exact tool counts are native regression assertions, not a general
  measure of good coordination.
- The coordinator owns design, scope, and final acceptance. Workers receive
  bounded objectives. Multi-file search, mechanical investigation, multi-file
  edits, and fix-until-green loops are delegation candidates; genuinely trivial
  single-step work can stay inline. The judge decides from actual blueprints and
  turns rather than a fixed worker registry or forced-delegation rule.
- Do not pre-read several source files merely to summarize them for a worker.
  Name inputs and let the worker inspect them. Review the smallest sufficient
  evidence after the worker returns.

## Fixtures and controls

- Keep fixtures small, pinned, licensed, and documented with provenance. Prefer
  local unit or AST verifiers over container-only acceptance for routine runs.
- Before trusting a scenario, validate both controls: the untouched baseline must
  fail the intended verifier and a known-good solution must pass it. Run the
  verifier from the materialized workspace root so relative paths match actual
  evaluation execution.
- Never weaken a verifier or fixture test to obtain a pass. A no-op solution
  must not become a false positive because the acceptance command is wrong.
  Preserve fixture provenance and licenses when scenarios move.

## Configuration, caching, and current binaries

- Evaluate actual blueprints and production RTK/delegation-guard hooks. Snapshot
  and record binary, full patch-stack source, blueprint, plugin, scenario, and
  redacted effective-config identities with every run.
- `just eval-workers [scenario]` makes paid live coordinator/worker,
  simulated-user, and judge calls. Its build gate fingerprints resolved
  `VERSION.txt`, every ordered catalog patch, targets, profile, and toolchain.
  It reuses binaries only with a valid success stamp and matching hashes. Never
  substitute an unproven existing debug binary.
- Simulator and judge calls use Pydantic AI with `Caching()`, stable
  scenario/role/config cache identities, and disabled retries. Do not claim
  prompt caching works or saves money without inspecting measured
  `cache_read_tokens` and `cost_usd` on a live probe first.
- Source-only edits in `.patched-jcode` are not implicit evaluation inputs.
  Include intended native fixes in the owning source commit and regenerated
  catalog patch first. Never reset `.patched-jcode` or another developer working
  copy to prepare an evaluation.
- Do not rebuild, reset, or rebase a source tree while an evaluation uses it.
  Give each output run a fresh directory and each result ledger/shared mutable
  build cache one active runner. Build logs and stamps are under
  `.tools/worker-eval/`; run evidence is under ignored `workers/results/`.
- Make required toolchain executables available to isolated subprocesses without
  user-specific PATH shims or configuration. Do not inherit personal
  jcode/XDG configuration or provider routing. Preserve only explicitly allowed
  toolchain locations and credentials.

## Complete evidence, projection, and independent dimensions

- Retain raw parent and worker snapshots plus journals, request prompt/tool
  tables, policies, native logs, durable state, hook payloads/traces, and
  judge/simulated-user wire bodies. The judge receives a deterministic
  behavior-focused projection by default and can retrieve excluded raw records by
  exact citation within `judge_max_calls`; retain normalized and readable
  complete transcripts separately.
- Native worker identity comes from `worker_blueprint_policy.name` and parent
  metadata, not animal labels, Python enums, or a fixed worker list. Workers not
  selected in a run are `not exercised`; their own behavior is unobserved, but
  the judge must still identify inappropriate coordinator non-use.
- Never issue a clean review after truncated evidence, an unresolved evidence
  gap, or missing worker output. Read the coverage ledger and verify citations.
  Judge-call budget exhaustion, invalid citations, and incomplete pages mean
  `incomplete`, not a clean review.
- Keep `execution`, `acceptance`, `evidence`, named `metrics`, `review`, and
  `cost_usd` independent. A passing acceptance command never erases runtime,
  delegation, or tool-health findings.
- `tool_start` and `tool_done` are not proof of worker completion or success.
  Verify native terminal state and final report, then independently run the
  acceptance command when the workspace is usable, including after a coordinator
  timeout or abnormal stop.
- A timeout is not proof of task failure or process termination. Inspect the
  original process, logs, current transcript, and task state before retrying. A
  slow final coordinator response can time out after the worker already passed.

## Tool health and runtime contracts

- Inventory tool input, the actual request-specific schema, effective session
  policy, native result/error, retries, worker identity, and hook traces before
  proposing a cause. Point-in-time `ListTools` is only a labeled fallback.
  Interleaved hook calls without native call IDs remain ambiguous; do not guess.
- Treat judge findings as cited hypotheses. Reproduce suspected runtime,
  metadata, or plugin defects with focused tests before patching. Uncertain
  diagnoses remain `unknown`; improvement suggestions do not edit inputs
  automatically.
- A newly spawned worker with a startup message must be `running` before the
  background turn is launched. Initial `ready` means registered, not completed;
  waits and context reads must observe the actual terminal result.
- Strict-schema payloads routinely include empty arrays and null fields. Treat
  unused empty mode fields as absent, and ensure every advertised `update` or
  `add` item property is accepted by the runtime contract. Test schema and
  runtime behavior together.
- Do not respond to a repeated deterministic tool error by changing incidental
  payload fields and retrying. Inspect the actual request, schema, and error
  first. Planning or bookkeeping errors must not become an endless objective.
- Regression tests for lifecycle races must assert intermediate `running`
  state, not only the final successful result.

## Patch and learning discipline

- Amend the source commit that owns behavior, then snapshot and validate the
  regenerated patch at its current hash and pinned base. Do not park a fix in a
  later unrelated patch or leave a source-only fix unsnapshotted.
- Reproduce a suspected upstream failure on clean pinned upstream before using
  the official learning workflow. Never learn or hide a patched-only failure.
  Review learned exclusion changes instead of treating them as proof that the
  patch passed.
- Read full error tails and actual tool inputs before changing code. A passing
  model report is not acceptance evidence; an unrelated failed compatibility
  test is not automatically a regression from the current patch. Do not launch
  repeated paid runs merely to seek a clean stochastic result.
