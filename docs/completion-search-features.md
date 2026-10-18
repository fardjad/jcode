# Completion and search features

These patches customize post-completion quality gates and web search engine
selection.

## Completion quality gates

Patch `1004-personal-feature-completion-quality-gates-respect-review.patch`
makes automatic post-completion todo quality checks respect the review
settings. When review is disabled, the quality gates do not run automatically.

## Configuration-owned web search engines

Patch `1005-personal-feature-keep-web-search-engine-selection-in-configuration.patch`
removes the per-call `engine` parameter from the local `websearch` tool, so the
model cannot choose an engine. The tool runs only configured engines, beginning
with upstream's `[websearch].engine`, then trying `fallback_engines` in order.
Upstream's custom engine is available only when chosen through configuration.
Upstream config semantics are otherwise unchanged, including provider-native
search (`prefer_native`, default on). To use only SearXNG:

```toml
[websearch]
engine = "searxng"
searxng_url = "https://searx.example.org"
prefer_native = false   # never use the provider's hosted search
fallback_engines = []   # upstream defaults to ["bing"]
```
