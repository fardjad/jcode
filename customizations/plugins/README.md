# Custom jcode plugins

This directory is the complete, versioned plugin installation. Follow the
[get started guide](../../docs/get-started.md) for portable installation and
upgrades with `bun tools/scripts/setup_assets.ts`. It links plugins, worker
blueprints, and skills together, or copies them when symlinks are unavailable.

Run setup from the catalog repository root, then configure the hooks below.
Setup requires Bun. The RTK plugin requires Python 3.14 or newer as `python3`
on PATH.

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
bun tools/scripts/isolate_config.ts -- python3 customizations/plugins/rtk/test_rtk_transform.py
```

The pre-tool transformer is supplied by upstream jcode in
`crates/jcode-base/src/hooks.rs` and `crates/jcode-app-core/src/tool/mod.rs`.
