---
name: evaluate-worker-blueprints
description: Run and review paid real-code jcode coordinator and worker evaluations with native evidence, independent acceptance, and cited diagnostic findings.
---

# Evaluate worker blueprints

Use this procedure from the catalog workspace. Follow `AGENTS.md`.
`tools/evaluator/README.md` documents formats.

## Author scenarios and controls

Write judge criteria for controllable harness behavior, including planning,
bounded assignments, worker discipline, and tool or runtime behavior. Review
inappropriate worker non-use without attributing behavior to unrun workers.
Treat direct coordinator execution as a boundary violation; allow bounded
parallel or sequential discovery and expect separate final verification when
acceptance evidence matters. Do not name fixed worker IDs or score model
intelligence in criteria.

Set `simulated_user.max_turns` per scenario. The turn loop in
`tools/evaluator/src/runner.ts` runs independent acceptance after each turn and
sends a fixed continuation after a failure. The scenario's simulated-user
instructions are metadata, not a model prompt. Preserve the acceptance result
after a coordinator error or timeout.

Keep fixtures small, pinned, licensed, and documented with provenance. Before
trusting a scenario, verify from the materialized workspace root that the
untouched baseline fails and a known-good solution passes its verifier.

## 1. Confirm scope and paid calls

Confirm the scenario ID or all scenarios, their judge criteria, and the turn
limit. The user must explicitly approve live coordinator, worker, and judge
calls. Report provider-reported cost; missing cost stays unknown.

## 2. Establish immutable current inputs

Let the automatic build gate establish the exact resolved `VERSION.txt` base
plus every ordered current catalog patch. Do not substitute `.patched-jcode`, an
existing debug binary, or a worker-only dependency subset. For non-live build
verification, run the isolated helper twice and inspect the persistent log,
status, stamp, and `built`/`reused` provenance:

```sh
bun tools/catalog/src/cli.ts workers build
bun tools/catalog/src/cli.ts workers build
```

The second result should prove reuse of identical binaries. Inspect reported
`.tools/worker-eval/builds/` artifacts on failure. Verify source, binary,
blueprint, plugin, scenario, and settings identities without printing credentials.

## 3. Run once through the isolated recipe

Run `just eval-workers <scenario-id>` or `just eval-workers` from the catalog
root. The recipe uses the isolated evaluator, snapshotted blueprints and RTK.
Delegation authority comes from native blueprint frontmatter and config overrides,
not a delegation-guard plugin. Do not read or print private credentials.

## 4. Read dimensions, cost, and evidence independently

Locate the fresh ignored `customizations/workers/evals/results/<run-id>/`
directory. Read the run and per-scenario `summary.json` and `report.md` files.
Inspect execution, acceptance attempts, events, native sessions and journals,
dispatch records, hook traces, diff, review, and `cost_usd` separately.
Missing cost or evidence is unknown. The coordinator sandbox has an isolated
network namespace and a strict configured-provider allowlist; acceptance has no
network allowlist.

Treat missing pages, unreadable sessions, ambiguous correlations, budget
exhaustion, and invalid citations as incomplete—not clean. Verify every emitted
finding against its cited native turns before calling the report trustworthy.
Check cited coordinator and worker evidence against the blueprint snapshots and
scenario criteria. Unselected workers are `not exercised`; their own behavior is
unobserved, but inappropriate coordinator non-use must be a finding.

## 5. Classify supported outcomes

Separate confirmed observations, likely diagnoses with confidence, optional
improvement opportunities, and unexercised workers.

## 6. Review before any improvement

Do not auto-apply judge suggestions. Before editing, reproduce the finding
from its cited evidence (a paid rerun needs approval), then present the
finding, citations, reproduction, and proposed owner to the user.

## 7. Re-evaluate an approved change narrowly

After an approved improvement, confirm paid scope and rerun the smallest
affected real scenario. Compare available input identities, independent
correctness, native evidence, provider-reported cost, and citations with the
prior run. Record missing evidence as unknown.

Do not modify learned upstream exclusions.
