# jcode harness client

`jcode-harness-client` is a synchronous Python 3.14 package for driving
sessions through the versioned jcode harness API, plus an optional real-code
harness evaluation runner (`jcode-eval`). The core client has no runtime
dependencies, does not import or require Opik, and is not coupled to any
evaluation framework.

## Client usage

```python
from jcode_harness_client import HarnessClient

with HarnessClient.unix(socket_path="/run/user/1000/jcode-api.sock") as client:
    session = client.create_session(working_dir="/workspace/project")
    first = session.send_message("Hi")
    second = session.send_message("I need help with an order")
    print(second.text)
```

A callback-based framework can use the same result without owning a session
object:

```python
from jcode_harness_client import HarnessClient

with HarnessClient.tcp(host="jcode-vm", port=8765) as client:
    result = client.send_message(
        "Hi",
        thread_id="simulation-scenario-42",
        working_dir="/workspace/project",
    )
    assistant_message = result.as_dict()
```

## Continue a session from prior conversation history

`create_session(history=...)` can initialize a new session with ordinary
persisted user and assistant messages before the first live model turn:

```python
from jcode_harness_client import ConversationTurn, HarnessClient

history = [
    ConversationTurn("user", "My name is Ada."),
    ConversationTurn("assistant", "Nice to meet you, Ada."),
    ConversationTurn("user", "What is my name?"),
    ConversationTurn("assistant", "Your name is Ada."),
]

with HarnessClient.unix("/run/user/1000/jcode-api.sock") as client:
    session = client.create_session(
        working_dir="/workspace/project",
        history=history,
    )
    result = session.send_message("Continue from there.")
    print(session.session_id, result.text)
```

The returned `session_id` is a normal durable jcode session ID. After the
client closes, use jcode's normal session picker, CLI, or API resume flow to
restore it. The Python client never reads or writes jcode session JSON or
journal files directly.

### History contract

- History is preserved exactly in order and text as ordinary `user` and
  `assistant` transcript messages. Repeated roles are not merged.
- A nonempty history must start with `user`, alternate roles, and end with
  `assistant`. The final assistant turn is required so resume logic does not
  mistake a trailing imported user message for an interrupted live turn.
- V1 supports only plain-text user and assistant turns. Images, system prompts,
  tool calls/results, timestamps, token usage, and provider-specific metadata
  are rejected rather than ignored.
- Limits are 100 user/assistant pairs, 64 KiB UTF-8 per message, and 1 MiB
  UTF-8 across the complete history. Empty content is rejected. `None` and
  `[]` omit the wire field and preserve ordinary empty-session behavior.
- The client validates all of this before sending a create request. It requires
  the negotiated `initial_history` capability and raises
  `UnsupportedCapabilityError` when an older bridge does not advertise it.
  Empty-session creation remains compatible with older bridges.
- The bridge validates again, probes the daemon's
  `session_initial_history` capability, and sends one internal batch request.
  `Attached` is not returned until the daemon confirms durable persistence.
  Failed validation or persistence returns an error and does not expose a
  successful seeded session.
- Seeding does not generate assistant text, turn lifecycle events, tool events,
  or token statistics. The next normal `send_message` creates the first live
  model turn and receives the imported transcript as provider context.

This is different from `send_message(..., no_reply=True)`: that operation adds
one user context message to an already attached session and cannot reconstruct
historical assistant replies.

`send_message` returns a `HarnessResult` with `text`, `session_id`,
`completed_normally`, and `abnormal`. Abnormal stops also carry `stop_reason`,
`stop_message`, and `provider_stop_reason`. `as_dict()` returns only the
generic `{"role": "assistant", "content": ...}` message. Transport,
protocol, API request, and turn-timeout failures raise typed exceptions;
abnormal stops are valid results and can include partial assistant text.
For frameworks that prefer a nested field, `stop_info` returns those three
stop strings or `None` for natural completion.

The default turn timeout is five minutes and can be set on the client or
overridden per message. A timed-out turn sends `Cancel`; that session remains
unavailable until its terminal events have drained. There is at most one turn
in flight per session. Different sessions may be driven concurrently.

Without a `thread_id`, each `client.send_message` call creates a new session.
With one, the mapping is in memory for the client's lifetime and later calls
reuse that created session. Its `working_dir` is fixed by the first call; a
conflicting explicit directory raises `ValueError`. Bindings are not persisted,
evicted, or restored after process restart. Close the client, preferably with a
`with` block, to best-effort detach its sessions.

## Daemon and TCP relay topology

The client only connects to an already-running jcode daemon and harness API
bridge. It never starts either process, supervises them, or starts `socat`.
For local use, point `HarnessClient.unix()` at the bridge's Unix socket.

For a VM/container, run the daemon and API bridge inside that environment, next
to each other, and forward a TCP port to the API bridge socket. For example,
start a bridge on `/run/jcode-api.sock` and run this relay in the VM/container:

```sh
socat TCP-LISTEN:8765,bind=0.0.0.0,reuseaddr,fork UNIX-CONNECT:/run/jcode-api.sock
```

`HarnessClient.tcp(host="jcode-vm", port=8765)` speaks the same NDJSON API
over the forwarded TCP stream. The daemon and bridge remain inside the
VM/container. Expose the forwarded port only on a trusted network and apply the
network access controls required by that deployment.

## Real-code scenario evaluation runner (`jcode-eval`)

The package also contains one **live** real-code scenario evaluation pipeline.
It uses actual coordinator/worker models, a scenario-configured Pydantic AI
simulated user, native sessions, production hooks, and a Pydantic AI diagnostic
judge. It has no scripted provider, fixed user responses, assertion DSL, or
fixed worker registry. Worker-specific scenarios and assets live in
[`../../customizations/workers/`](../../customizations/workers/README.md); this package remains generic.

From the catalog root, the normal interface is:

```sh
just eval-workers                         # all bundled scenarios, once each
just eval-workers polyglot-rest-api-fixer # one dynamically selected scenario
```

These commands make live provider and judge calls and may incur cost. They are
not part of `just test-catalog`. The recipe first builds or reuses the exact
resolved `VERSION.txt` base plus every ordered catalog patch. Build fingerprints
cover patch contents, targets, debug profile, and Rust toolchain. Verified stamps
and binary hashes provide the no-Cargo fast path. Build logs and statuses live
under `.tools/worker-eval/builds/`; source worktrees are dedicated to evaluation,
never `.patched-jcode`. Each run manifest records this provenance.

The generic CLI is also explicit about every input:

```sh
tools/evaluator/.venv/bin/python -m jcode_harness_client init-settings workers

tools/evaluator/.venv/bin/python -m jcode_harness_client run \
  --binary /path/to/jcode \
  --bridge /path/to/jcode-harness-api-bridge \
  --settings customizations/workers/.eval-settings.toml \
  --scenarios customizations/workers/scenarios \
  --blueprints customizations/workers/blueprints \
  --plugins customizations/plugins \
  --build-provenance /path/to/build-provenance.json \
  --output customizations/workers/results/run-1
```

### Scenarios, metrics, judge criteria, and simulated user

Scenarios have no schema-version field. They contain exactly `id`, `fixture`,
`prompt`, `accept`, `metrics`, `judge`, and `simulated_user`:

```yaml
id: polyglot-rest-api-fixer
fixture: ../fixtures/polyglot-rest-api
prompt: >-
  In the current workspace, implement RestAPI according to README.md so the
  unit tests pass completely.
accept: [python3, -m, unittest, rest_api_test.py]
metrics: [tool_health]
judge:
  criteria:
    - >-
      Good coordination uses suitable workers for nontrivial work, gives them
      bounded assignments, and independently verifies the result. Unjustified
      worker non-use is a coordinator finding.
simulated_user:
  max_turns: 8
  instructions: >-
    Approve reasonable plans when asked and encourage the coding agent to make
    sound decisions itself. Do not provide code or weaken acceptance. Continue
    until the agent explicitly announces completion.
```

`metrics` selects named built-in analyzers through a small registry. Unknown
metric IDs fail preflight; new built-ins need no YAML shape change.
`judge.criteria` states what good controllable coordinator, worker, tool, and
harness behavior looks like, including when worker non-use is a delegation
mistake. It does not score task correctness or model intelligence; independent
`accept` is the sole correctness gate. Criteria cannot disable complete coverage,
dynamic ownership, or citation validation.

The simulated user's model comes from private settings, while its behavior and
turn cap come from the scenario. After each normal coordinator turn, the
Pydantic AI actor receives the original task and conversation and returns strict
`{done, message}` structured output (with a 512-token response cap). A false
reply becomes the next native user message; `done` is allowed only when the
agent explicitly announces completion. Its sanitized wire bodies live under
`raw/simulated-user/`. Exhausting the turn cap is an execution error, not an
acceptance result.

The loader rejects unknown/duplicate fields (including `version`), aliases,
malformed argv, bad fixtures, and symlinks. There is no worker name or delegation
flag.

### Pydantic AI caching and total cost reporting

Simulator and judge calls run through a shared Pydantic AI backend with:

- `Caching()` enabled by default using stable cache-routing identities derived
  from scenario ID, role, and configuration/context hashes (never per-request
  random keys), plus `CachePoint` markers separating common prefixes from
  turn- or page-specific payloads;
- zero SDK retries (`max_retries=0`) and zero Pydantic AI output retries
  (`retries=0`, `UsageLimits(request_limit=1)`);
- transport-level interception that writes sanitized `.input.json` and
  `.response.json` wire bodies with mode `600` and never retains auth headers.

Prompt caching is not assumed to work or save money automatically—cache writes
can cost more than uncached input, and custom OpenAI-compatible/Azure routes may
ignore or reject cache parameters. Always inspect `usage.cache_read_tokens` and
`usage.cache_write_tokens` from a small live probe before claiming cache savings.

Each `scenario-summary.json` and run `summary.json` reports one best-effort
`cost_usd` total summing native coordinator/worker session `token_usage`,
simulator calls, and judge calls without double counting. Provider-reported cost
is used when present; otherwise `genai-prices` estimates known models. Any call
whose cost cannot be determined increments `unpriced_calls` (omitted when zero)
instead of silently reporting `$0`.

### Dynamic blueprints and production hooks

Only `coordinator` is reserved. Every discovered worker Markdown file is an
input, not a Python registration. Workers may be added, removed, or renamed
without evaluator changes. Native `worker_blueprint_policy.name`,
`parent_session_id`, request snapshots, and generated tool definitions establish
what was actually available and exercised. Unselected workers are reported as
`not exercised`, which does not assess their unobserved behavior. The judge must
still decide whether each non-use was justified or a coordinator delegation
mistake.

Blueprint and plugin content is snapshotted for the run. The actual RTK and
delegation-guard executables are wrapped by transparent evaluation-only tracers.
The wrappers retain ordered stdin/stdout/stderr, status, duration, environment
and payload context, and referenced large payload files before TTL cleanup. They
forward the original bytes, environment, argv, and exit status without another
timeout. Native post-transform envelopes do not always contain a tool-call ID;
ambiguous correlation stays an explicit gap rather than a guessed association.

### Complete raw evidence, behavior-focused judge projection, and retrieval

Each fresh output directory contains a manifest, summary, and one directory per
scenario with normalized `transcript.jsonl`, readable `transcript.md`, outer
events, native `sessions.json`, `conversation.json`, `changes.patch`, acceptance
artifacts, `metrics.json`, selected metric reports such as `tool-health.json`,
judge coverage/review files, `scenario-summary.json`, and raw evidence. Complete
raw evidence is never truncated or summarized.

To avoid wasting tokens on bulky task outputs, `judge_projection.py` builds a
deterministic behavior-focused projection for judge pages:

- **Included by default**: coordinator and worker narrative messages,
  simulated-user turns, delegation decisions, assignments, final reports, tool
  names/IDs/inputs/schemas/policies/errors/retries/lifecycle, blueprint and
  plugin snapshots, edit file summaries, acceptance status/exit code, and hook
  metadata/correlations.
- **Excluded by default (with explicit retrievable citations)**: non-error tool
  output bodies (such as search/grep results, file reads, and command stdout),
  full source diffs, acceptance stdout/stderr logs, native debug logs, raw
  hook payload binaries, static request prompt dumps, and raw reasoning blobs.

Every excluded item is listed in `judge-coverage.json` under
`excluded_by_default` with its `raw_citation` and reason. During page review or
synthesis, the judge may return exact `retrieval_requests` citation IDs. The
evaluator validates each requested citation against the retained raw bundle,
supplies the full raw records on the next call, records retrieved IDs in
`retrieved_citations`, and counts retrieval rounds against `judge_max_calls`.

The result dimensions are independent:

- `execution`: `completed`, `runtime error`, or `timeout`
- `acceptance`: `pass`, `fail`, `error`, or `not run`
- `evidence`: `complete` or `incomplete`, with explicit gaps
- `metrics`: each selected built-in's independent report; `tool_health` is
  `healthy`, `issues found`, `incomplete`, or `not requested`
- `simulated_user`: paid calls, turn cap, and termination reason
- `review`: `clean`, `findings`, or `incomplete`
- `cost_usd`: best-effort scenario/run total, with `unpriced_calls` when nonzero

A passing acceptance command does not erase delegation, runtime, evidence, or
tool-health findings.

### Isolation and secrets

`launch_harness(...)` starts explicitly selected binaries with Unix sockets,
replaces HOME/JCODE_HOME and every XDG path, drops inherited provider routing,
and terminates process groups. Acceptance uses disposable configuration and an
external Cargo target directory. Settings must be owner-only mode 600 and define
`coordinator`, `swarm`, `judge`, and `simulator` model roles. Credentials are
never serialized into manifests, effective configs, summaries, transcripts,
hook traces, judge payloads, or simulated-user payloads. Consistent redaction
preserves citation structure.

## Development

This directory is a standalone uv project. It does not depend on the rest of
the repository, and every command below runs from `tools/evaluator/`. Use uv and
Python 3.14:

```sh
uv sync --extra eval
python3 ../scripts/isolate_config.py -- uv run pytest -q
python3 ../scripts/isolate_config.py -- uv run ruff check .
python3 ../scripts/isolate_config.py -- uv run ruff format --check .
python3 ../scripts/isolate_config.py -- uv run mypy
```

PyYAML and `pydantic-ai-slim[anthropic,openai,openrouter]` are the evaluation
runtime dependencies. Pytest, Ruff, and mypy are development-only dependencies
recorded in `uv.lock`. Unit tests use fake servers/transports and native
evidence fixtures and never make live provider calls, compile the workspace, or
read personal jcode configuration.

## Manual bridge and daemon smoke test

This optional live smoke test is not part of the checks above. It needs
`socat` and two binaries built from a jcode checkout that includes harness
history seeding:

```sh
cargo build -p jcode --bin jcode
cargo build -p jcode-harness-api-server --bin jcode-harness-api-bridge
```

Then, from `tools/evaluator/`, run it with a throwaway home so no personal
configuration is read:

```sh
smoke_home=$(mktemp -d)
HOME="$smoke_home/home" JCODE_HOME="$smoke_home/jcode" \
  uv run python smoke/run_smoke.py \
  --jcode /path/to/jcode/target/debug/jcode \
  --bridge /path/to/jcode/target/debug/jcode-harness-api-bridge
```
