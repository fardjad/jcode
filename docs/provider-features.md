# Provider features

These settings require catalog patches `1002-personal-openrouter-eu.patch` and
`1003-personal-provider-family-allowlist.patch` to be applied.

jcode reads user configuration from `~/.jcode/config.toml`.

## OpenRouter EU

`openrouter-eu` is an OpenRouter provider using:

```text
https://eu.openrouter.ai/api/v1
```

It uses its own credential:

```bash
export OPENROUTER_EU_API_KEY='...'
```

Configure it as default:

```toml
[provider]
default_provider = "openrouter-eu"
default_model = "openrouter-eu:openai/gpt-4.1"
```

Use an EU model explicitly:

```text
openrouter-eu:openai/gpt-4.1
```

Standard OpenRouter remains separate:

```toml
[provider]
default_provider = "openrouter"
default_model = "openrouter:openai/gpt-4.1"
```

EU and standard OpenRouter use separate route identities, credentials, and
endpoint-cache namespaces. Standard OpenRouter uses `OPENROUTER_API_KEY`.

## Provider-family allowlist

Set `[provider].allowed_providers` to restrict model selection, model cycling,
fallback, and completion requests to listed families. An absent or empty list
keeps normal unrestricted behavior.

```toml
[provider]
allowed_providers = ["openrouter-eu"]
default_provider = "openrouter-eu"
default_model = "openrouter-eu:openai/gpt-4.1"
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
openrouter-eu
openai-compatible
jcode
grok-build
```

Important distinctions:

- `openrouter` permits only standard OpenRouter.
- `openrouter-eu` permits only OpenRouter EU.
- `openai-compatible` permits named OpenAI-compatible profiles.
- `jcode` permits jcode subscription routes.
- `grok-build` permits Grok Build routes.

For example, standard OpenRouter without EU:

```toml
[provider]
allowed_providers = ["openrouter"]
default_provider = "openrouter"
```

The allowlist does not prevent provider startup, credential discovery, or
background catalog refresh. It prevents disallowed families from appearing in
model-selection outputs or receiving completion/fallback requests.
