# Delegation guard plugin

This personal jcode catalog plugin replaces oversized successful tool results
before they enter the coordinator's context. It emits a compact delegation
nudge containing the result size and, when supplied by the runtime, the path to
the full result. It never includes the original result in that nudge and does
not choose a particular worker.

## Source and updates

This is local catalog content based on the design in
`plans/delegation-guard-plugin.md`. Its integration depends on the runtime hook
capabilities supplied by catalog patches 1022 and 1023. To update it, compare
its input and output contract with the current `post_tool_transform` hook,
retain its fail-open behavior, and run the focused contract tests below. The
runtime, not this plugin, creates the result files and removes them with its
configured TTL cleanup.

## Install and configure

Install the executable by copying it into your jcode plugins directory:

```bash
cp plugins/delegation-guard/delegation-guard-transform \
  ~/.jcode/plugins/delegation-guard-transform
chmod 755 ~/.jcode/plugins/delegation-guard-transform
```

Configure the post-tool transformer:

```toml
[hooks]
post_tool_transform = "~/.jcode/plugins/delegation-guard-transform"
post_tool_transform_timeout_ms = 500
```

Set an optional byte threshold. Invalid or negative values use the default of
8192 bytes (8 KB):

```text
JCODE_DELEGATION_GUARD_THRESHOLD=8192
```

## Algorithm

1. If `JCODE_HOOK_SWARM_ENABLED` is not `1`, emit nothing. The plugin is
   inactive when swarm is disabled.
2. If `JCODE_HOOK_PROCESS_ROLE` is not `coordinator`, emit nothing. Workers
   and internal processes always preserve their results.
3. Read precomputed `output_bytes` from the `post_tool_transform` JSON envelope.
   If it is at or below the configured threshold, emit nothing.
4. For a larger result, emit exactly `{"tool_result":"..."}`. The replacement
   states the byte size and includes `tool_result_file` when it is available,
   so the coordinator can delegate inspection according to its own guidance.

The script only reads standard input and environment variables. It neither
creates nor deletes temporary files, and the runtime owns temporary-file TTL
cleanup.

## Test

Run the isolated contract tests from the repository root:

```bash
python3 plugins/delegation-guard/test_transform.py
```
