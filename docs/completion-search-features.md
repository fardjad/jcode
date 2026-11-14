# Completion and search features

This patch customizes post-completion quality gates.

## Completion quality gates

Patch `0007-completion-gates-follow-review-settings.patch`
makes automatic post-completion todo quality checks respect the review
settings. When both automatic review and judging are disabled, the quality
gates do not run automatically.
