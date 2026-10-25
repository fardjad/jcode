# Completion and search features

These patches customize post-completion quality gates and web search engine
selection.

## Completion quality gates

Patch `1004-personal-feature-completion-quality-gates-respect-review.patch`
makes automatic post-completion todo quality checks respect the review
settings. When review is disabled, the quality gates do not run automatically.

## Upstream web search configuration

Upstream v0.99.1 owns web search engine and locale selection through
configuration. Request-level engine selection and Bing market overrides are
unavailable. Configure the engine and locale using upstream settings. The
former personal search patch is no longer carried.
