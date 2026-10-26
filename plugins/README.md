# Custom jcode plugins

This directory is the complete, versioned plugin installation. Follow the
[get started guide](../docs/get-started.md) for portable installation and
upgrades with `python3 tools/scripts/setup_assets.py`. It links plugins and worker
blueprints together, or copies them when symlinks are unavailable.

Run setup from the catalog repository root, then configure the hooks below.
Asset setup and plugins require plain Python 3.14 or newer as `python3` on PATH.
They do not require uv.

## RTK command transformer

`rtk/rtk-transform` is the executable source at
`~/.jcode/plugins/rtk/rtk-transform`. It is a Python adapter, not a compiled RTK
binary. This is behavioral port of RTK's OpenCode
`hooks/opencode/rtk.ts`: it intercepts jcode's pre-tool-transform event for
`bash`, uses `rtk rewrite` as source of truth, silently fails open, and
replaces command only when RTK returns non-empty different value. jcode has no
separate `shell` tool, so `bash` is equivalent integration boundary.

RTK requires the `pre_tool_transform` hook, which upstream jcode ships
since v0.89 (merged from the former candidate patch
`0001-candidate-pre-tool-input-transformers.patch`). Plugin source remains
external catalog content.

Configure transformer in jcode config:

```toml
[hooks]
pre_tool_transform = ["python3 ~/.jcode/plugins/rtk/rtk-transform"]
```

Run isolated contract tests from repository root:

```bash
python3 tools/scripts/isolate_config.py -- python3 plugins/rtk/test_rtk_transform.py
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
`0002-candidate-add-hook-context-and-post-tool-transform.patch` to be
applied and configured with `post_tool_transform`. Plugin source remains
external catalog content.

Configure transformer in jcode config:

```toml
[hooks]
post_tool_transform = ["python3 ~/.jcode/plugins/delegation-guard/delegation-guard-transform"]
post_tool_transform_timeout_ms = 500
```

Run isolated contract tests from repository root:

```bash
python3 tools/scripts/isolate_config.py -- python3 plugins/delegation-guard/test_transform.py
```
