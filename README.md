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
patches. If upstream changes conflict with a patch, use AI assistance to update
the affected patch, then run the command again.

When you are happy with the synchronized catalog, push both the catalog and
selected upstream base:

```bash
just push
```

### 2. Install the patched version

Build and install the current patched jcode:

```bash
just install-patched-version
```

This refreshes `.patched-jcode/`, builds it, and installs the resulting jcode
version locally.

### 3. Validate and test patches

Validate the patch catalog and ensure the patches apply:

```bash
just validate-patch-files
```

Test one patch and its declared validation command:

```bash
just test-patch-file patches/1001-personal-release-installer-path-opt-in.patch
```

Use this after changing a patch, and before relying on it after an upstream
sync.

### 4. Prepare an upstream contribution

Create a branch from a candidate patch:

```bash
just create-upstream-candidate-branch-from patches/0001-candidate-pre-tool-input-transformers.patch
```

Review the created branch, then use it to open an upstream contribution. Keep
candidate patches independent from personal-only patches whenever possible.
