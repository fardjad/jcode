# jcode harness client and evaluator

`jcode-harness-client` provides a synchronous Python client for the versioned
jcode harness API. `jcode-eval` runs real-code worker evaluations using that
client. For evaluation principles and the review procedure, see
[AGENTS.md](../../AGENTS.md) and `/evaluate-worker-blueprints`.

## Evaluation setup

From the repository root, install the evaluator dependencies and create the
private shared configuration from the adjacent credential-free example:

```sh
uv sync --project tools/evaluator --extra eval
uv run --project tools/evaluator --extra eval jcode-eval init-config \
  customizations/workers/evals/config.toml
```

Fill in the provider URL, model, and credentials in the newly created file;
the command refuses to overwrite an existing file. The config must be a
regular owner-only file (mode 600); credential-file ACL checks on Windows are
not supported. Public scenario `settings` tables override shared settings
key-wise and cannot contain credentials. The example documents the supported
provider, role and worker model, limit, effort, and delegation guard settings.
Workers are discovered from the supplied blueprints; only `coordinator` is
reserved. Per-worker settings must refer to discovered worker IDs.

Validate the selected settings without building or making provider calls:

```sh
uv run --project tools/evaluator --extra eval jcode-eval validate-settings \
  --config customizations/workers/evals/config.toml \
  --scenarios customizations/workers/evals/scenarios
```

Add `--scenario <id>` to validate one scenario. Settings validation reads the
private config, so keep its contents and credentials private.

## Run and results

After explicit authorization for a paid live evaluation, run:

```sh
just eval-workers                 # all scenarios
just eval-workers <scenario-id>   # one scenario
```

The recipe validates settings before building or reusing binaries for the pinned
base and current patch stack. It runs the evaluator with isolated configuration.
For direct use, `jcode-eval run --help` lists the required binary, bridge,
config, scenario, blueprint, plugin, and output paths and optional scenario
and build provenance inputs. An output directory must be empty.

Each run writes one `report.md` under
`customizations/workers/evals/results/<run-id>/`, with a section per scenario:
acceptance, provider-reported cost or unknown, timings, delegation observations,
and cited findings. Native session journals and cited evidence are retained in
the scenario directories. The report includes scenario instrumentation events
when available. Secrets are redacted. Missing evidence is recorded as a gap;
findings alone do not determine the exit code. The runner exits nonzero for run
errors or failed acceptance.

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
Optional `[settings]` overrides shared settings. Optional private
`[instrumentation]` declares blind isolation, a scenario-local controller,
environment, and journal names; see the bundled escalation scenario for an
example. Fixture provenance and controls are documented in
[worker evaluation fixtures](../../customizations/workers/evals/README.md).

## Client usage

The core client has no runtime dependencies. Connect to an already-running
jcode daemon and harness API bridge:

```python
from jcode_harness_client import HarnessClient

with HarnessClient.unix(socket_path="/run/user/1000/jcode-api.sock") as client:
    session = client.create_session(working_dir="/workspace/project")
    result = session.send_message("Hi")
    print(session.session_id, result.text)
```

`HarnessClient.tcp(host=..., port=...)` connects to a forwarded bridge. The
client does not start or supervise the daemon, bridge, or network relay.
`send_message` returns a `HarnessResult` with text, session ID, completion
status, and abnormal-stop details; transport and protocol failures raise typed
exceptions. A `thread_id` passed to `client.send_message` reuses its session
within that client instance. The default turn timeout is five minutes and may
be overridden. Only one turn may be in flight per session.
