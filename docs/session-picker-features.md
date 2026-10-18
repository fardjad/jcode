# Session picker features

These settings require catalog patches
`1006-personal-feature-configure-session-picker-sort-order.patch`
and
`0003-candidate-delete-sessions-from-picker-with-ctrl-d.patch`
to be applied.

jcode reads user configuration from `~/.jcode/config.toml`.

## Session picker sort order

Control how the session picker orders sessions with
`[display].session_picker_sort`.

```toml
[display]
session_picker_sort = "recent"
```

Supported values:

```text
recent   Sort by last activity time (default).
created  Sort by session creation time.
name     Sort alphabetically.
```

To open the picker scoped to the current project, use upstream's
`resume_project_scope = true` setting.

## Delete sessions from the picker

Delete the selected session directly from the session picker without leaving
the picker interface. This is a built-in keybinding, no configuration needed.
