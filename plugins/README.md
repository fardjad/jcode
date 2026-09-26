# Custom jcode plugins

This directory is the complete, versioned plugin installation. Symlink it once
instead of copying individual executables or plugin projects:

```bash
mkdir -p ~/.jcode
ln -sfn "$(pwd)/plugins" ~/.jcode/plugins
```

Run the command from the catalog repository root. Every configured plugin path
below is relative to that symlink.

`just ensure-personal-assets` maintains this symlink together with the worker
blueprint symlink and verifies the configured plugin executables. The plugins
use only Python's standard library, so no `uv sync`, virtual environment, or
compilation is needed.

## RTK command transformer

`rtk/rtk-transform` is the executable source at
`~/.jcode/plugins/rtk/rtk-transform`. It is a Python adapter, not a compiled RTK
binary. This is behavioral port of RTK's OpenCode
`hooks/opencode/rtk.ts`: it intercepts jcode's pre-tool-transform event for
`bash`, uses `rtk rewrite` as source of truth, silently fails open, and
replaces command only when RTK returns non-empty different value. jcode has no
separate `shell` tool, so `bash` is equivalent integration boundary.

RTK requires candidate patch
`0001-candidate-pre-tool-input-transformers.patch` to be applied and
configured with `pre_tool_transform`. That patch is not part of this plugin's
catalog patch dependency metadata; plugin source remains external catalog
content.

Configure transformer in jcode config:

```toml
[hooks]
pre_tool_transform = ["~/.jcode/plugins/rtk/rtk-transform"]
```

Run isolated contract tests from repository root:

```bash
python3 plugins/rtk/test_rtk_transform.py
```

jcode-side generic pre-tool transformer implementation is supplied by the
candidate patch, including `crates/jcode-base/src/hooks.rs` and
`crates/jcode-app-core/src/tool/mod.rs`.

## Delegation guard transformer

`delegation-guard/delegation-guard-transform` is the executable source at
`~/.jcode/plugins/delegation-guard/delegation-guard-transform`. It intercepts jcode's
post-tool-transform event and replaces oversized successful tool results with
a compact delegation nudge before they enter the coordinator's context. It
fires only when swarm is enabled and the process role is coordinator. Workers
and non-swarm sessions are unaffected. It fails open on any error.

The delegation guard requires the post-tool transform hook from
`1011-personal-feature-add-hook-context-and-post-tool-transform.patch` to be
applied and configured with `post_tool_transform`. Plugin source remains
external catalog content.

Configure transformer in jcode config:

```toml
[hooks]
post_tool_transform = ["~/.jcode/plugins/delegation-guard/delegation-guard-transform"]
post_tool_transform_timeout_ms = 500
```

Run isolated contract tests from repository root:

```bash
python3 plugins/delegation-guard/test_transform.py
```
