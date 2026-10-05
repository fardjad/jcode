# jcode Harness Client

`jcode-harness-client` is a synchronous Python 3.14 package for driving
sessions through the versioned jcode harness API. It has no runtime
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
        thread_id="simulation-case-42",
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

## Development

Use uv and Python 3.14. From the repository root, run `cd evaluator && uv sync`
to install the editable package and development tools. `just test-evaluator`
runs pytest, Ruff lint/format checks, and mypy with a disposable `HOME`,
`JCODE_HOME`, and XDG directories supplied by `scripts/isolate_config.py`.
The Python package has no runtime dependencies. Pytest, Ruff, and mypy are
development-only dependencies recorded in `uv.lock`.

## Manual bridge and daemon smoke test

This optional live smoke test does not run as part of `just test-evaluator`.
From the repository root, run:

```sh
just test-evaluator-smoke
```

The recipe refuses to build from a dirty `.patched-jcode`, builds jcode and the
bridge with isolated configuration, then runs the smoke helper. The helper
creates a restricted OpenAI-compatible provider profile, temporary working and
socket directories, and a loopback fake completions endpoint. It launches the
daemon, bridge, and `socat` relay with disposable `HOME`, `JCODE_HOME`, and XDG
variables, exercises the client over TCP, checks for a normal `smoke reply`,
and terminates every child process. It makes no external model requests. The
unit tests cover abnormal event order, cancellation, split NDJSON lines, and
concurrent callers. Never substitute a personal config path or real provider.

The fake-server tests also cover history serialization, capability refusal before
the create request, invalid role/order/content inputs, exact and over-limit
boundaries, API errors, and transport closure. The smoke helper seeds a small
history before the real mocked-provider turn, so the optional subprocess test
exercises the complete history-capable bridge path without contacting an
external model.
