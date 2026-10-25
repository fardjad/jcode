---
name: evaluate-worker-blueprints
description: Run and review paid real-code jcode coordinator/worker blueprint scenario evaluations with current patched binaries, Pydantic AI caching/cost reporting, complete evidence, independent acceptance, tool health, and cited diagnostic findings.
---

# Evaluate worker blueprints

Use this procedure from the catalog workspace. Follow root `AGENTS.md`,
`workers/AGENTS.md`, and `evaluator/AGENTS.md`; those hold the rules. The
`evaluator/README.md` documents formats. This skill is operational guidance, not
another source of blueprint or patch intent.

## 1. Confirm scope and paid calls

Confirm the scenario ID (or all scenarios), its named metrics, harness-focused
judge criteria, and simulated-user behavior, and that the user explicitly
approves live coordinator/worker, simulated-user, and judge calls.
Implementation and non-live tests do not imply that approval. When verifying
Pydantic AI caching or custom-route behavior, run one small scenario or probe
first and confirm actual `cache_read_tokens` and `cost_usd` before requesting
approval for all bundled scenarios. Do not launch repeated paid runs merely to
seek a clean stochastic result. Keep any later improvement and rerun as a
separately reviewed scope.

## 2. Establish immutable current inputs

Let the automatic build gate establish the exact resolved `VERSION.txt` base
plus every ordered current catalog patch. Do not substitute `.patched-jcode`, an
existing debug binary, or a worker-only dependency subset. For non-live build
verification, run the isolated helper twice and inspect the persistent log,
status, stamp, and `built`/`reused` provenance:

```sh
python3 scripts/isolate_config.py -- python3 scripts/prepare_worker_eval_build.py
python3 scripts/isolate_config.py -- python3 scripts/prepare_worker_eval_build.py
```

The second result should prove reuse of identical binaries. Inspect reported
`.tools/worker-eval/builds/` artifacts on failure. Verify source, binary,
blueprint, plugin, scenario, and settings identities without printing
credentials. Ensure no build, reset, rebase, or concurrent writer will change
inputs while a harness is active.

## 3. Run once through the isolated recipe

Run `just eval-workers <scenario-id>` or `just eval-workers` from the catalog
root. The recipe uses the isolated evaluator and actual snapshotted blueprints,
RTK, and delegation guard. Do not read or print private credentials, real
personal jcode configuration, or personal XDG paths. Do not move evaluation into
normal catalog tests.

## 4. Read dimensions, cost, and evidence independently

Locate the fresh ignored `workers/results/<run-id>/` directory. Read
`summary.json`, each `scenario-summary.json`, acceptance logs, complete session
set and journals, normalized transcript and simulated-user conversation, final
diff, selected metric inventories, judge coverage ledger (`excluded_by_default`
and `retrieved_citations`), and review. Keep execution, independent acceptance,
evidence, named metrics, review, and `cost_usd` separate. Independent `accept`
is the sole correctness gate; a passing acceptance command does not erase
delegation, runtime, or tool-health findings. Check `unpriced_calls` whenever
inspecting `cost_usd`, and verify `usage.cache_read_tokens` before claiming
prompt-cache savings.

Treat missing pages, unreadable sessions, ambiguous correlations, budget
exhaustion, and invalid citations as incomplete—not clean. Verify every emitted
finding against its cited native turns before calling the report trustworthy.
Confirm that the judge reviewed the coordinator and every spawned worker against
the actual snapshot and scenario criteria. Workers not selected are
`not exercised`; their own behavior is unobserved, but inappropriate coordinator
non-use must be a finding.

## 5. Classify supported outcomes

Separate confirmed observations, likely diagnoses with confidence, optional
improvement opportunities, and unexercised workers. Tool-health facts are not
proof of root cause. Post-transform hook calls without native call IDs may be
ambiguous. Leave uncertain ownership as `unknown` pending reproduction. Never
assume bundled worker names are exhaustive or permanent.

## 6. Review before any improvement

Do not auto-apply judge suggestions. Before editing, present the supported
finding, citations, and proposed owner to the user. Blueprint changes target the
implicated Markdown only. Tool metadata, plugin, or jcode runtime defects require
focused reproduction and their owning tests/patch workflow. Source fixes belong
in the current catalog stack before another evaluation; never edit mail patches
directly or reset working copies.

## 7. Re-evaluate an approved change narrowly

After an approved improvement, confirm paid scope and rerun the smallest
affected real scenario. Compare input/source/blueprint/plugin hashes,
independent correctness, tool health, coverage, cache reads, total cost, and
citations with the prior run. One clean stochastic run is a smoke signal, not
universal proof.

This skill must not weaken acceptance, modify learned upstream exclusions, or
commit/push. It must not invoke the personal-configuration exception from
`ensure-personal-jcode-setup`.
