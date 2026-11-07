# Rationale: LLM-efficient jcode customization

This catalog deliberately treats the coordinator's context as the scarce,
expensive resource. Its efficiency work does not claim that every delegated
operation uses fewer tokens. Delegation adds prompts, messages, summaries, and
possibly follow-up reads. Instead, these patches provide controls that make it
practical to spend strong-model context only where its judgment is useful and
to measure the resulting trade-offs.

## Design principles

1. **Allocate capability to the task.** Clear, localized implementation and
   verification can use `swarm_routine`; moderate implementation/debugging and
   scoped Routine assistance can use `swarm_advanced`; hard reasoning and
   cross-cutting diagnosis can use `swarm_expert`. A blueprint can pin a model
   and effort appropriate to that work, while planning and acceptance
   judgment stay with the coordinator, which is planning-only and delegates
   all execution. Shipped tier models are unspecified, so cheaper-model
   savings are not guaranteed.
2. **Keep bulky evidence out of the coordinator by default.** The coordinator
   receives a bounded report, and can retrieve more only when it is needed.
3. **Apply deterministic guards at the context boundary.** A plugin can replace
   an oversized result before the result is added to coordinator context.
4. **Do not spend on checks the user has disabled.** Optional automatic quality
   workflows must not silently cause extra completion turns.
5. **Measure before declaring a saving.** Cost, token usage, retry, and output
   facts are collected only through an explicit, privacy-bounded opt-in
   boundary.

## Worker blueprints and delegated roles

Patch
`1003-personal-feature-worker-blueprints-and-session-lifecycle-fixes.patch`
adds reusable tiered worker contracts and a reserved `coordinator.md` blueprint.
Routine, Advanced, and Expert omit a worker-level tool allowlist, exposing broad
task-relevant tools subject to global policy and assignment scope. Each tier
keeps recursion bounded: Routine disables the generic `swarm` tool and every
tier spawn name, and Advanced and Expert disable the generic `swarm` tool and
their peer and upward spawn names while exposing only `swarm_routine` for
bounded read-only extraction within their own scope. Unknown or newly
registered spawn tools are not authorized for workers.
Communication remains `report-to-parent`; coordinator and worker IDs are
`coordinator`, `routine`, `advanced`, and `expert`. Old `fixer`, `investigator`,
and `research` IDs are retired. Copy-mode setup preserves unrelated local
assets. Shipped model mappings remain unspecified.

At runtime, each valid regular blueprint becomes a coordinator-visible
`swarm_<id>` tool. Broad capability does not broaden assignment scope or
change approval, cost, or security boundaries. Attempt, escalation, and
handback discipline is owned by the central worker rules; see
`customizations/workers/AGENTS.md` and `customizations/workers/README.md`.

The coordinator is planning-only and delegates all execution; the boundary,
routing, discovery, and acceptance-judgment rules are owned by
`customizations/workers/AGENTS.md`. Its blueprint front matter denies the
generic execution and research tools as a second line of defense, while the
empty enabled list keeps the generated tier spawn tools, `skill_manage`
skill access, read, and write available.

The shipped capability tiers perform bounded tasks. A smaller, cheaper, faster
model with appropriate reasoning effort may be a good fit, but tier models are
not pinned and no saving is promised without measurement. Configure models by
blueprint ID if needed; the coordinator retains planning and acceptance
judgment.

## Bounded spawn summaries and incremental inspection

Patch
`1003-personal-feature-worker-blueprints-and-session-lifecycle-fixes.patch`
makes the initial `swarm_<id>` tool response a compact summary, bounded to
2,000 characters. Worker reports may contain the detail needed for handoff.
Deterministic, non-LLM follow-up reads page the report or transcript in
2,000-character chunks but do not resume execution. Preserve acceptance evidence
and remaining work, and keep bulky diagnostics in artifacts where possible. The
coordinator can retrieve more evidence only when needed.

This is a context-shaping mechanism, not information deletion: the worker's full
result remains available for paged retrieval according to the runtime's
follow-up interface. A coordinator can still consume a large amount of text if
it repeatedly reads the whole result. Final verification is executed by
workers: a separate verification worker independent of the implementation runs
the required checks when acceptance evidence matters, and the coordinator
judges acceptance from the returned evidence.

## Hook infrastructure and the delegation guard

Patch `0002-candidate-add-hook-context-and-post-tool-transform.patch`
adds context-aware hook integration. Hooks receive whether swarm is enabled and
the process role (`coordinator`, `worker`, or `internal`). It also adds:

- a synchronous `post_tool_transform` hook that can replace the
  model-visible tool result; and
- temporary-file spilling for large hook payloads, with runtime-managed TTL
  cleanup.

The synchronous transform is deliberately fail-open. A malformed output,
nonzero exit, spawn problem, or timeout leaves the original tool result
unchanged. This protects normal tool semantics but means the guard is an
optional configured optimization, not a guarantee.

`customizations/plugins/delegation-guard/delegation-guard-transform` uses that interface. When
swarm is enabled, the current process is the coordinator, and the runtime's
precomputed `output_bytes` exceeds its configured threshold, the plugin
replaces the successful result with a small nudge. The default threshold is
8,192 bytes. The nudge states the size and, where the runtime supplied one,
the full-result file path. It never includes the oversized result and it does
not choose a worker. The coordinator retains the judgment of whether and how
to delegate inspection.

This makes the *threshold decision* deterministic and role-scoped. It avoids a
common failure mode where an expensive coordinator receives a huge result
before it can decide to delegate. It does not make all tool output small:
results at or below the threshold, worker and internal results, disabled swarm,
and hook failures retain their normal behavior.

## Review preferences and completion quality gates

Patch `1004-personal-feature-completion-quality-gates-respect-review.patch`
prevents automatic post-completion todo quality continuations when both
automatic review and automatic judging are disabled. The skipped continuations
include ownership, validation, and synthetic final-response checks. Incomplete
work progress reminders remain active, and enabling either review workflow
preserves the existing completion gates.

This is a direct control over discretionary post-completion model turns. It
does not disable quality mechanisms globally, and it should not be described
as lowering the cost of work whose review or judging the user chose to retain.
Its purpose is to honor the user's review preference rather than spending
additional tokens on automatic checks they explicitly turned off.

## Related efficiency-enabling changes

### Input normalization before execution

Upstream jcode (since v0.89, merged from the former candidate patch
`0001-candidate-pre-tool-input-transformers.patch`) provides a
fail-open `pre_tool_transform` hook that rewrites tool JSON before pre-tool
policy evaluation. The catalog's RTK adapter uses this generic hook to rewrite
Bash commands through RTK. This is an enabling mechanism for command-output
reduction or normalization, not proof of a saving by itself: its effect depends
on the configured adapter and command. The hook preserves the policy gate and
uses the original input if transformation fails or times out.

### Upstream web search configuration

Upstream v0.99.1 owns web search engine and locale selection through
configuration. Request-level engine selection and Bing market overrides are
unavailable. Configure the engine and locale using upstream settings. The
former personal search patch is no longer carried.

## How to evaluate the approach

A useful evaluation compares end-to-end tasks, not only the coordinator's
visible result size. For each representative task, record:

1. coordinator input and output usage;
2. worker input and output usage, model, effort, and retries;
3. original versus model-visible tool-result bytes;
4. number and size of summaries and follow-up pages;
5. provider-reported cost where available; and
6. task success, elapsed time, and any quality regression.

The intended outcome is lower expensive-coordinator context consumption without
blindly hiding evidence or degrading task completion. A workflow that produces
a smaller coordinator transcript but increases total provider cost, retries, or
failure rate is not automatically an efficiency improvement.

## Source map

- `1003-personal-feature-worker-blueprints-and-session-lifecycle-fixes.patch`
- `1004-personal-feature-completion-quality-gates-respect-review.patch`
- `1006-personal-feature-configure-session-picker-sort-order.patch`
- `0002-candidate-add-hook-context-and-post-tool-transform.patch`
- upstream `pre_tool_transform` hook (formerly
  `0001-candidate-pre-tool-input-transformers.patch`)
- `customizations/workers/README.md` and `customizations/workers/blueprints/`
- `customizations/plugins/delegation-guard/README.md`
- `docs/swarm-delegation-features.md`
- `docs/completion-search-features.md`
