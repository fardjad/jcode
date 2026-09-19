# Session picker features

These settings require catalog patches
`1003-personal-feature-configure-session-picker-sort-and-filter.patch`
and
`1004-personal-feature-delete-sessions-from-picker-with-ctrl-d.patch`
to be applied.

jcode reads user configuration from `~/.jcode/config.toml`.

## Session picker sort order

Control how the session picker orders sessions with
`[display].session_picker_sort`.

```toml
[display]
session_picker_sort = "created"
```

Supported values:

```text
created     Sort by session creation time (default).
updated     Sort by last activity time.
```

## Session picker default filter

Control the initial filter shown when opening the resume picker with
`[display].session_picker_default_filter`.

```toml
[display]
session_picker_default_filter = "current-dir"
```

Supported values:

```text
current-dir   Only sessions from the current working directory.
all           All sessions.
```

## Delete sessions from the picker

Delete the selected session directly from the session picker without leaving
the picker interface. This is a built-in keybinding, no configuration needed.
