# Installer and update features

These patches customize the installer and self-update behavior.

## Release installer PATH opt-in

Patch `0001-candidate-isolate-release-installer-path-opt-in.patch` keeps release
installer shell PATH changes opt-in. The installer will not modify the user's
shell profile unless explicitly requested.

## Self-update repository selection

Patch `1002-personal-feature-configure-fork-updates-and-identity.patch`
lets fork builds check and install updates from a selected GitHub repository.
The default repository is `fardjad/jcode`; set `JCODE_UPDATE_REPO` to override it.
For example, configure the update repository in the environment:

```bash
export JCODE_UPDATE_REPO="OWNER/REPOSITORY"
```

When `JCODE_UPDATE_REPO` is unset, `fardjad/jcode` is used.
