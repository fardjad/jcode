# Completion and search features

These patches customize post-completion quality gates and web search engine
selection.

## Completion quality gates

Patch `1009-personal-feature-completion-quality-gates-respect-review.patch`
makes automatic post-completion todo quality checks respect the review
settings. When review is disabled, the quality gates do not run automatically.

## Ordered web search engine policy

Patch `1010-personal-feature-enforce-ordered-web-search-engine-policy.patch`
makes web search engine selection an ordered policy. Configure the search
engine order in `~/.jcode/config.toml`:

```toml
[websearch]
engine = "google"
```

The policy enforces that only configured engines are used, in the specified
order, preventing fallback to unconfigured or default engines.
