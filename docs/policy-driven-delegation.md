# Policy-driven delegation

The native dispatcher and output guard are available in a jcode binary built with patch `0008-blueprints-and-policy-driven-dispatcher.patch`. Applying or validating the source patch does not install it. Check the binary before using these settings. The currently running binary and configured workflow have not been validated for this feature.

## Routing policy

The coordinator blueprint declares routing policy in its YAML front matter. The
shipped policy uses an ordered worker list and limits retry and assistance
grants per task:

```yaml
routing:
  workers: [routine, advanced, expert]
  budget: {retries: 2, assistance-sessions: 3}
  on-blocked:
    capability: {action: assist-next, on-unavailable: return}
    environment: {action: retry, worker: same, max-retries: 1, on-exhausted: return}
    default: {action: return}
```

A task may start with any appropriate tier, including Advanced or Expert. The
dispatcher follows an explicit tier hint when present, otherwise the configured
order. It does not require a cheapest-first pipeline. Policy and worker
definitions are snapshotted for the task.

Worker IDs match routing entries exactly and are case-sensitive. A rejected ID
reports the configured eligible IDs.

A worker's reported failure class is advisory. The dispatcher validates the
blocker and policy eligibility, then enforces scope and budgets. `capability`
can request the next eligible worker; `environment` can retry the same worker
within its retry allowance. Other classes, missing class rules, and exhausted
actions return control to the coordinator. A return does not implicitly request
verification. The coordinator decides what to do next.

## Worker observer panes

In a binary built with this patch, opt in with:

```toml
[terminal]
dispatch_worker_panes = true
```

The default is off. Supported Herdr and tmux clients open an unfocused,
read-only TUI pane for each dispatch worker and each bounded `extract` worker.
Each pane renders the worker's live assistant output, reasoning, tool intents,
and inline file diffs rather than an interactive session. The pane closes
automatically when its worker finishes, blocks, or stops; closing a pane early
does not stop the worker. Output is bounded: long events are truncated and a
slow viewer can skip events or disconnect. A retry reuses its worker pane;
bounded assistance and output-guard `extract` workers each open their own pane.
Finished and blocked workers are removed from resident server memory and live
swarm state when the turn finishes while their persisted session transcripts and
operation records remain on disk.

Execution falls back to headless mode if a pane cannot be opened safely,
including when a custom terminal spawn hook is configured. Other uses of that
hook are unchanged. After a Herdr split returns a pane ID, a failed or
interrupted launch attempts to close that pane. A split that hangs or returns
no usable ID cannot be cleaned up this way. Real multiplexer behavior and
installed binaries must be checked separately from source tests.

## Checks and retry limits

The coordinator declares authorized checks by stable ID and executable shell
command. Use the project's test runner, not a prose description. The native
check operation records actual process exits and stable invocation identities.
It counts failed declared checks toward the assignment's hard `attempt_cap`,
including expected failures, redelivery, and resume. A retry grants another
turn in the same worker session but cannot exceed the cap or reset its count.
Incidental tool failures and ordinary shell commands are not declared checks
and do not count. A worker or helper must not run undeclared checks under this
contract. Assistance receives only the check IDs the coordinator explicitly
authorizes.
Dispatched reports list check-tool runs as checks; ordinary shell observations
and their results belong in findings.

An interrupted check with no known process exit has an unknown outcome. The
dispatcher returns it for a coordinator decision instead of guessing or
rerunning it automatically. If the destructive-command gate refuses a check
before it starts, the coordinator receives the refusal and the check does not
count as an execution. Other execution errors retain their underlying reason.
The runtime limits retry and assistance counts. It does not set provider-call,
time, token, or cost quotas.

## Assistance and ownership

Assistance is a separate scoped assignment, not a promotion of the owner's
whole task. The helper receives explicit allowed paths and the minimum work
needed to resolve the named blocker. Nested assistance uses the same task-wide
assistance counter. It does not replenish the owner's retry budget or
`attempt_cap`.

The owner remains responsible for its task. A helper reports to its immediate
requester, which resumes in the same suspended session and retains ownership. A
helper cannot reassign the owner. If the owner cannot resume, the coordinator
is responsible for avoiding concurrent writes. Declared paths scope the task and
its helpers; the dispatcher does not lock those paths or sandbox filesystem
writes. jcode's file-activity notifications report observed peer touches but are
advisory and do not prevent concurrent edits.

The runtime binds report callbacks and report `identity` fields (`task_id`,
`session_id`, `turn_serial`, `blocker_id`, and `source_session_id`) to the
active turn and purpose. It retains operation state and frozen worker snapshots
in server memory, and writes private task snapshots for offline evaluator
evidence. Snapshots are never loaded as runtime state: after a server restart,
dispatch state is empty and the coordinator may need to inspect session history
before deciding whether to submit a new task.

## Native output guard and extraction

Configure the native output guard explicitly in the coordinator, Advanced, or
Expert blueprint front matter, or its `[agents.worker_blueprints.<id>]`
override:

```yaml
delegation:
  output-guard: {max-bytes: 8192}
  extraction: routine
```

Blueprint front matter and config overrides are the guard's authority. Child
permissions do not enable or configure the guard. The extraction declaration
derives the required native operation and authority; it does not require
duplicate `enabled-tools` or `allowed-child-blueprints` entries. Routine is
unguarded by default and can be used for extraction.

The guard limits final text and title or metadata by bytes on success and error
responses. Paging and `accept_large_output` do not bypass it. Images remain
excluded. The runtime saves the complete private result and returns a bounded
control notice of at most 1024 bytes. When extraction is available, the notice
points to it. If saving fails, the runtime returns a bounded failure instead of
exposing the payload or claiming it was saved.

Extraction is read-only and limited to exact named inputs. It cannot run
commands or create child workers. Descriptor-based exact-file checks have Linux
and macOS implementations. Linux has validation evidence; macOS runtime is
untested. Extraction is disabled on Windows, where verified descriptor reads
are not available.

## Deployment status

The external delegation-guard checkout plugin is retired. The native dispatcher
and output guard are built into a binary with patch
`0008-blueprints-and-policy-driven-dispatcher.patch`, need no external plugin
or setup hook, and are configured through worker blueprint front matter and
config overrides. Removing the checkout plugin does not change existing
installed copies or their config.

Before claiming deployment complete, build and install the intended patched
binary, restart it, and check the configured workflow. Native source tests do
not establish installed behavior, macOS runtime support, paid-provider
adherence, or the behavior of an existing installed copy.
