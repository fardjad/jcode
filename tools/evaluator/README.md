# jcode harness client

## Tiered worker evaluation

Optional `[worker_models]` maps arbitrary discovered worker IDs to model strings.
Omitted entries use `[models].swarm`; unknown IDs fail preflight. Only
`coordinator` is reserved. Resolved mappings are recorded in the run manifest.
Worker-specific examples belong in the worker catalog, not generic runner code.
Paid runs still need approved models, a run matrix, and a budget.

Usage summaries add `by_role`, `by_model`, `agent`, and `evaluation_overhead`
views of the same calls. Do not add these views together. Native worker roles
use observed blueprint names, with `swarm` fallback for missing identity.
Aggregate provider-reported cost, unpriced calls, and separate cache counts
remain.

The optional `escalation_resumption` metric analyzes the scenario-local
escalation controller evidence that the runner retains into the raw root
before temporary cleanup: `other/events.jsonl`, `other/guard-events.jsonl`,
`other/state.json`, `other/artifacts/`, and spilled replacements under
`hook-payloads/<session>/`. The controller mode is resolved from the event
journal itself: blind journals carry `mode: blind`; older scripted journals
have no mode field and stay on the unchanged scripted path. The runner
always sets the controller mode explicitly (`blind` for live runs) and
records it in the manifest and effective controller provenance. The metric
is not a correctness gate. In blind mode the report is purely observational:
a private judge-aware fault timeline, behavior coverage (worker delegation,
higher-tier assistance, resume handoff, same-session resumption, guard
extractions) with no required tier route, artifact shielding, and unknowns
for missing evidence; unexercised coverage is reported, never a violation,
and any reasonable tier route or finishing without escalation is
acceptable. Guard extractions correlate only guard runs that actually
replaced a result: the extraction child is a later spawn by the same
immediate parent whose native assignment names the exact saved payload
path from the guarded call's retained result and whose actual report is
retained with the required facts whenever the evidence can derive them;
a replacement-free guard run makes no extraction row. Native access to
controller-private journal paths is reported as observed or uncertain in
`private_access`, never claimed as verified blindness. In scripted mode it
reports the historical dimensions
(`orchestration` choreography, `context_shielding`, `evidence_unknown`)
unchanged. Every
controller record is re-emitted with a native citation and the exact session
names joined from native evidence; unknown or missing evidence leaves the
report incomplete, never clean.

The optional `coordination` metric reads fenced `coordination` JSON notes:
`event` (`blocked`, `assistance`, `handback`, `done`), `task_id`, and, for links,
`blocker_id` and `source_session_id`. Correlation requires observed identities,
source notes, and a native spawn assignment carrying the task/blocker/source
identities as exact identifier tokens, so prefixes such as `task1` inside
`task10` never correlate and unknown lineage stays unknown. Sibling helpers
must share a coordinator parent; a parent and its own child also correlate
through the native spawn edge plus the shared task identity, keeping
three-level root→advanced→routine lineage trustworthy without new packet
schema. Missing evidence is incomplete. These are structural facts, not proof
that help was useful or retries were meaningful.
Review cited assignments/results for scope and quality. Independent acceptance
remains the sole correctness gate. Completed-report paging is not resumption.

Compare saved run summaries offline, without provider calls:

```text
python3 tools/scripts/isolate_config.py -- uv run --project tools/evaluator --extra eval jcode-eval compare baseline/summary.json candidate/summary.json
```

Pairs must match scenario IDs, source hashes, and fixture hashes. Reports retain
all tasks and show acceptance, agent cost/tokens per accepted task including
failed spend, timing medians/ranges, and missing usage/pricing. Cache counts and
role/model details stay in each pair. Check manifests separately for controlled
coordinator/simulator/judge settings, build and environment. Compare repeated
runs pair by pair and examine variance before concluding improvement. Legacy
summaries without agent breakdowns cannot establish agent-only savings.

Offline tests require the optional evaluation dependencies:

```text
python3 tools/scripts/isolate_config.py -- uv run --project tools/evaluator --extra eval pytest -q tools/evaluator/tests
```

Synthetic trace tests prove analyzer behavior, not model compliance. Live paired
runs and trace review are required before claiming token or latency improvement.

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
# Create the owner-only shared config.toml (mode 600, no overwrite)
# from the shipped credential-free example.
tools/evaluator/.venv/bin/python -m jcode_harness_client init-config \
  customizations/workers/evals/config.toml

# Validate the shared config plus every selected scenario's TOML settings
# overrides without any build or paid call.
tools/evaluator/.venv/bin/python -m jcode_harness_client validate-settings \
  --config customizations/workers/evals/config.toml \
  --scenarios customizations/workers/evals/scenarios
```

Settings are inherited and merged key-wise: a required shared
`config.toml` in `customizations/workers/evals/` is merged under every
scenario's public TOML `settings` mapping, per key inside the known
tables (`provider`, `models`, `worker_models`, `limits`, `effort`,
`worker_effort`, `delegation_guard`). A scenario key overrides the
defaults value, an explicit list (including an empty
`delegation_guard.worker_blueprints`) replaces the defaults list, and
unmentioned keys inherit; a scenario without a `settings` mapping
inherits everything. Credential fields are rejected at any nesting in
the public scenario overrides, and every selected scenario's overrides are
validated before any build or paid call. The shared `config.toml` is
required: a missing config file is a clear preflight error even when no
scenario overrides exist, and there is no retired single-file fallback.
Provenance records the raw scenario config, the private config file, and a
sanitized effective-settings hash; secrets never appear in manifests.

```sh
tools/evaluator/.venv/bin/python -m jcode_harness_client run \
  --binary /path/to/jcode \
  --bridge /path/to/jcode-harness-api-bridge \
  --config customizations/workers/evals/config.toml \
  --scenarios customizations/workers/evals/scenarios \
  --blueprints customizations/workers/blueprints \
  --plugins customizations/plugins \
  --build-provenance /path/to/build-provenance.json \
  --output customizations/workers/evals/results/run-1
```

### Scenarios, metrics, judge criteria, and simulated user

Scenarios have no schema-version field. They contain exactly `id`, `fixture`,
`prompt`, `accept`, `metrics`, `judge`, and `simulated_user`, plus an optional
credential-free `settings` mapping of shared-defaults overrides:

```toml
id = "polyglot-rest-api-fixer"
fixture = "fixtures"
prompt = "In the current workspace, implement RestAPI according to README.md so the unit tests pass completely."
accept = ["python3", "-m", "unittest", "rest_api_test.py"]
metrics = ["tool_health"]

[judge]
criteria = ["Define good controllable coordination and worker behavior here."]

[simulated_user]
max_turns = 8
instructions = "Approve reasonable plans when asked and encourage the coding agent to make sound decisions itself. Do not provide code or weaken acceptance. Continue until the agent explicitly announces completion."
```

`metrics` selects named built-in analyzers through a small registry. Unknown
metric IDs fail preflight; new built-ins need no schema shape change.
`judge.criteria` is a list of strings. Judge-criteria authoring policy,
including the planning-only coordinator boundary and worker non-use review,
is owned by
[`customizations/workers/AGENTS.md`](../../customizations/workers/AGENTS.md).
Criteria cannot disable complete coverage, dynamic ownership, or citation
validation, and the judge does not score task correctness or model
intelligence; independent `accept` is the sole correctness gate.

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

### Scenario settings overrides and timeout budgets

Each scenario may define a public, credential-free `settings` mapping in its
own version-controlled `config.toml` under
`customizations/workers/evals/scenarios/<id>/`.
The mapping is any subset of the shared config tables and merges key-wise
over the required private `evals/config.toml`, which stays the
only shared settings file and the only place credentials may live.
Credential-named fields are rejected at any nesting depth. The retired local
per-scenario `<scenario-id>.toml` copies are archived under the ignored
`evals/results/migration-archives/` (mode 600); the loader never reads them and
has no compatibility fallback. The CLI requires `--config` and validates
every selected scenario's TOML overrides before any build or paid call:
unknown keys, credential fields, malformed names, and path traversal all
fail preflight.

Migration from the retired single-file and per-scenario TOML formats is
complete. Archived copies remain available for historical reference, but
there is no migration command or compatibility path in the active evaluator.

`turn_timeout_seconds` bounds one coordinator turn, including any delegated
worker wait inside that turn. `execution_timeout_seconds` bounds the whole
coordinator execution, measured from the first started task, including worker
waits and simulated-user handoffs; it excludes the build, the independent
`acceptance_timeout_seconds` acceptance budget, and judge review, which keep
their own separate budgets. Values are positive integers in seconds; an
omitted `execution_timeout_seconds` leaves the execution unbounded except by
the per-turn limit. The escalation-resumption scenario documents the approved
1200/1800 budgets in its TOML settings; every other scenario keeps the
300-second default by inheriting the shared config.

### Reasoning effort routing

Each settings file may set plain `[effort]` levels (`low`, `medium`, `high`)
for `coordinator`, `swarm`, `judge`, and `simulator`, plus a
`[worker_effort]` map keyed by discovered worker blueprint; a worker
without an entry inherits the swarm effort, and omitted values keep provider
defaults. There are no recursion aliases such as `swarm`. The coordinator
level becomes the native `[agents] swarm_root_effort`, the swarm level
becomes `swarm_effort`, and worker entries become per-blueprint
`[agents.worker_blueprints.<id>] effort` overrides. Judge and simulator
calls map onto each provider's real request fields: `reasoning_effort` for
OpenAI-compatible chat, Anthropic's output-config `effort` via the SDK's
`anthropic_effort`, and OpenRouter's generic top-level `reasoning` object
(`{"effort": ...}`). Shipped examples pin the `advanced` and `expert`
workers to `medium`; all other efforts stay unset by default.

At runtime the runner gives every coordinator turn the budget
`min(turn limit, remaining execution budget)`, so worker waits inside a turn
can never exceed the overall deadline, and never passes a negative or zero
wait: an exhausted deadline stops the scenario before the next turn with a
clear `timeout` status, the `execution-deadline` termination, and no silent
retry. All evidence is preserved and independent acceptance still runs.

Provenance is recorded at every layer: `manifest.json` carries the configured
overrides plus the documented presets, each scenario's `effective/config.json`
carries its resolved budgets and their sources, and the execution record
carries the applied budgets and a `deadline_exceeded` flag.

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

Each `scenario-summary.json` and run `summary.json` reports one `cost_usd`
total summing provider-reported costs from native coordinator/worker session
`token_usage`, simulator calls, and judge calls without double counting.
Every priced call records its cost provenance in `cost_sources`:
`reported-native` for a numeric cost field retained by native session usage
(including `0.0`), and `reported-wire` for provider-reported cost in a
response body or provider details. A cost is only taken from
provider-reported fields, and only when it is a finite nonnegative real
number (zero is valid; booleans, negatives, NaN, and infinities are
rejected), so malformed or missing provider-reported costs stay unpriced
with a safe reason instead of poisoning totals. There is no token-count
estimate, bundled price database, or SDK `cost()` fallback. Historical
costs are never invented or back-filled. Any call without a usable
provider-reported cost increments `unpriced_calls` (omitted when zero)
instead of silently reporting `$0`, and every aggregate also carries
`unpriced_reasons` with a concrete, redaction-safe reason per unpriced call
(`no provider-reported cost in native session usage` or
`no provider-reported cost in wire response`). Reasons never contain
settings, URLs, credentials, raw wire bodies, or exception messages. The
remaining limitation is that native sessions which retain token usage but
no reported cost field cannot be priced.

### Dynamic blueprints and production hooks

Only `coordinator` is reserved. Every discovered worker Markdown file is an
input, not a Python registration. Workers may be added, removed, or renamed
without evaluator changes. Native `worker_blueprint_policy.name`,
`parent_session_id`, request snapshots, and generated tool definitions establish
what was actually available and exercised. Session ancestry retains both the
report-back `parent_session_id` and the raw transcript `parent_id`; when the
native listing omits `parent_session_id`, the actual transcript parent is used
and nothing is fabricated when both are missing, so nested lineage and worker
coverage stay trustworthy and each session's native usage is counted once.
Unselected workers are reported as
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

The delegation guard's worker selection and threshold are explicit evaluator
settings (`[delegation_guard]` in the shared config or a scenario
`settings.delegation_guard` override). `worker_blueprints`
defaults to `advanced` and `expert`, accepts only those ids, and an empty list
disables worker-role guarding while the coordinator nudge stays active;
`threshold` defaults to 8192 and must be a nonnegative integer. The values are
injected into the isolated runtime environment as
`JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS` and `JCODE_DELEGATION_GUARD_THRESHOLD`
only when the delegation-guard plugin is part of the run, and are never
inherited from the caller's environment. The manifest records the sanitized
settings values, the per-scenario effective config records the exact injected
environment, and hook traces capture the guard variables alongside session
identity.

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

Validated judge findings are deduplicated deterministically before the review is
written. Two findings collapse only when they name the same owner (kind, id, and
path), use the same finding kind, and have exactly equal observed and expected
strings, including case and whitespace. The first finding in stable page order
keeps its canonical observed and expected wording, a collapsed duplicate raises
severity to the strongest one supported, lowers confidence to the minimum seen,
and unions citations in first-seen order, so an exact duplicate claim cited
through different evidence records still collapses. Distinct suggestions are
never discarded: suggestion wording unique after whitespace and case
normalization is joined, in stable first-seen order with the first canonical
wording kept verbatim, into the single suggestion string the fixed finding
schema already provides. Findings that cite exactly the same records but differ
in observed or expected text remain separate, distinct issues that merely share
an owner, kind, or some citations are never merged, and violation versus
improvement-opportunity nuance is preserved. No semantic similarity is inferred
by the deterministic identity. The judge instructions continue to ask for one
canonical finding per underlying cause; deterministic dedup is a structural
duplicate guard, not a replacement for that canonical-judgment instruction.

The result dimensions are independent:

- `execution`: `completed`, `runtime error`, or `timeout`
- `acceptance`: `pass`, `fail`, `error`, or `not run`
- `evidence`: `complete` or `incomplete`, with explicit gaps
- `metrics`: each selected built-in's independent report; `tool_health` is
  `healthy`, `issues found`, `incomplete`, or `not requested`
- `simulated_user`: paid calls, turn cap, and termination reason
- `review`: `clean`, `findings`, or `incomplete`
- `cost_usd`: provider-reported scenario/run total, with `unpriced_calls` when nonzero

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

## Blind runtime boundary

The blind boundary (`jcode_harness_client.runtime.launch_blind_harness`) is
scenario-specific: only the escalation-resumption scenario opts into it by
default, because its escalation controller runs in blind mode. That
scenario's live runs launch the native daemon and bridge inside a real bwrap
sandbox with an empty root, a fresh PID namespace, and only the public staging
tree (`bin`, `workspace`, `config`, `runtime` under `/var/tmp/<run>/`) mounted
at identical absolute paths. Every other scenario keeps the plain host
runtime by default: no sandbox, staging root, or broker is created for it,
even when bwrap is installed. The staged workspace is a copy of the
materialized fixture; pristine copies, acceptance, judge, and instrumentation
stay on the host. Preflight is fail-closed: missing bwrap, no plain system
Python, or a missing native binary aborts the blind scenario rather than
degrading to a host-visible launch. `run()`'s `runtime_isolation` argument is
an explicit test/internal override of the scenario defaults, never a live
configuration option; an explicit override wins for any scenario after
validation. The public staging root is removed once acceptance and retention
complete, and a per-scenario `finally` removes it on any hard failure, so no
evaluation copy lingers in a public location; only the temp stage the run
itself created is ever deleted.

Inside the sandbox the only broker-facing artifact is the stateless public
hook proxy. The daemon's host PID is discovered by exact launch argv inside
the bwrap process group and registered with the host `HostHookBroker` before
any turn runs; hook requests authenticate through parent-PID plus exact
`/proc` cmdline re-verification, so forged or unregistered callers are
denied. The actual RTK, delegation-guard, and escalation-controller wrappers
execute host-side as private broker operations with their private
environment; controller and guard variables never enter the sandbox. The
`HOME`/`JCODE_HOME`/XDG routing inside the sandbox is set only through the
validated `public_dirs` channel (`PUBLIC_DIR_ENV_KEYS` values must resolve
inside the public staging root), never through free-form environment
variables. The manifest records the `runtime_isolation` override (absent for
scenario defaults) plus each scenario's effective isolation in
`runtime_isolation_by_scenario`, and the effective config
records `filesystem_isolation` provenance (mode, staging root, mounts, broker
endpoint, host-private locations) with no secrets.
