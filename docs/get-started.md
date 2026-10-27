# Get started

## Install

You need Git and Python 3.14 or newer, available as `python3` on PATH.

**macOS / Linux** (also requires `curl`, `tar`, and `sha256sum` or `shasum`):

```bash
git clone --branch personalized https://github.com/fardjad/jcode.git "$HOME/.jcode/source"
cd "$HOME/.jcode/source"
bash tools/scripts/install.sh
python3 tools/scripts/setup_assets.py
```

**Windows PowerShell:**

```powershell
git clone --branch personalized https://github.com/fardjad/jcode.git "$HOME\.jcode\source"
Set-Location "$HOME\.jcode\source"
& .\tools\scripts\install.ps1
python3 tools/scripts/setup_assets.py
```

Run each command only after the previous one succeeds. Follow the installer's
PATH instructions, then launch `jcode`.

Asset setup links plugins, worker blueprints, and skills into
`~/.jcode/plugins`, `~/.jcode/worker-blueprints`, and `~/.jcode/skills`,
falling back to copies if symlinks are unavailable. Linked directories
reflect the checkout exactly. Copied assets include only allowlisted
content: a shipped skill needs an explicit `!/name/` entry in
`skills/.gitignore`, and unlisted local skill directories stay out of the
copy. Use `--copy` to force copies. Set `JCODE_HOME` to use a different jcode
home. Setup never changes your config or replaces unmanaged directories. If it
reports a conflict, back up and move the conflicting destination yourself.

## Configure

Merge the settings you want into `~/.jcode/config.toml`.

```toml
[display]
pin_images = false
compact_notifications = true
show_agentgrep_output = true
show_bash_output = true
tool_call_details = true
session_picker_default_filter = "current-dir"
session_picker_sort = "created"

[features]
memory = false
swarm = true
auto_poke = false

[tools]
mcp_tools = "auto"
disabled = ["swarm", "maintainer_feedback", "panel", "applet"]

[provider]
default_provider = "openrouter-eu"
default_model = "openai/gpt-6.1-sol"
allowed_providers = ["openrouter-eu"]
same_provider_account_failover = false
retry_backoff_cap_secs = 300

[agents]
swarm_model = "openrouter-eu:z-ai/glm-5.3-flash"
swarm_root_effort = "medium"
memory_model = "openrouter-eu:z-ai/glm-5.3-flash"

[hooks]
pre_tool_transform = ["python3 ~/.jcode/plugins/rtk/rtk-transform"]
post_tool_transform = ["python3 ~/.jcode/plugins/delegation-guard/delegation-guard-transform"]
post_tool_transform_timeout_ms = 500

[ambient]
model = "openrouter-eu:gpt-6.1-sol"

[compaction]
mode = "proactive"
max_context_tokens = 0

[autoreview]
model = "openrouter-eu:gpt-6.1-sol"

[autojudge]
model = "openrouter-eu:z-ai/glm-5.3-flash"

[launch_hotkeys]
enabled = false
```

- Sign in to your provider through jcode. Update `allowed_providers` and model
  overrides together when choosing another provider.
- Disabling `swarm` above leaves blueprint tools such as `swarm_fixer` enabled.
  [Worker blueprints](../workers/README.md) can override model settings and
  make paid calls. Review them before use.
- For hook details, see [plugins](../plugins/README.md). For provider choices,
  see [provider configuration](provider-features.md).

## Upgrade

Update the checkout, binary, and assets together. A binary-only update does not
refresh plugins, blueprints, or skills.

**macOS / Linux:**

```bash
cd "$HOME/.jcode/source"
git pull --ff-only
bash tools/scripts/install.sh
python3 tools/scripts/setup_assets.py
```

**Windows PowerShell:**

```powershell
Set-Location "$HOME\.jcode\source"
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw "Checkout update failed" }
& .\tools\scripts\install.ps1
python3 tools/scripts/setup_assets.py
```

Stop if any step fails. Setup keeps the installation's existing link or copy
mode. Linked assets
update with the checkout. Copied assets refresh when setup runs, but local
changes to managed files stop the update. Keep `.setup-assets-state.json` in
your jcode home so setup can recognize its copies.

Review release notes for new hook settings, then restart jcode. To select a
binary version, use `bash tools/scripts/install.sh vX.Y.Z` or
`& .\tools\scripts\install.ps1 -Version vX.Y.Z`. This does not pin the checkout's assets.
