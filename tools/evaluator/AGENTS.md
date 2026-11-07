# Generic evaluator rules

The package in this directory is reusable infrastructure. Worker-specific
scenarios, fixtures, blueprint Markdown, settings, and results belong in
`customizations/workers/`. Catalog patch materialization and build identity belong in
`tools/catalog/src/jcode_catalog/`
and the `just eval-workers` recipe, not this package.
Worker coordination, authoring, and evaluation methodology is owned by
`customizations/workers/AGENTS.md`; this file keeps only generic runner
mechanics.

## Maintain generic inputs

- Only `coordinator` is reserved. Never register current worker IDs, filenames,
  permissions, or stereotypes in runner code, finding schemas, or tests of
  generic behavior. Discover each run's workers and validate finding owners
  against that run's snapshot.
- Keep fixture-specific logic and catalog-patch logic out of the package.
  Scenarios contain only `id`, `fixture`, `prompt`, `accept`, `metrics`,
  `judge`, `simulated_user`, and an optional credential-free `settings`
  mapping of shared-defaults overrides, with no schema-version field. Do
  not add a scripted provider, delegation flag, or generic assertion DSL. Built-in metric
  IDs are explicit in YAML and resolved through a small registry; unknown IDs
  fail preflight.
- Scenario and judge authoring policy is owned by
  `customizations/workers/AGENTS.md`; keep only mechanics here. The Pydantic
  AI simulated user gets its behavior and turn cap from the scenario, its
  model from private settings, and returns strict structured replies; never
  replace it with fixed responses.
- Use Pydantic AI for simulator and judge calls with `Caching()`, stable
  scenario/role/config cache identities, and disabled SDK/agent retries (`retries=0`
  and `max_retries=0`). Do not assume caching works or saves money without
  measuring actual `cache_read_tokens`.
- Preserve the low-level harness client and its transport/history contracts.
  Native inventory wrappers must tolerate unknown additive fields and preserve
  raw evidence.

## Evidence, projection, and cost before verdicts

- Retain complete coordinator and worker sessions, journals, request prompt/tool
  tables, policies, outer events, native logs, durable state, hook payloads, and
  hook traces. Replay native journal vectors and join identities by session ID;
  keep `parent_id` distinct from `parent_session_id`.
- Never truncate or delete raw evidence. Send the judge a deterministic
  behavior-focused projection by default (messages, delegations, assignments,
  tool inputs/schemas/policies/errors/retries/lifecycle, blueprints, plugins,
  and hook metadata) and allow bounded citation-specific raw retrieval on
  request within `judge_max_calls`. Missing or ambiguous evidence makes coverage
  incomplete and prevents a clean review.
- Stable citations use native IDs, request-snapshot indexes, and journal
  positions (`scenario:<id>:...` for scenario-level artifacts). Do not derive
  primary citations from response-list positions, animal labels, or report prose.
- Keep execution, independent acceptance, evidence, named metrics, diagnostic
  review, and provider-reported-only `cost_usd` separate. Independent `accept` is
  the sole correctness gate; there is no judge `task_compliance`. Run acceptance
  after timeout when the workspace is usable. Preserve original fixtures and
  complete final diffs.
- Report one provider-reported-only `cost_usd` subtotal per scenario and per run
  across native coordinator/worker usage, simulator usage, and judge usage
  without double counting. A valid provider-reported zero is accepted. Missing
  or invalid reported costs remain unpriced: do not estimate or use fallback
  pricing, and identify unpriced calls. When any calls are unpriced, describe
  `cost_usd` as a subtotal, not the full cost.
- Tool-health code reports observed schema/policy/outcome/hook facts and missing
  evidence, not speculative causes. Judge findings are cited hypotheses, not
  authorization to edit blueprints, plugins, jcode, or patches.

## Security and tests

- Replace personal HOME/JCODE_HOME/XDG paths and never read the user's real
  jcode configuration. Do not inherit provider routing. Preserve only explicit
  toolchain locations and credentials needed by a live run.
- Keep private settings mode 600 and redact configured secrets consistently from
  every raw and derived artifact without changing citation IDs.
- Hook instrumentation must preserve original argv/environment/stdin/stdout/
  stderr/exit semantics and add no timeout. Retain referenced native payloads
  before TTL cleanup and record ambiguous call correlation as a gap.
- Add focused fake-server and unit tests for every scenario, Pydantic AI
  backend/caching/cost, projection/retrieval, evidence, pagination, citation,
  ownership, timeout, acceptance, diff, secret, and hook contract. Normal tests
  must not compile jcode or incur provider/judge cost. Run all checks through
  `tools/scripts/isolate_config.py`.
