# Installer and update features

Patch `0001-installer-and-fork-updates.patch` customizes the installer and
self-update behavior.

## Release installer PATH opt-in

The release installer keeps shell PATH changes opt-in and will not modify the
user's shell profile unless explicitly requested.

## Self-update repository selection

Fork builds can check and install updates from a selected GitHub repository.
The default repository is `fardjad/jcode`; set `JCODE_UPDATE_REPO` to override
it.
For example, configure the update repository in the environment:

```bash
export JCODE_UPDATE_REPO="OWNER/REPOSITORY"
```

When `JCODE_UPDATE_REPO` is unset, `fardjad/jcode` is used.
