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

Setup installs the files in [`customizations/`](../customizations/):

| Source | Installed destination |
| --- | --- |
| `customizations/config.toml` | `~/.jcode/config.toml` |
| `customizations/plugins/` | `~/.jcode/plugins/` |
| `customizations/skills/` | `~/.jcode/skills/` |
| `customizations/workers/blueprints/` | `~/.jcode/worker-blueprints/` |

Setup uses symlinks by default and falls back to copies if symlinks are
unavailable. Use `--copy` to force copies. Set `JCODE_HOME` to use a different
jcode home. Linked assets reflect the checkout, including local additions.
Copies include only allowlisted content: shipped plugins and skills need an
explicit `!/name/` entry in their source directory's `.gitignore`. Unlisted
local plugins and skills stay out of copies.

An existing unmanaged `config.toml`, including a symlink to your dotfiles, is
preserved and skipped with a notice. Merge the template settings yourself if
you keep that file. Setup never manages `config.private.toml` and does not
replace unmanaged asset directories. If it reports a conflict, back up and
move the conflicting destination yourself before retrying.

## Configure

The [generic template](../customizations/config.toml) contains display,
feature, tool, hook, and compaction defaults. It uses Python hook commands,
enables blueprint-based delegation, and leaves provider and model choices to
your private configuration. Its context budget is derived from the selected
model rather than fixed to one model's capacity.

Keep personal settings in `~/.jcode/config.private.toml`, beside the logical
`config.toml` path, even when the main file is symlinked elsewhere. Private
settings override the main file, and environment overrides take precedence
over both. Tables merge recursively, while arrays and scalar values replace
the corresponding main-file value. Both files reload live. Setup neither
creates nor updates the private file.

Create the private file locally, outside version control. On macOS or Linux,
restrict its permissions with `chmod 600 ~/.jcode/config.private.toml`; on
Windows, restrict access using the file's security permissions.

The following example reflects this fork's model setup. Model availability,
provider identifiers, prices, and supported reasoning levels can change, so
adapt it to your provider rather than treating these names as requirements.
No credentials or private search endpoint are included.

```toml
[provider]
# The coordinator handles planning, integration, and judgment. Give it a
# larger, more intelligent frontier model rather than optimizing only for cost.
default_provider = "openrouter-eu"
default_model = "openai/gpt-6.1-sol"
allowed_providers = ["openrouter-eu"]
openai_service_tier = "flex"
same_provider_account_failover = false
retry_backoff_cap_secs = 300

[agents]
# Workers handle bounded tasks. A smaller, cheaper, faster model with higher
# reasoning effort can be a good fit. Check that the model supports the effort.
swarm_model = "openrouter-eu:z-ai/glm-5.3-flash"
swarm_root_effort = "medium"
swarm_effort = "xhigh"
memory_model = "openrouter-eu:z-ai/glm-5.3-flash"

[ambient]
model = "openrouter-eu:z-ai/glm-5.3-flash"

[autoreview]
# Review is judgment-heavy, so this setup uses the larger model here too.
model = "openrouter-eu:gpt-6.1-sol"

[autojudge]
model = "openrouter-eu:z-ai/glm-5.3-flash"

[websearch]
engine = "searxng"
prefer_native = false
fallback_engines = []
# Set searxng_url locally, or use JCODE_SEARXNG_URL in your environment.
# This engine needs an endpoint before it can perform searches.
```

- Sign in to your provider through jcode. Update `allowed_providers` and model
  overrides together when choosing another provider.
- Coordinator and worker model choices are a starting point, not a guarantee.
  Evaluate quality, latency, and cost on your own tasks.
- Disabling the generic `swarm` tool in the template leaves blueprint tools
  such as `swarm_fixer` enabled. [Worker blueprints](../customizations/workers/README.md)
  can override model settings and make paid calls. Review them before use.
- For hook details, see [plugins](../customizations/plugins/README.md). For
  provider choices, see [provider configuration](provider-features.md).

## Upgrade

Update the checkout, binary, and customizations together. A binary-only update
will not refresh the config template, plugins, blueprints, or skills.

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

Stop if any step fails. Setup retains its existing copy mode. Linked assets
update with the checkout; copied assets and a managed copied config refresh
when setup runs. Local changes to managed copies stop the update rather than
being overwritten. Existing unmanaged configs continue to be skipped. Keep
`.setup-assets-state.json` in your jcode home so setup can recognize its copies.

When using a linked config, put personal overrides in `config.private.toml`
instead of editing the shared template. Review upstream template changes on
upgrade, since a linked main config follows the checkout immediately.

Review release notes for new hook settings, then restart jcode. To select a
binary version, use `bash tools/scripts/install.sh vX.Y.Z` or
`& .\tools\scripts\install.ps1 -Version vX.Y.Z`. This does not pin the checkout's assets.
