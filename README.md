# jcode personalizations

This repository is a soft fork of [jcode](https://github.com/1jehuang/jcode).
It keeps personal changes as patches on top of upstream instead of maintaining a
separate copy of jcode's source.

The goal is to make upstream updates routine: sync to the latest jcode release
or a chosen version, then use AI assistance to carry personal patches forward
when upstream changes affect them.

## Get started

Follow the [get started guide](docs/get-started.md) to clone this catalog into
`~/.jcode/source`, install a release, set up plugins and worker blueprints, and
adapt an example configuration. The guide includes Windows commands, copy
fallback when symlinks are unavailable, and steps to upgrade the binary and
assets together.

## Install a patched release

**macOS or Linux**

```bash
curl -fsSL https://raw.githubusercontent.com/fardjad/jcode/personalized/tools/scripts/install.sh | bash
```

**Windows PowerShell**

```powershell
irm https://raw.githubusercontent.com/fardjad/jcode/personalized/tools/scripts/install.ps1 | iex
```

<details>
<summary>Advanced installation options</summary>

Download the installer to choose a specific release version or pass installer
flags such as a custom installation directory.

```bash
curl -fsSLO https://raw.githubusercontent.com/fardjad/jcode/personalized/tools/scripts/install.sh
bash install.sh vX.Y.Z
# JCODE_INSTALL_DIR="$HOME/bin" bash install.sh
```

```powershell
irm https://raw.githubusercontent.com/fardjad/jcode/personalized/tools/scripts/install.ps1 -OutFile install.ps1
.\install.ps1 vX.Y.Z
.\install.ps1 -InstallDir "$HOME\bin"
```

</details>

## Repository structure

- [`patches/`](patches/) contains the ordered source changes applied to jcode.
- [`customizations/plugins/`](customizations/plugins/) contains personal plugins kept separately from source
  patches.
- [`docs/`](docs/) documents features added by patches.

The generated `.patched-jcode/` directory is your local, fully patched jcode
checkout. It is not committed.

## Patch conventions

Personal changes use `personal-` in their patch filename. They are for behavior
that you want to keep in this soft fork.

Potential upstream contributions use `candidate-` in their patch filename and
have the `upstream-candidate` kind. Keep them focused and suitable for
submitting to jcode independently of personal changes.

## Developer tools

Install uv, then run `uv sync --project tools/catalog` from this checkout.
Developer workflows use Python 3.14 through
`uv run --project tools/catalog jcode-catalog`; `just` provides thin shortcuts.
User asset setup and plugins use plain `python3` 3.14 or newer, without uv.

```bash
uv run --project tools/catalog jcode-catalog --help
uv run --project tools/catalog jcode-catalog test
uv run --project tools/catalog jcode-catalog patches check
uv run --project tools/catalog jcode-catalog patches test patches/<name>.patch
uv run --project tools/catalog jcode-catalog patches test --all
```

Developer commands isolate user configuration before checks and builds. Paid
worker evaluation, installation, publishing, and version changes run only when
explicitly invoked. Tests are colocated with their modules using `_test.py`.

When a workflow needs cargo-nextest, it checks for a newer stable release at
most once every 24 hours. The notice is informational: workflows keep using
the pinned version, and network failures never prevent testing.

## Workflows

Run `just help` to see every command.

### Configuration safety

Repository workflows and tests must not access the user's real
`~/.jcode/config.toml` unless that access is expressly requested. Validation
commands, compatibility tests, and the patched-workspace compile check use
disposable `HOME`, `JCODE_HOME`, and XDG directories through
`tools/scripts/isolate_config.py`. Keep new commands behind the same boundary
when they can execute jcode or load configuration. This repository does not
inspect
or modify personal configuration as part of normal workflows.

### 1. Sync jcode and carry patches forward

Upgrades start with a decision-first review from a jcode session in this
repository:

```text
/plan-upstream-sync vX.Y.Z
```

The project-local skill checks that the catalog is clean and current with its
tracking remote, then compares the pinned release with the chosen canonical
stable release. It creates four temporary reports in ignored `sync-scratch/`
covering upstream context, upstream intent, patch overlap, and a decision-ready
plan. Patch metadata remains the source of truth for patch intent. The skill
stops at the plan; executing it needs your separate approval. Reports may be
discarded after a successful validated sync with your permission.

See [the skill](.jcode/skills/plan-upstream-sync/SKILL.md) for the procedure and
[AGENTS.md](AGENTS.md#decision-first-upstream-review) for the governing rules.
These are agent workflow rules, not checks enforced by any recipe.

Executing the plan uses ordinary recipes. Pin the new release, checking the
commit hash that was reviewed:

```bash
just set-version vX.Y.Z <reviewed-hash>
```

`VERSION.txt` records the stable upstream release targeted by the catalog.
`set-version` fetches it from the canonical jcode URL, regardless of configured
remotes, and writes the pin without validating anything. Every recipe then
works on the new release: recreate `.patched-jcode`, carry patches forward with
the commit-first workflow below, snapshot, and finish with:

```bash
just learn-upstream-exclusions
just validate-patched-copy
```

The pin, patches, and docs are committed together. Until then,
`git checkout -- VERSION.txt` restores the previous pin.

When you are happy with the synchronized catalog, you can push the catalog and
any candidate branches. Agents push only when you explicitly ask:

```bash
just push
```

`VERSION.txt` is the only version pin. Catalog workflows do not use a `master`
branch; upgrades go through `/plan-upstream-sync` and `just set-version`.

### 2. Make a change (commit-first workflow)

Never edit `.patch` files directly. Work in `.patched-jcode/` and let the
snapshot tool regenerate patches from commits.

```bash
just create-patched-copy          # ensure .patched-jcode is clean and current
just list-patches                 # see which commit maps to which patch
# edit source files in .patched-jcode/
# commit or amend the relevant commit there
just snapshot-patches             # regenerate and normalize personal patch hashes
just validate-patch-files         # confirm patches apply cleanly
just test-catalog                 # after changing tools/, customizations/plugins/, or the justfile
just test-patch-file patches/<name>.patch   # run the patch's validation
```

`just snapshot-patches` automatically zeroes the `From` hash in every personal
patch so recreated backing commits do not create irrelevant diffs.
Upstream-candidate patch hashes are preserved as provenance.

Each commit above the release pinned by `VERSION.txt` in `.patched-jcode/` is
one patch. The commit message must include the `X-Jcode-Patch-*` headers,
including `X-Jcode-Patch-File` naming its patch file, and the
body sections (`Patch intent:`,
`Why it exists:`, `Upstream integration points:`, `Update guidance:`,
`Validation:`). Snapshot maps commits to files by that header, checks every
commit before writing, and deletes patches no commit claims only with
`just snapshot-patches --prune`. See `AGENTS.md` for the full format and the
remove, reorder and split steps.

#### Patch whitespace

The catalog validator checks whitespace only in added source lines inside
unified-diff hunks. Snapshots automatically suppress the space prefix on empty
context lines; the validator distinguishes those formatting lines from added
or removed empty source lines, and still reports genuine whitespace defects at
their source line. Running `git diff --check` directly on a `.patch` file can
report false positives because diff context prefixes and mail-patch formatting
are not source whitespace. Fix a reported source line in the owning commit in
`.patched-jcode`, then regenerate the patch with `just snapshot-patches`.
Never strip whitespace from generated `.patch` artifacts directly.

### 3. Install the patched version

Build and install the current patched jcode:

```bash
just install-patched-version
```

This refreshes `.patched-jcode/`, builds it, and installs the resulting jcode
version locally.

### 4. Validate and test patches

Validate the patch catalog and ensure the patches apply:

```bash
just validate-patch-files
```

Test one patch and its declared validation command:

```bash
just test-patch-file patches/0001-candidate-isolate-release-installer-path-opt-in.patch
```

Use this after snapshotting a change, and before relying on it after an
upstream sync.

### 5. Prepare an upstream contribution

Create a branch from a candidate patch:

```bash
just create-upstream-candidate-branch-from patches/0002-candidate-add-hook-context-and-post-tool-transform.patch
```

Review the created branch, then use it to open an upstream contribution. Keep
candidate patches independent from personal-only patches whenever possible.
