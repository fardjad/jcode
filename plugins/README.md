# Custom jcode plugins

This directory versions personal plugins installed locally under
`~/.jcode/plugins`.

## RTK command transformer

`rtk/rtk-transform` is executable source installed at
`~/.jcode/plugins/rtk-transform`. It is Python adapter, not compiled RTK
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

Install or refresh local plugin with:

```bash
install -m 755 plugins/rtk/rtk-transform ~/.jcode/plugins/rtk-transform
```

Configure transformer in jcode config:

```toml
[hooks]
pre_tool_transform = ["~/.jcode/plugins/rtk-transform"]
```

Run isolated contract tests from repository root:

```bash
python3 plugins/rtk/test_rtk_transform.py
```

jcode-side generic pre-tool transformer implementation is supplied by the
candidate patch, including `crates/jcode-base/src/hooks.rs` and
`crates/jcode-app-core/src/tool/mod.rs`.
