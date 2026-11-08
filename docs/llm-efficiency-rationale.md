# Rationale: LLM-efficient jcode customization

The worker setup treats the coordinator's context as a scarce, expensive
resource. The coordinator retains planning and judgment while delegating
execution to workers matched to the difficulty of each task. Routine handles
clear localized work, Advanced handles moderate implementation and blockers,
and Expert handles hard reasoning. Models are configurable rather than fixed to
tiers, so this design does not guarantee savings.

Delegation itself adds prompts, messages, reports, and sometimes follow-up
reads. The aim is to spend strong-model context where its judgment matters,
not to claim every delegated operation costs less. Compact previews and a
guard against oversized tool results help keep bulky evidence out of the
coordinator's context until it is needed, without discarding the underlying
evidence. A guard can fail open, and repeated reads can still consume context.

Optional completion checks respect review preferences rather than silently
spending on disabled workflows. Command-output transformation is another
possible optimization, not proof of a saving by itself.

Evaluate the approach on representative end-to-end tasks: consider task
success, elapsed time, retries, total usage, provider-reported cost when
available, and any quality regression. A smaller coordinator transcript is not
automatically an improvement if total cost or failure rates increase.

For the mechanics see
[worker delegation features](swarm-delegation-features.md),
[the delegation guard](../customizations/plugins/delegation-guard/README.md),
and [completion quality gates](completion-search-features.md). For evaluation
usage see the [evaluator guide](../tools/evaluator/README.md).
