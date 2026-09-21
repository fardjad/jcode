# Rationale: LLM-efficient jcode customization

This catalog deliberately treats the coordinator's context as the scarce,
expensive resource. Its efficiency work does not claim that every delegated
operation uses fewer tokens. Delegation adds prompts, messages, summaries, and
possibly follow-up reads. Instead, these patches provide controls that make it
practical to spend strong-model context only where its judgment is useful and
to measure the resulting trade-offs.

## Design principles

1. **Allocate capability to the task.** Routine shell work, repository
   inspection, research, UI automation, and focused implementation can use a
   purpose-built worker rather than the coordinator's model and effort.
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
`1008-personal-feature-add-worker-blueprints-with-runtime-integration.patch`
adds reusable Markdown blueprints and the reserved `coordinator.md` blueprint.
A regular blueprint has durable role instructions and can declare its model,
reasoning effort, enabled and disabled tools, communication policy, and
runtime timeout. This supports intentionally placing narrow work on a model or
effort level appropriate to that work, while keeping complex coordination with
the coordinator.

At runtime, each valid regular blueprint becomes a coordinator-visible
`swarm_<id>` tool. The supplied roles in `workers/blueprints/` demonstrate this
pattern:

- `investigator` runs commands, searches code, and returns compact facts.
- 
- `research` handles web and jcode documentation research.
- `automation` handles browser, desktop, and Gmail workflows.

The coordinator blueprint explicitly guides selection of these workers for
large reads, verbose commands, broad searches, research, and focused edits.
This is the reusable, runtime-supported form of the delegation workflow
illustrated by `workers/`, not merely a collection of prompt files.

A blueprint **can** pin a worker model and effort. It does not promise that a
worker is cheaper, that it is always the right choice, or that delegation
reduces total tokens. The saving comes when a lower-cost specialist avoids
placing its raw investigation and tool output in the coordinator context.

## Bounded worker results and incremental inspection

The same `1008` patch makes the initial swarm-tool response a compact summary,
bounded to 2,000 characters. It also supplies deterministic, non-LLM follow-up
reads, paged in 2,000-character chunks. Consequently, the coordinator can ask
for a small conclusion first and inspect more evidence only for ambiguous or
important cases.

This is a context-shaping mechanism, not information deletion: the worker's
full result remains available for paged retrieval according to the runtime's
follow-up interface. A coordinator can still consume a large amount of text if
it repeatedly reads the whole result. The discipline encoded in the coordinator
prompt is therefore essential: delegate bulky inspection and request only the
specific additional portion required for a decision.

## Hook infrastructure and the delegation guard

Patch `1011-personal-feature-add-hook-context-and-post-tool-transform.patch`
adds context-aware hook integration. Hooks receive whether swarm is enabled and
the process role (`coordinator`, `worker`, or `internal`). It also adds:

- a synchronous `post_tool_transform` hook that can replace the
  model-visible tool result;
- a detached `post_tool_record` hook for observability; and
- temporary-file spilling for large hook payloads, with runtime-managed TTL
  cleanup.

The synchronous transform is deliberately fail-open. A malformed output,
nonzero exit, spawn problem, or timeout leaves the original tool result
unchanged. This protects normal tool semantics but means the guard is an
optional configured optimization, not a guarantee.

`plugins/delegation-guard/delegation-guard-transform` uses that interface. When
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

Patch `1009-personal-feature-completion-quality-gates-respect-review.patch`
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

### Evidence rather than assumptions

Patch `1012-personal-feature-measure-delegation-efficiency.patch` adds an
opt-in, content-free measurement boundary. It records bounded facts about tool
attempts, guard outcomes, original and model-visible result sizes, worker
lineage, summaries, follow-up reads, retries, and observer health. Where
available, it also carries nullable response-derived OpenRouter usage and cost
facts, including served model, route, cost details, discount, and timestamp.
It does not use static catalog pricing.

The patch is not itself a token-saving mechanism. Collection is off by default,
and the runtime does not persist, aggregate, report, or score the facts. Its
value is falsifiability: an external opt-in observer can compare guarded and
unguarded flows, determine whether work was actually delegated, and distinguish
visible-context reduction from added worker cost. The boundary intentionally
excludes prompts, tool-result content, paths, and arbitrary metadata.

### Input normalization before execution

Candidate patch `0001-candidate-pre-tool-input-transformers.patch` adds a
fail-open `pre_tool_transform` hook that rewrites tool JSON before pre-tool
policy evaluation. The catalog's RTK adapter uses this generic hook to rewrite
Bash commands through RTK. This is an enabling mechanism for command-output
reduction or normalization, not proof of a saving by itself: its effect depends
on the configured adapter and command. The patch preserves the policy gate and
uses the original input if transformation fails or times out.

### Avoiding unrequested search fallback

Patch `1010-personal-feature-enforce-ordered-web-search-engine-policy.patch`
limits web search to configured engines in their specified order. This is
primarily a policy and predictability change. It can avoid wasted fallback
attempts and their associated tool context, but it is not a general token
reduction claim and should be evaluated separately from the delegation system.

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

- `1008-personal-feature-add-worker-blueprints-with-runtime-integration.patch`
- `1009-personal-feature-completion-quality-gates-respect-review.patch`
- `1010-personal-feature-enforce-ordered-web-search-engine-policy.patch`
- `1011-personal-feature-add-hook-context-and-post-tool-transform.patch`
- `1012-personal-feature-measure-delegation-efficiency.patch`
- `0001-candidate-pre-tool-input-transformers.patch`
- `workers/README.md` and `workers/blueprints/`
- `plugins/delegation-guard/README.md`
- `plans/delegation-guard-efficiency-measurement.md`
- `docs/swarm-delegation-features.md`
- `docs/completion-search-features.md`
