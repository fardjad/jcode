# Installer and update features

These patches customize the installer and self-update behavior.

## Release installer PATH opt-in

Patch `1001-personal-release-installer-isolate-release-installer-path-opt-in.patch` keeps release
installer shell PATH changes opt-in. The installer will not modify the user's
shell profile unless explicitly requested.

## Self-update repository selection

Patch `1007-personal-feature-allow-selecting-the-self-update-repository.patch`
lets fork builds check and install updates from a selected GitHub repository
instead of the upstream default. Configure the update repository in
`~/.jcode/config.toml`:

```toml
[updates]
repository = "fardjad/jcode"
```

When unset, the default upstream repository is used.
