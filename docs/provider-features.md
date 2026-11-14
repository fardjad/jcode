# Provider features

These settings require catalog patches `0006-provider-improvements.patch` and
`0009-named-openrouter-reasoning.patch`.

Put provider settings in `~/.jcode/config.private.toml`; they override the
shared `config.toml`.

## OpenRouter endpoint

The `openrouter` provider talks to `https://openrouter.ai/api/v1` by default.
Point it at another OpenRouter deployment, such as the EU endpoint, in config:

```toml
[provider]
default_provider = "openrouter"
default_model = "openrouter:openai/gpt-4.1"
openrouter_api_base = "https://eu.openrouter.ai/api/v1"
```

The endpoint keeps the usual `OPENROUTER_API_KEY` credential, `openrouter:`
model prefix, and `openrouter` cache. jcode uses it for chat requests, the
model catalog refresh, and usage and credit lookups. Cached model lists,
provider endpoints, and prices from another endpoint are ignored until refreshed.

`JCODE_OPENROUTER_API_BASE` overrides the config value for one process. A
named or OpenAI-compatible profile that owns the shared runtime slot sets that
variable for itself, so explicit `openrouter:` model switches keep the
configured OpenRouter endpoint while such a profile is active.

OpenRouter chat streams request SSE usage accounting with `usage.include = true`
on both the built-in `openrouter` provider and named `type = "openrouter"`
profiles. When OpenRouter returns a valid terminal `usage.cost` USD figure,
jcode persists it on the assistant message as `cost_usd`; missing or unreturned
costs stay absent rather than estimated.

Named `type = "openrouter"` profiles accept OpenRouter reasoning efforts across
prefixed and unprefixed routed models, send the unified `reasoning.effort`
request field, and honor `model_catalog = false` when live `/models` probing
should stay off.

## Provider-family allowlist

Set `[provider].allowed_providers` to restrict model selection, model cycling,
fallback, and completion requests to listed families. An absent or empty list
keeps normal unrestricted behavior for other families. The jcode hosted family
is denied unless it is explicitly listed in `allowed_providers`, and it is
never the default or automatically selected (patch `0004-privacy-improvements.patch`).

```toml
[provider]
allowed_providers = ["openrouter"]
default_provider = "openrouter"
default_model = "openrouter:openai/gpt-4.1"
```

Supported families:

```text
claude
openai
copilot
antigravity
gemini
cursor
bedrock
openrouter
openai-compatible
jcode
grok-build
```

Important distinctions:

- `openrouter` permits OpenRouter at its configured endpoint.
- `openai-compatible` permits named OpenAI-compatible profiles.
- `jcode` permits jcode hosted routes only when explicitly listed in
  `allowed_providers`; otherwise the family is denied.
- `grok-build` permits Grok Build routes.

The allowlist does not prevent provider startup, credential discovery, or
background catalog refresh. It prevents disallowed families from appearing in
model-selection outputs or receiving completion/fallback requests.
