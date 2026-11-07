# Delegation guard plugin

This personal jcode catalog plugin replaces oversized successful tool results
before they enter the coordinator's context. It emits a compact delegation
nudge containing the result size and, when supplied by the runtime, the path to
the full result. It never includes the original result in that nudge and does
not choose a particular worker.

## Source and updates

This is local catalog content based on the design in
`plans/delegation-guard-plugin.md`. Its integration depends on the runtime hook
capabilities supplied by `0002-candidate-add-hook-context-and-post-tool-transform.patch`
and worker blueprint context supplied by
`1003-personal-feature-worker-blueprints-and-session-lifecycle-fixes.patch`.
To update it, compare its input and output contract with the current
`post_tool_transform` hook, retain its fail-open behavior, and run the focused
contract tests below. The runtime, not this plugin, creates the result files
and removes them with its configured TTL cleanup.

## Install and configure

Follow the [get started guide](../../../docs/get-started.md) to install the assets,
then merge the hooks below into `~/.jcode/config.toml`. Without
`post_tool_transform`, the guard does not run. Setup never changes your config.

```toml
[hooks]
pre_tool_transform = ["python3 ~/.jcode/plugins/rtk/rtk-transform"]
post_tool_transform = ["python3 ~/.jcode/plugins/delegation-guard/delegation-guard-transform"]
post_tool_transform_timeout_ms = 500
```

Set an optional byte threshold. Invalid or negative values use the default of
8192 bytes (8 KB):

```text
JCODE_DELEGATION_GUARD_THRESHOLD=8192
```

Worker transforms are disabled by default. To opt in to selected Advanced and
Expert workers, set a comma-separated allowlist in the environment inherited
by jcode:

```sh
export JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS=advanced,expert
```

Only the built-in `advanced` and `expert` blueprint IDs can be selected. Routine,
custom, and unknown worker blueprints remain unchanged. Selected workers are
nudged to ask `swarm_routine` for bounded, read-only extraction from the saved
result file. If that path is missing, the nudge says to report a blocker to the
parent rather than retry blindly.

## Algorithm

1. If `JCODE_HOOK_SWARM_ENABLED` is not `1`, emit nothing. The plugin is
   inactive when swarm is disabled.
2. By default, only `coordinator` processes are transformed. Worker transforms
   require `JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS` to select their trusted
   `blueprint_id`; only built-in `advanced` and `expert` IDs are eligible.
   Internal processes and all other worker blueprints preserve their results.
3. Read precomputed `output_bytes` from the `post_tool_transform` JSON envelope.
   If it is at or below the configured threshold, emit nothing.
4. For a larger coordinator result, emit exactly `{"tool_result":"..."}`.
   The replacement states the byte size and includes `tool_result_file` when it
   is available, so the coordinator can delegate inspection according to its
   own guidance. For a selected worker, the replacement directs bounded,
   read-only extraction through `swarm_routine` and reports a blocker rather
   than retrying if the file path is absent.

The script only reads standard input and environment variables. It neither
creates nor deletes temporary files, and the runtime owns temporary-file TTL
cleanup.

## Test

Run the isolated contract tests from the repository root:

```bash
python3 customizations/plugins/delegation-guard/test_transform.py
```
