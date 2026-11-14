# Worker evaluator

The Bun and TypeScript evaluator runs worker scenarios through the jcode
TypeScript SDK. It uses deterministic checks alongside a toolless AI diagnostic
judge. See [AGENTS.md](../../AGENTS.md) and `/evaluate-worker-blueprints` for
the evaluation procedure.

## Setup and offline commands

Run commands from the repository root. Install the tools workspace dependencies
with `cd tools && bun install`. The evaluator materializes the SDK from the
upstream Git tag pinned in `VERSION.txt` into `.tools/sdk/typescript/`; it does
not need `.patched-jcode`. If the pinned tag is not cached, SDK bootstrap may
fetch it. To prepare it explicitly, run:

```sh
bun tools/scripts/isolate_config.ts -- bun tools/catalog/src/cli.ts sdk-sync
```

Create the private settings file from the adjacent credential-free example:

```sh
bun tools/scripts/isolate_config.ts -- bun tools/evaluator/src/cli.ts init-config \
  --config customizations/workers/evals/config.toml
```

Fill in the provider URL, model, and credentials without sharing the file. The
command refuses to overwrite an existing file. Settings must be a regular,
non-symlink file owned by the caller with no group or other permissions, such as
mode 600. Windows credential-file ACL checks are not supported. Public scenario
`settings` tables override shared settings key-wise and cannot contain
credentials. The example documents provider, role and worker model, limit, and
effort settings. Per-worker settings must refer to discovered blueprint IDs;
`coordinator` is reserved.

Validate settings and scenarios without building or calling a provider:

```sh
bun tools/scripts/isolate_config.ts -- bun tools/evaluator/src/cli.ts validate-settings \
  --config customizations/workers/evals/config.toml \
  --scenarios customizations/workers/evals/scenarios
```

Add `--scenario <id>` to select one scenario. Validation reads the private
config and may bootstrap the SDK, including a tag fetch if necessary. CLI help
is available without a config:

```sh
bun tools/scripts/isolate_config.ts -- bun tools/evaluator/src/cli.ts --help
```

## Live runs and results

A live evaluation can incur provider charges. Obtain separate explicit
authorization before running it. Turn limits and timeouts do not cap charges;
set a provider spending limit first.

```sh
just eval-workers                 # all seven default scenarios
just eval-workers <scenario-id>   # one scenario
```

The catalog validates settings before building or reusing native binaries for
the pinned base and current patch stack. It holds the build lock during the
run and passes build provenance to the evaluator. Direct `run` usage requires
`--config`, `--scenarios`, `--binary`, `--bridge`, `--blueprints`, `--plugins`,
and `--output`; `--scenario` and `--build-provenance` are optional. The top-level
`--help` lists the commands and options.

After each coordinator turn, the evaluator runs independent acceptance in a
separate blind sandbox against a snapshot of the produced workspace. Fixture
verifier files explicitly named in the acceptance argv are restored from the
original fixture. On failure, the next turn receives a fixed approval and
continuation prompt. A pass stops the loop; an execution or acceptance error does
not resume it. The judge is skipped if execution fails, and a batch stops after
the first failed scenario or judge error. Its report lists skipped scenarios.
The judge makes one structured-output attempt; invalid output is an error.
The original fixture is checked for changes. The configured simulated-user
instructions are required metadata, but the runner does not call a simulator model.

The run directory under `customizations/workers/evals/results/<run-id>/` and
each scenario directory contain `summary.json` and `report.md`. The Markdown
report contains the JSON result, including acceptance checks, conversation,
SDK and native evidence, deterministic findings, judge review, hook traces,
and build provenance. Judge citations resolve against `reviewCitations`, which
preserves the evidence the judge saw. Deterministic findings resolve against
final `evidence.citations`.

The evaluator redacts the configured API key. Treat reports as sensitive: other
task data can still be present. Provider-reported session costs are summed only
when available; missing cost is unknown, not zero.
Missing evidence is recorded as a gap. A native input or policy rejection is a
gap, not proof of a patch defect. The caller's input and rejection remain in
the native evidence. The exit status is nonzero for execution errors, failed
acceptance, or judge errors; findings alone do not fail a run.
The evaluator does not attach to worker sessions to collect evidence. It keeps
available native messages and dispatch records, and reports unavailable worker
SDK histories and effective tool lists as gaps.

## Blind isolation

Live runs require Linux, Node.js 22.12+, and the pinned sandbox-runtime
package's system dependencies: bubblewrap (`bwrap`), `socat`, and ripgrep (`rg`),
working user, PID, and network namespaces. The evaluator stages a private
workspace, isolated home and configuration, read-only policy files, and copied
native executables. It scrubs the environment and keeps the SDK control home
separate from the sandbox daemon home.
The RTK hook and sandbox use the same staged RTK executable. If RTK is absent,
the hook leaves commands unchanged.

Absent protected configuration paths are staged as readable, read-only
placeholders. They can appear in the coordinator workspace even when the
fixture does not contain them.

The coordinator runs in an isolated network namespace with a strict allowlist
for the configured provider host. Direct host loopback, abstract Unix sockets,
and unrelated host filesystem sockets are unreachable. A configured loopback
provider is reachable through the proxy. Unix socket access is enabled for the
SDK and RTK hook brokers; only their staged socket directory is exposed.
Acceptance runs in a separate sandbox with an empty network allowlist and no SDK
state or provider credentials. It copies the read-only workspace snapshot into
private sandbox tmpfs before running the verifier. System directories including
`/usr` and `/etc` are mounted read-only; blind execution does not hide them.

## Scenario format

Each immediate `scenarios/<id>/config.toml` describes one task; the default
fixture is its adjacent `fixtures/` directory. For example:

```toml
id = "example-task"
fixture = "fixtures"
prompt = "Implement the behavior described in README.md."
accept = ["python3", "-m", "unittest"]

[judge]
criteria = ["Identify actionable coordination issues with evidence."]

[simulated_user]
max_turns = 8
instructions = "Approve reasonable plans without supplying solutions."
```

`prompt`, the nonempty acceptance argv, judge criteria, and simulated-user
settings are required. The ID may be omitted when discovered by directory; if
present it must match the directory name. A direct config path requires an ID.
Optional `[settings]` overrides shared settings. Blind isolation is always used;
the legacy `instrumentation.isolation = "blind"` is accepted but does not
select a different mode. `guard_max_bytes` changes only guarded blueprint copies
staged for that scenario. Worker routing, assistance, and guard use require
native trace evidence and remain unknown without it. Fixture provenance and
controls are documented in [worker evaluation fixtures](../../customizations/workers/evals/README.md).
