# jcode personalizations

This repository is a soft fork of [jcode](https://github.com/1jehuang/jcode).
It keeps personal changes as patches on top of upstream instead of maintaining a
separate copy of jcode's source.

The goal is to make upstream updates routine: sync to the latest jcode release
or a chosen version, then use AI assistance to carry personal patches forward
when upstream changes affect them.

## Install a patched release

**macOS or Linux**

```bash
curl -fsSL https://raw.githubusercontent.com/fardjad/jcode/personalized/install.sh | bash
```

**Windows PowerShell**

```powershell
irm https://raw.githubusercontent.com/fardjad/jcode/personalized/install.ps1 | iex
```

<details>
<summary>Advanced installation options</summary>

Download the installer to choose a specific release version or pass installer
flags such as a custom installation directory.

```bash
curl -fsSLO https://raw.githubusercontent.com/fardjad/jcode/personalized/install.sh
bash install.sh vX.Y.Z
# JCODE_INSTALL_DIR="$HOME/bin" bash install.sh
```

```powershell
irm https://raw.githubusercontent.com/fardjad/jcode/personalized/install.ps1 -OutFile install.ps1
.\install.ps1 vX.Y.Z
.\install.ps1 -InstallDir "$HOME\bin"
```

</details>

## Repository structure

- [`patches/`](patches/) contains the ordered source changes applied to jcode.
- [`plugins/`](plugins/) contains personal plugins kept separately from source
  patches.

The generated `.patched-jcode/` directory is your local, fully patched jcode
checkout. It is not committed.

## Patch conventions

Personal changes use `personal-` in their patch filename. They are for behavior
that you want to keep in this soft fork.

Potential upstream contributions use `candidate-` in their patch filename and
have the `upstream-candidate` kind. Keep them focused and suitable for
submitting to jcode independently of personal changes.

## Workflows

Run `just help` to see every command.

### 1. Sync jcode and carry patches forward

Sync to the latest upstream `master`:

```bash
just sync
```

Or sync to a specific upstream release tag:

```bash
just sync vX.Y.Z
```

This refreshes the local upstream base and rebuilds `.patched-jcode/` with your
patches. If upstream changes conflict with a patch, resolve the conflict in
`.patched-jcode/`, amend the affected commit, then run `just snapshot-patches`.

When you are happy with the synchronized catalog, push both the catalog and
selected upstream base:

```bash
just push
```

### 2. Make a change (commit-first workflow)

Never edit `.patch` files directly. Work in `.patched-jcode/` and let the
snapshot tool regenerate patches from commits.

```bash
just create-patched-copy          # ensure .patched-jcode is clean and current
just list-patches                 # see which commit maps to which patch
# edit source files in .patched-jcode/
# commit or amend the relevant commit there
just snapshot-patches             # regenerate every .patch from commits
just validate-patch-files         # confirm patches apply cleanly
just test-patch-file patches/<name>.patch   # run the patch's validation
```

Each commit above `master` in `.patched-jcode/` is one patch. The commit message
must include the `X-Jcode-Patch-*` headers and the body sections (`Patch intent:`,
`Why it exists:`, `Upstream integration points:`, `Update guidance:`,
`Validation:`). See `AGENTS.md` for the full format.

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
just test-patch-file patches/1001-personal-release-installer-path-opt-in.patch
```

Use this after snapshotting a change, and before relying on it after an
upstream sync.

### 5. Prepare an upstream contribution

Create a branch from a candidate patch:

```bash
just create-upstream-candidate-branch-from patches/0001-candidate-pre-tool-input-transformers.patch
```

Review the created branch, then use it to open an upstream contribution. Keep
candidate patches independent from personal-only patches whenever possible.
