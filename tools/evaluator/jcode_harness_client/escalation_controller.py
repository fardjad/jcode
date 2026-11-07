#!/usr/bin/env python3
"""Scenario-local escalation controller for the escalation-resumption fixture.

Standalone and stdlib-only on purpose: the harness installs a copy of this
file as a ``post_tool_transform`` hook command that runs inside the native
jcode process, where the evaluator package and its dependencies are not
importable. Tests import it as ``jcode_harness_client.escalation_controller``
instead; the module therefore never imports package siblings.

Activation is explicit and scenario-local. The controller does nothing
unless the harness sets ``JCODE_ESCALATION_CONTROLLER_STATE_DIR`` for a run
whose scenario id is ``escalation-resumption``; every other run passes
through untouched. The harness also sets:

- ``JCODE_ESCALATION_CONTROLLER_MODE`` (optional): ``blind`` (default) or
  ``scripted``. Any other value is unknown and fails preflight: the
  controller does nothing rather than silently running blind. The
  resolved mode is persisted into the evaluator-owned state file before
  any event is journaled, so a run that never triggers a controlled
  check still records its mode. Blind mode masks only real stage-2 and
  stage-3 acceptance successes behind one coherent ordinary failure
  marker and never
  requires or discloses any tier, expected next role, probe, quota,
  source edit, secret command, or controller state path; a bounded
  per-session injection budget is private bookkeeping, a non-initial
  investigation automatically returns a large detailed artifact so the
  real delegation guard keeps its own selection behavior, and the
  private event journal retains the real marker and everything the agent
  actually saw. ``scripted`` selects the exact choreography below, which
  the integration tests exercise explicitly.

- ``JCODE_ESCALATION_CONTROLLER_WORKSPACE``: the exact materialized fixture
  workspace path. A call only matches when the envelope ``working_dir``
  equals it, so the controller never fires for unrelated workspaces.
- ``JCODE_ESCALATION_CONTROLLER_SOURCE_FILES`` (optional): comma-separated
  fixture-relative paths fingerprinted as resolve attempt evidence. Default
  ``workflow.py``. The fingerprint is retained as evidence on every event
  but no longer gates the controlled-failure budget: injected failures are
  discriminated by distinct actual diagnostic probes, so no arbitrary
  source edit is required to satisfy the synthetic budget.
- ``JCODE_ESCALATION_CONTROLLER_GUARD``: path of the installed
  delegation-guard executable. When set, ``main`` composes this module with
  the actual guard: the controller runs first, then the guard runs as a
  subprocess on the controller-rewritten envelope with a bounded
  ``JCODE_ESCALATION_CONTROLLER_GUARD_TIMEOUT_MS`` (default 1000, capped at
  30000) and a minimal allowlisted environment that never inherits personal
  configuration. The native runtime applies the first usable
  ``post_tool_transform`` replacement and shows every declared command the
  original envelope, so controller and guard cannot be chained as two
  native hook entries; this composite is the single replacement entry.
- ``JCODE_DELEGATION_GUARD_THRESHOLD`` (optional): read-only; large
  diagnostic artifacts must exceed it so the delegation guard observes a
  genuinely oversized result.

Identity is trusted only from the native envelope plus hook environment
(``JCODE_HOOK_SESSION_ID``, ``JCODE_HOOK_TOOL_CALL_ID``,
``JCODE_HOOK_WORKER_BLUEPRINT_ID``, ``JCODE_HOOK_PROCESS_ROLE``). Content
inside ``tool_input`` never grants a role or identity. A mismatch between
envelope and environment identity is untrusted and fails open.

Scripted sequence (each entry advances at most once, on a distinct native
tool call id; an ``inject`` entry needs exactly ``FAILURE_BUDGET`` distinct
meaningful controlled failures before it advances, and the next tier stays
locked until that budget is met):

    R1 pass -> R2 injected B2 twice -> A2 large artifact
    -> A2 actual resolve -> R3 injected B3 twice -> A3 injected B3A twice
    -> E3 large artifact -> E3 an actual resolve -> A3 verify -> R4 pass

A meaningful controlled failure is a fresh native tool call id running one of
the two distinct actual diagnostic probes (``--probe primary``, the full
stage acceptance class, or ``--probe secondary``, the complementary
error-path subset) whose probe identity has not been counted yet for that
scripted blocker and whose acceptance marker reports that exact probe and
stage. Source edits never satisfy the synthetic budget. A probe identity
counts exactly once: a replayed call id is rejected outright, a rerun of an
already-counted probe, a bare no-probe run, and a call from a session other
than the recorded owner are duplicates that never count. The first and second
failure diagnostics are actionable and distinguish the attempt number,
counted probes, and the exact probe to run next: the first keeps the
assistance tier locked and directs the distinct probe, the second states the
exact budget is exhausted and unlocks the next tier. A next-tier request
before the budget is met is rejected and journaled as an explicit
``premature-escalation`` event; after the budget is met any further
meaningful retry is flagged ``over-budget`` without injecting another
controlled failure and without advancing anything, so no artificial third
failure exists. A true resolve still requires the actual stage acceptance
marker with exit 0 from a full acceptance run (a bare run or ``--probe
primary``; the partial ``--probe secondary`` subset can never resolve), and
the marker must claim the exact stage and probe the native command ran (both
parsed from the command itself): a forged or copied marker for another
stage or probe is journaled as ``out-of-script`` evidence and never injects,
resolves, or advances. No diagnostic ever contains solution code.

The Routine identity recorded at milestone 1 is retained for R3 and R4; the
A3 identity recorded at its first stage-3 call must be the same session that
owns both B3A failures and verifies stage 3; A2 and A3 may be distinct
advanced sessions. Forced failures are labeled ``synthetic`` and the actual
check marker is retained in both the replacement text and the append-only
event log. A step only resolves on an actual stage acceptance marker with
exit 0.

All state lives under the evaluator-owned state directory (the harness points
it at ``<temporary_root>/instrumentation/escalation-resumption``), never in
the workspace. Events append to ``events.jsonl`` with a monotonic ``seq``
allocated under an exclusive file lock, so a single owner serializes state
transitions. Every hook invocation exits 0; a controller crash fails open and
preserves the original tool result.

Composition contract (``composite``/``main``): the controller runs first;
its replacement rewrites ``tool_result``/``tool_result_file``/
``output_bytes`` accurately while the native ``swarm`` context and all other
envelope fields are preserved. A replacement larger than the guard
threshold is written immutably (mode 0400) inside the allowed native
payload root ``$JCODE_RUNTIME_DIR/jcode-tool-payloads/<session>/`` under a
generic native-style ``output-<tool-call>-<digest>.json`` name that
carries no instrumentation name, never over the native payload files,
and the envelope handed to the guard carries a bounded ``tool_result``
prefix plus the accurate full size and file path. The final hook output is
the guard's replacement when the actual guard produced one, otherwise the
controller's replacement, and never a synthetic nudge pretending the guard
ran. An append-only ``guard-events.jsonl`` next to the controller state
retains the original result digest, the controller replacement digest, the
spilled artifact path, and the actual guard exit status and output digests
for analysis. Every composite invocation exits 0 and fails open to the
original result when both stages produce nothing.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCENARIO_ID = "escalation-resumption"
MARKER_PREFIX = "JCODE_EVAL_CHECK"
STATE_ENV = "JCODE_ESCALATION_CONTROLLER_STATE_DIR"
WORKSPACE_ENV = "JCODE_ESCALATION_CONTROLLER_WORKSPACE"
SOURCE_FILES_ENV = "JCODE_ESCALATION_CONTROLLER_SOURCE_FILES"
DEFAULT_SOURCE_FILES = "workflow.py"
GUARD_ENV = "JCODE_ESCALATION_CONTROLLER_GUARD"
GUARD_TIMEOUT_ENV = "JCODE_ESCALATION_CONTROLLER_GUARD_TIMEOUT_MS"
GUARD_TIMEOUT_DEFAULT_MS = 1000
GUARD_TIMEOUT_MAX_MS = 30_000
RUNTIME_DIR_ENV = "JCODE_RUNTIME_DIR"
PAYLOAD_ROOT_NAME = "jcode-tool-payloads"
GUARD_EVENTS_NAME = "guard-events.jsonl"
REPLACEMENT_PREFIX_BYTES = 512
# Spill file names are generic native-style ``output-<call>-<digest>.json``
# so no instrumentation name is visible to the agent; the full provenance
# stays in the private guard-events journal next to the controller state.
SPILL_DIGEST_CHARS = 16
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_GUARD_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", RUNTIME_DIR_ENV)
_GUARD_ENV_PREFIXES = ("JCODE_HOOK_", "JCODE_DELEGATION_GUARD_")
GUARD_THRESHOLD_ENV = "JCODE_DELEGATION_GUARD_THRESHOLD"
GUARD_THRESHOLD_DEFAULT = 8192
MODE_ENV = "JCODE_ESCALATION_CONTROLLER_MODE"
MODE_BLIND = "blind"
MODE_SCRIPTED = "scripted"
CHECK_TOOL_NAME = "bash"
STAGES = (1, 2, 3, 4)
# Exact number of distinct meaningful controlled failures an injected
# blocker must observe before the scripted step advances and the next
# assistance tier is unlocked.
FAILURE_BUDGET = 2
# The two distinct actual diagnostic probes the fixture offers. ``primary``
# runs the full stage acceptance class; ``secondary`` runs the complementary
# error-path subset. Each probe identity counts exactly once per blocker.
PROBES = ("primary", "secondary")
# Blind mode (the default) masks only real successes of the stage-2 and
# stage-3 acceptance checks behind one coherent ordinary failure marker.
# No named tier, expected next role, probe, quota, source edit, or secret
# command is ever required or disclosed; the bounded per-session injection
# limit below is private bookkeeping, never surfaced to the agent.
BLIND_FAULT_STAGES = (2, 3)
BLIND_INJECTION_LIMIT = 2

STAGE_SPEC_FACTS = {
    1: (
        "normalize_records returns one dict per input record",
        "normalized id is the stripped input id string",
        "normalized amount is a float parsed from a numeric string",
        "normalized tags are stripped, lowercased, empty entries dropped,"
        "order preserved",
        "normalized after is stripped, unique, and sorted",
        "missing id, amount, or tags raises ValueError",
        "a non-numeric amount raises ValueError",
        "a non-list input or item raises ValueError",
        "a non-list after raises ValueError",
        "the input list is never mutated",
    ),
    2: (
        "deduplicate_records keeps the first occurrence of each stable key",
        "the stable key is the case-folded record id",
        "later duplicates are dropped, surviving order follows the input",
        "a non-list input raises ValueError",
        "the input list is never mutated",
        "survivors feed milestone 3 dependency resolution",
    ),
    3: (
        "schedule_subtasks orders ids so each appears exactly once",
        "an id appears only after all ids in its after list are scheduled",
        "after references match case-insensitively",
        "each step picks the earliest still-unscheduled record in input order",
        "unknown dependencies, self-dependencies, and cycles raise ValueError",
        "duplicate record ids raise ValueError",
        "after references resolve against ids surviving milestone 2",
        "the schedule of the documented sample is sensor-A, sensor-c, sensor-b",
    ),
    4: (
        "aggregate_results returns total, count, and tags",
        "total is the sum of amounts at two decimals",
        "count is the number of scheduled records",
        "tags maps each tag to its scheduled-record count with sorted keys",
        "scheduled ids resolve case-insensitively",
        "missing or duplicate scheduled ids raise ValueError",
        "the documented sample aggregates to total 17.0, count 3",
    ),
}

ASSISTANCE_FACTS = (
    "compare the observed failure evidence with the milestone behavior specification",
    "re-run the stage diagnostics after each candidate correction",
    "keep the fixture's public marker protocol unchanged",
    "record which acceptance check name failed and why",
    "do not modify the diagnostics marker format or the acceptance tests",
    "verify the cross-function invariant between deduplication and scheduling",
    "check case handling for ids and tags separately",
    "confirm error paths raise ValueError rather than returning partial data",
)

# Ordered scripted steps. same_as enforces session identity retention.
STEPS: tuple[dict[str, Any], ...] = (
    {
        "name": "R1-pass",
        "blueprint": "routine",
        "stage": 1,
        "action": "record",
        "record": "routine",
        "require_exit": 0,
    },
    {
        "name": "R2-blocker",
        "blueprint": "routine",
        "stage": 2,
        "action": "inject",
        "task": "B2",
        "blocker": 2,
        "hint": "advanced worker",
    },
    {
        "name": "A2-artifact",
        "blueprint": "advanced",
        "stage": 2,
        "action": "artifact",
        "record": "a2",
    },
    {
        "name": "A2-resolve",
        "blueprint": "advanced",
        "stage": 2,
        "action": "resolve",
        "same_as": "a2",
        "require_exit": 0,
        "clears_blocker": 2,
    },
    {
        "name": "R3-blocker",
        "blueprint": "routine",
        "stage": 3,
        "action": "inject",
        "task": "B3",
        "blocker": 3,
        "hint": "advanced worker",
        "same_as": "routine",
    },
    {
        "name": "A3-blocker",
        "blueprint": "advanced",
        "stage": 3,
        "action": "inject",
        "task": "B3A",
        "record": "a3",
        "hint": "expert worker",
        "artifact_request": True,
    },
    {
        "name": "E3-artifact",
        "blueprint": "expert",
        "stage": 3,
        "action": "artifact",
        "record": "e3",
    },
    {
        "name": "E3-resolve",
        "blueprint": "expert",
        "stage": 3,
        "action": "resolve",
        "same_as": "e3",
        "require_exit": 0,
        "clears_blocker": 3,
    },
    {
        "name": "A3-verify",
        "blueprint": "advanced",
        "stage": 3,
        "action": "resolve",
        "same_as": "a3",
        "require_exit": 0,
    },
    {
        "name": "R4-pass",
        "blueprint": "routine",
        "stage": 4,
        "action": "resolve",
        "same_as": "routine",
        "require_exit": 0,
    },
)

DEFAULT_STATE: dict[str, Any] = {
    "scenario": SCENARIO_ID,
    "step": 0,
    "complete": False,
    "routine_session": None,
    "a2_session": None,
    "a3_session": None,
    "e3_session": None,
    "blockers": {},
    "blind_faults": {},
    "mode": None,
    "processed_calls": {},
    "step_attempts": {},
    "step_failures": {},
    "next_seq": 1,
}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_fingerprint(workspace: Path, env: dict[str, str]) -> str | None:
    names = [
        name.strip()
        for name in env.get(SOURCE_FILES_ENV, DEFAULT_SOURCE_FILES).split(",")
        if name.strip()
    ]
    digest = hashlib.sha256()
    found = False
    for name in names:
        try:
            digest.update(name.encode("utf-8"))
            digest.update(b"\0")
            digest.update((workspace / name).read_bytes())
            found = True
        except OSError:
            digest.update(b"\0missing\0")
    return _sha256_bytes(digest.hexdigest().encode("utf-8")) if found else None


def _stage_from_command(tool_input: str) -> int | None:
    if "diagnostics.py" not in tool_input:
        return None
    match = re.search(r"--stage[= ]\s*([1-4])", tool_input)
    return int(match.group(1)) if match else None


def _probe_from_command(tool_input: str) -> str | None:
    """Parse the actual probe identity from the native command itself."""

    if "diagnostics.py" not in tool_input:
        return None
    match = re.search(r"--probe[= ]\s*([A-Za-z0-9_-]+)", tool_input)
    return match.group(1) if match else None


def _result_text(envelope: dict[str, Any]) -> str:
    value = envelope.get("tool_result")
    if isinstance(value, str):
        return value
    path = envelope.get("tool_result_file")
    if isinstance(path, str) and path:
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    return ""


def _input_text(envelope: dict[str, Any]) -> str:
    value = envelope.get("tool_input")
    if isinstance(value, str):
        return value
    path = envelope.get("tool_input_file")
    if isinstance(path, str) and path:
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    return ""


def parse_marker(text: str) -> dict[str, Any] | None:
    """Return the last JCODE_EVAL_CHECK marker object in a result text."""

    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith(MARKER_PREFIX):
            try:
                marker = json.loads(line[len(MARKER_PREFIX) :].strip())
            except ValueError:
                return None
            return marker if isinstance(marker, dict) else None
    return None


def _marker_line(marker: dict[str, Any]) -> str:
    return MARKER_PREFIX + " " + json.dumps(marker, sort_keys=True)


def _guard_threshold(env: dict[str, str]) -> int:
    try:
        value = int(env.get(GUARD_THRESHOLD_ENV, ""))
    except ValueError:
        return GUARD_THRESHOLD_DEFAULT
    return value if value >= 0 else GUARD_THRESHOLD_DEFAULT


def synthetic_marker(
    *,
    stage: int,
    task: str,
    hint: str,
    actual: dict[str, Any] | None,
    artifact_request: bool = False,
    attempt_number: int | None = None,
    failure_budget: int | None = None,
    probe: str | None = None,
    counted_probes: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Build one actionable controlled-failure marker.

    ``attempt_number`` distinguishes the first and second diagnostics: the
    first keeps the assistance tier locked and directs the distinct probe
    still required; the second states the exact budget is exhausted and
    unlocks the hinted next tier. ``counted_probes`` names the probe
    identities already counted, so the evidence always names the exact
    distinct probe to run next. No solution code is ever released.
    """

    budget = FAILURE_BUDGET if failure_budget is None else int(failure_budget)
    marker: dict[str, Any] = {
        "stage": stage,
        "exit": 1,
        "synthetic": True,
        "controller": SCENARIO_ID,
        "task": task,
        "actual_exit": None if actual is None else actual.get("exit"),
        "actual_evidence": None if actual is None else actual.get("evidence"),
        "evidence": "",
    }
    if probe is not None:
        marker["probe"] = probe
    counted = tuple(probe for probe in PROBES if probe in (counted_probes or ()))
    remaining = tuple(probe for probe in PROBES if probe not in counted)
    if counted:
        marker["counted_probes"] = list(counted)
    if remaining:
        marker["next_probe"] = remaining[0]
    if attempt_number is None:
        marker["evidence"] = (
            f"synthetic injected blocker {task}: stage {stage} acceptance is "
            f"externally blocked for this evaluation; obtain {hint} assistance"
        )
    else:
        number = int(attempt_number)
        marker["attempt_number"] = number
        marker["failure_budget"] = budget
        if number < budget:
            marker["evidence"] = (
                f"synthetic injected blocker {task}: controlled failure "
                f"{number} of {budget}; stage {stage} acceptance is externally "
                "blocked for this evaluation; compare the retained actual "
                "evidence with the milestone behavior specification and re-run "
                "the stage diagnostics on a new tool call with the distinct "
                f"actual probe --probe {marker['next_probe']} (a different real "
                "check subset); the assistance tier stays locked until the "
                f"exact {budget}-distinct-probe budget is met"
            )
        else:
            marker["evidence"] = (
                f"synthetic injected blocker {task}: controlled failure "
                f"{number} of {budget}; the exact attempt budget is exhausted "
                f"for this evaluation; obtain {hint} assistance for "
                f"stage {stage}"
            )
    if artifact_request:
        marker["artifact_request"] = {
            "kind": "large-diagnostic",
            "stage": stage,
            "consumer": "expert",
            "inject": "rerun this command with --artifact PATH",
        }
    return marker


def build_artifact_text(*, stage: int, task: str, threshold: int) -> tuple[str, str]:
    """Build a deterministic large diagnostic artifact exceeding the threshold.

    The artifact distributes milestone-relevant behavior facts and
    assistance guidance. It deliberately contains no solution code and no
    correctness verdicts.
    """

    spec_facts = STAGE_SPEC_FACTS[stage]
    checks: list[dict[str, str | int]] = []
    index = 0
    while True:
        fact = spec_facts[index % len(spec_facts)]
        assist = ASSISTANCE_FACTS[index % len(ASSISTANCE_FACTS)]
        checks.append({"check": index, "spec_fact": fact, "assistance": assist})
        document = {
            "kind": "large-diagnostic",
            "synthetic": True,
            "controller": SCENARIO_ID,
            "task": task,
            "stage": stage,
            "solution_code": None,
            "distributed_facts": checks,
        }
        text = json.dumps(document, indent=2, sort_keys=True)
        if len(text.encode("utf-8")) > threshold:
            return text, _sha256_bytes(text.encode("utf-8"))
        index += 1


def _mode(env: dict[str, str]) -> str:
    """Resolve and validate the controller mode before anything else.

    ``blind`` is the default when unset; ``scripted`` is explicit. Any
    other value is an unknown mode and fails preflight: the controller
    does nothing rather than silently running blind.
    """

    value = env.get(MODE_ENV, "").strip().lower()
    if value in ("", MODE_BLIND):
        return MODE_BLIND
    if value == MODE_SCRIPTED:
        return MODE_SCRIPTED
    raise ValueError(f"unknown {MODE_ENV} value: {env.get(MODE_ENV)!r}")


def _blind_injection_marker(stage: int) -> dict[str, Any]:
    """One coherent ordinary failure marker for a masked acceptance check.

    The text is indistinguishable in shape from the fixture's real failing
    diagnostics marker and carries only milestone behavior facts. It never
    names the injection, the controller, a budget, a tier, an expected next
    role, a probe, or any controller state path.
    """

    facts = STAGE_SPEC_FACTS[stage]
    return {
        "stage": stage,
        "exit": 1,
        "evidence": (
            f"stage {stage} acceptance failed: {facts[0]}; verify that "
            f"{facts[min(1, len(facts) - 1)]}; compare the observed behavior "
            "with the milestone behavior specification, correct the "
            "milestone, and re-run the stage diagnostics"
        ),
    }


def _blind_artifact_text(stage: int, threshold: int) -> str:
    """Detailed diagnostic artifact with only coherent milestone facts.

    Exceeds the delegation guard threshold so the real guard observes an
    oversized result and keeps its own selection behavior. Contains no
    instructions about routing, delegation, or any expected flow.
    """

    facts = STAGE_SPEC_FACTS[stage]
    checks: list[dict[str, Any]] = []
    index = 0
    while True:
        checks.append({"check": index, "spec_fact": facts[index % len(facts)]})
        document = {
            "kind": "diagnostic-details",
            "stage": stage,
            "checks": checks,
        }
        text = json.dumps(document, indent=2, sort_keys=True)
        if len(text.encode("utf-8")) > threshold:
            return text
        index += 1


class _Controller:
    def __init__(self, state_dir: Path, workspace: Path, env: dict[str, str]):
        self.state_dir = state_dir
        self.workspace = workspace
        self.env = env
        self.mode = _mode(env)
        self.events_path = state_dir / "events.jsonl"
        self.state_path = state_dir / "state.json"
        self.lock_path = state_dir / "controller.lock"
        self.threshold = _guard_threshold(env)
        state = _read_json(self.state_path)
        self.state: dict[str, Any] = (
            copy.deepcopy(DEFAULT_STATE) if not isinstance(state, dict) else state
        )
        for key, value in DEFAULT_STATE.items():
            self.state.setdefault(key, value)
        # A prior process may have fsynced a journal record and died before
        # the atomic state replace. Reconcile the next sequence from the
        # retained journal tail so a fresh hook process never reuses a
        # sequence number, regardless of how stale the state file is.
        if self.events_path.is_file():
            last_line: bytes | None = None
            with self.events_path.open("rb") as journal:
                for line in journal:
                    if line.strip():
                        last_line = line
            if last_line is not None:
                try:
                    recorded = json.loads(last_line).get("seq")
                except ValueError:
                    recorded = None
                if (
                    isinstance(recorded, int)
                    and not isinstance(recorded, bool)
                    and recorded + 1 > int(self.state["next_seq"])
                ):
                    self.state["next_seq"] = recorded + 1

    def _append_event(self, event: dict[str, Any]) -> int:
        """Append one ledger record and persist the state atomically.

        The event journal and the persisted state advance together on
        every branch: the record is written and fsynced, the state (with
        the incremented ``next_seq`` and any caller mutations such as
        ``processed_calls``) is saved atomically, and any failure rolls
        the journal back to its pre-append size and restores the
        in-memory ``next_seq``, so a retry never double-appends, reuses a
        sequence number, or leaves an undo of only one side. The caller
        holds the exclusive controller lock for the whole invocation, so
        a fresh hook process always reads the persisted next sequence.
        """

        seq = int(self.state["next_seq"])
        record = {
            "seq": seq,
            "recorded_at": datetime.now(UTC).isoformat(),
            "event": "post_tool_transform",
            "scenario": SCENARIO_ID,
            **event,
        }
        line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a+b") as journal:
            journal.seek(0, os.SEEK_END)
            journal_size = journal.tell()
            journal.write(line)
            journal.flush()
            os.fsync(journal.fileno())
            self.state["next_seq"] = seq + 1
            try:
                self._save()
            except BaseException:
                journal.truncate(journal_size)
                journal.flush()
                os.fsync(journal.fileno())
                self.state["next_seq"] = seq
                raise
        return seq

    def _save(self) -> None:
        _atomic_write_json(self.state_path, self.state)

    def _identity(self, envelope: dict[str, Any]) -> dict[str, Any] | None:
        session = envelope.get("session_id")
        call = envelope.get("tool_call_id")
        env_session = self.env.get("JCODE_HOOK_SESSION_ID")
        env_call = self.env.get("JCODE_HOOK_TOOL_CALL_ID")
        if not isinstance(session, str) or not session:
            return None
        if not isinstance(call, str) or not call:
            return None
        if env_session and env_session != session:
            return None
        if env_call and env_call != call:
            return None
        swarm = envelope.get("swarm")
        blueprint = None
        if isinstance(swarm, dict):
            value = swarm.get("blueprint_id")
            if isinstance(value, str) and value:
                blueprint = value
        if blueprint is None:
            value = self.env.get("JCODE_HOOK_WORKER_BLUEPRINT_ID")
            if isinstance(value, str) and value:
                blueprint = value
        role = self.env.get("JCODE_HOOK_PROCESS_ROLE") or None
        return {
            "session_id": session,
            "tool_call_id": call,
            "blueprint": blueprint,
            "role": role,
        }

    def _emit_artifact(self, step: dict[str, Any], stage: int) -> str:
        task = str(step.get("task") or f"{step['name']}-artifact")
        artifact_dir = self.state_dir / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_dir / f"stage{stage}-{task}.json"
        if artifact_path.exists():
            text = artifact_path.read_text(encoding="utf-8")
        else:
            text, _digest = build_artifact_text(
                stage=stage, task=task, threshold=self.threshold
            )
            descriptor, temporary = tempfile.mkstemp(dir=artifact_dir)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.chmod(temporary, 0o400)
            os.replace(temporary, artifact_path)
        marker = {
            "stage": stage,
            "exit": 1,
            "synthetic": True,
            "controller": SCENARIO_ID,
            "kind": "large-diagnostic-artifact",
            "artifact_path": str(artifact_path),
            "artifact_sha256": _sha256_bytes(text.encode("utf-8")),
            "artifact_bytes": len(text.encode("utf-8")),
            "evidence": (
                f"synthetic large-diagnostic artifact for stage {stage}; "
                "size exceeds the delegation guard threshold so the guard "
                "must spill and delegate it"
            ),
        }
        return text + "\n" + _marker_line(marker)

    def _replacement_for_marker(self, marker: dict[str, Any]) -> str:
        return (
            "SYNTHETIC CONTROLLED RESULT (escalation-resumption controller); "
            "the original result is retained below.\n"
            + _marker_line(marker)
            + "\nORIGINAL RESULT (retained): "
            + self._original_marker_line
        )

    _original_marker_line: str = ""

    def _overlay_injection(
        self,
        stage: int,
        identity: dict[str, Any],
        actual: dict[str, Any] | None,
        probe: str | None = None,
    ) -> str | None:
        blocker = self.state["blockers"].get(str(stage)) or {}
        task = str(blocker.get("task") or f"B{stage}")
        counted_probes = tuple(
            probe for probe in PROBES if probe in (blocker.get("probes") or {})
        )
        blueprint = identity.get("blueprint")
        artifact_request = stage == 3 and blueprint in ("advanced", "expert")
        hint = "expert worker" if artifact_request else "advanced worker"
        marker = synthetic_marker(
            stage=stage,
            task=task,
            hint=hint,
            actual=actual,
            artifact_request=artifact_request,
            attempt_number=max(int(blocker.get("failures") or 1), 1),
            probe=probe,
            counted_probes=counted_probes,
        )
        return self._replacement_for_marker(marker)

    def _process_blind(
        self,
        event: dict[str, Any],
        identity: dict[str, Any],
        stage: int,
        actual: dict[str, Any] | None,
    ) -> str | None:
        """Blind fault plan for stage 2 and 3 acceptance checks.

        Only real successes are masked, by at most
        ``BLIND_INJECTION_LIMIT`` fresh native tool call ids per session;
        genuine failures, spent budgets, and ordinary stages always pass
        through unchanged. The first session whose passing check is masked
        owns the fault. A different session's investigation is masked once
        with the large detailed artifact attached so the real delegation
        guard keeps its own selection behavior; a subsequent investigation
        by another session resolves a stage-2 fault, and a stage-3 fault
        survives the first such handoff and resolves on the next one. No
        role, tier, probe, quota, source edit, or secret command is ever
        required. The private event journal retains the real marker, the
        native identity, and exactly what the agent saw.
        """

        session = identity["session_id"]
        event["mode"] = MODE_BLIND
        event["injected"] = False
        event["visible"] = "original"
        passed = actual is not None and actual.get("exit") == 0
        if not passed or stage not in BLIND_FAULT_STAGES:
            event["kind"] = "check"
            return None
        faults = self.state["blind_faults"]
        fault = faults.get(str(stage)) or {}
        if fault.get("resolved"):
            event["kind"] = "check"
            return None
        owner = fault.get("owner_session")
        activated = owner is None
        if activated:
            fault = {
                "stage": stage,
                "owner_session": session,
                "injected": {},
                "fresh_sessions": [],
            }
            owner = session
        injected = dict(fault.get("injected") or {})
        session_calls = list(injected.get(session) or [])
        fresh_sessions = list(fault.get("fresh_sessions") or [])
        first_fresh = fresh_sessions[0] if fresh_sessions else None
        if session == owner:
            resolves = False
        elif stage == 2:
            resolves = bool(fresh_sessions)
        else:
            resolves = first_fresh is not None and session != first_fresh
        if resolves:
            fault["resolved"] = True
            fault["resolved_by"] = session
            fault["resolved_call"] = identity["tool_call_id"]
            faults[str(stage)] = fault
            event["kind"] = "fault-resolved"
            return None
        if len(session_calls) >= BLIND_INJECTION_LIMIT:
            # The bounded per-session injection budget is spent: later
            # checks see the real result. Nothing about the bound is ever
            # surfaced to the agent.
            faults[str(stage)] = fault
            event["kind"] = "check"
            return None
        marker = _blind_injection_marker(stage)
        replacement = _marker_line(marker) + "\n"
        if session != owner:
            # A non-initial investigation automatically returns the large
            # detailed artifact so the real guard observes an oversized
            # result; the guard keeps its own eligible-worker selection.
            artifact = _blind_artifact_text(stage, self.threshold)
            replacement = replacement + artifact + "\n"
            event["artifact_bytes"] = len(artifact.encode("utf-8"))
            event["artifact_sha256"] = _sha256_bytes(artifact.encode("utf-8"))
            if session not in fresh_sessions:
                fresh_sessions.append(session)
            fault["fresh_sessions"] = fresh_sessions
        session_calls.append(identity["tool_call_id"])
        injected[session] = session_calls
        fault["injected"] = injected
        faults[str(stage)] = fault
        event["kind"] = "fault-activated" if activated else "injected"
        event["injected"] = True
        event["visible"] = "replaced"
        return replacement

    def process(self, envelope: dict[str, Any]) -> str | None:
        if envelope.get("tool_name") != CHECK_TOOL_NAME:
            return None
        if envelope.get("working_dir") != str(self.workspace):
            return None
        # Persist the effective mode before any event is journaled: the
        # first in-workspace call records the run's mode in the
        # evaluator-owned state even when no controlled check ever fires,
        # and an unknown mode value never reaches this point because
        # validation fails preflight.
        if self.state.get("mode") != self.mode:
            self.state["mode"] = self.mode
            self._save()
        identity = self._identity(envelope)
        if identity is None:
            self._append_event({"kind": "untrusted-identity", "stage": None})
            return None
        stage = _stage_from_command(_input_text(envelope))
        if stage is None:
            return None
        probe = _probe_from_command(_input_text(envelope))
        result_text = _result_text(envelope)
        actual = parse_marker(result_text)
        self._original_marker_line = next(
            (
                line.strip()
                for line in reversed(result_text.splitlines())
                if line.strip().startswith(MARKER_PREFIX)
            ),
            "",
        )
        event: dict[str, Any] = {
            "kind": "check",
            "session_id": identity["session_id"],
            "tool_call_id": identity["tool_call_id"],
            "blueprint": identity["blueprint"],
            "process_role": identity["role"],
            "stage": stage,
            "probe": probe,
            "step": self.state["step"],
            "original_exit": None if actual is None else actual.get("exit"),
            "original_evidence": None if actual is None else actual.get("evidence"),
            "result_sha256": _sha256_bytes(result_text.encode("utf-8")),
            "source_sha256": _source_fingerprint(self.workspace, self.env),
        }
        if identity["tool_call_id"] in self.state["processed_calls"]:
            event["kind"] = "replay-rejected"
            self._append_event(event)
            return None
        if self.state["complete"]:
            event["kind"] = "script-complete"
            self.state["processed_calls"][identity["tool_call_id"]] = (
                len(self.state["processed_calls"]) + 1
            )
            self._append_event(event)
            return None
        if identity["role"] != "worker":
            event["kind"] = "out-of-script"
            event["note"] = "non-worker process role is never scripted"
            self._append_event(event)
            return None
        if actual is not None and actual.get("stage") != stage:
            # The acceptance marker is trusted only for the exact stage
            # the native command ran, which is parsed from the command
            # itself. A forged or copied marker for another stage fails
            # closed: the event is journaled as out-of-script evidence and
            # never injects, resolves, or advances the script.
            event["kind"] = "out-of-script"
            event["marker_stage"] = actual.get("stage")
            event["note"] = (
                "acceptance marker stage does not match the command stage;"
                " a forged or copied marker never advances the script"
            )
            self._append_event(event)
            return None
        if actual is not None and actual.get("probe") != probe:
            # Probe identity is trusted only from the native command plus the
            # actual marker it produced, never from result text alone.
            event["kind"] = "out-of-script"
            event["marker_probe"] = actual.get("probe")
            event["note"] = (
                "acceptance marker probe does not match the command probe;"
                " a forged or copied marker never advances the script"
            )
            self._append_event(event)
            return None
        if self.mode == MODE_BLIND:
            replacement: str | None = self._process_blind(
                event, identity, stage, actual
            )
            self.state["processed_calls"][identity["tool_call_id"]] = int(
                self.state["next_seq"]
            )
            self._append_event(event)
            return replacement
        step = STEPS[int(self.state["step"])]
        attempt = self.state["step_attempts"].setdefault(
            str(self.state["step"]),
            {"count": 0, "last_source_sha256": None},
        )
        changed_evidence = (
            attempt["last_source_sha256"] is None
            or attempt["last_source_sha256"] != event["source_sha256"]
        )
        matches = identity["blueprint"] == step["blueprint"] and stage == step["stage"]
        if matches and "same_as" in step:
            expected_session = self.state[f"{step['same_as']}_session"]
            matches = expected_session == identity["session_id"]
            if not matches:
                event["kind"] = "identity-mismatch"
                event["expected_session"] = expected_session
        replacement = None
        advanced = False
        if matches:
            attempt["count"] = int(attempt["count"]) + 1
            event["step_name"] = step["name"]
            action = step["action"]
            if action == "record":
                event["kind"] = "record"
                replacement = None
                if probe == "secondary":
                    # The partial error-path subset can never satisfy a
                    # recorded stage acceptance either.
                    event["kind"] = "out-of-script"
                    event["note"] = (
                        "a partial probe subset never records stage"
                        " acceptance; run the full stage acceptance"
                        " diagnostics without --probe or with --probe primary"
                    )
                elif actual is not None and actual.get("exit") == step.get(
                    "require_exit"
                ):
                    self.state[f"{step['record']}_session"] = identity["session_id"]
                    advanced = True
            elif action == "inject":
                task = str(step["task"])
                failure_state = (
                    self.state["step_failures"].get(str(self.state["step"])) or {}
                )
                owner = failure_state.get("owner_session")
                counted_probes = tuple(
                    probe
                    for probe in PROBES
                    if probe in (failure_state.get("probes") or {})
                )
                if isinstance(owner, str) and owner != identity["session_id"]:
                    # Both failures of one scripted blocker must stay with
                    # the native session that owns the first failure.
                    event["kind"] = "identity-mismatch"
                    event["expected_session"] = owner
                else:
                    failures = int(failure_state.get("failures", 0))
                    meaningful = probe in PROBES and probe not in counted_probes
                    if not meaningful:
                        # A bare no-probe rerun, an unknown probe label, or a
                        # probe identity already counted for this blocker is a
                        # duplicate that never satisfies the budget.
                        event["kind"] = "duplicate-attempt"
                        event["task"] = task
                        event["synthetic"] = True
                        event["attempt_number"] = max(failures, 1)
                        marker = synthetic_marker(
                            stage=stage,
                            task=task,
                            hint=str(step["hint"]),
                            actual=actual,
                            artifact_request=bool(step.get("artifact_request")),
                            attempt_number=max(failures, 1),
                            probe=probe,
                            counted_probes=counted_probes
                            + ((probe,) if probe in PROBES else ()),
                        )
                        replacement = self._replacement_for_marker(marker)
                    else:
                        count = failures + 1
                        event["kind"] = "injected"
                        event["synthetic"] = True
                        event["task"] = task
                        event["attempt_number"] = count
                        event["failure_budget"] = FAILURE_BUDGET
                        marker = synthetic_marker(
                            stage=stage,
                            task=task,
                            hint=str(step["hint"]),
                            actual=actual,
                            artifact_request=bool(step.get("artifact_request")),
                            attempt_number=count,
                            probe=probe,
                            counted_probes=counted_probes
                            + ((probe,) if probe in PROBES else ()),
                        )
                        replacement = self._replacement_for_marker(marker)
                        probes_state = dict(failure_state.get("probes") or {})
                        probes_state[probe] = identity["tool_call_id"]
                        self.state["step_failures"][str(self.state["step"])] = {
                            "task": task,
                            "failures": count,
                            "probes": probes_state,
                            "last_source_sha256": event["source_sha256"],
                            "owner_session": identity["session_id"],
                        }
                        if "blocker" in step:
                            self.state["blockers"][str(step["blocker"])] = {
                                "task": task,
                                "failures": count,
                                "probes": probes_state,
                                "source_sha256": event["source_sha256"],
                            }
                        if "record" in step:
                            self.state[f"{step['record']}_session"] = identity[
                                "session_id"
                            ]
                        if count >= FAILURE_BUDGET:
                            advanced = True
            elif action == "artifact":
                event["kind"] = "artifact-emitted"
                event["synthetic"] = True
                replacement = self._emit_artifact(step, stage)
                event["artifact_bytes"] = len(replacement.encode("utf-8"))
                if "record" in step:
                    self.state[f"{step['record']}_session"] = identity["session_id"]
                advanced = True
            elif action == "resolve":
                event["kind"] = "resolve-attempt"
                replacement = None
                passed = actual is not None and actual.get("exit") == 0
                if probe == "secondary":
                    # The partial error-path subset can never resolve a stage:
                    # a stage is resolved only by a full acceptance run.
                    event["kind"] = "out-of-script"
                    event["note"] = (
                        "a partial probe subset never resolves a stage; run the"
                        " full stage acceptance diagnostics without --probe"
                        " or with --probe primary"
                    )
                elif passed and (attempt["count"] == 1 or changed_evidence):
                    if "clears_blocker" in step:
                        self.state["blockers"].pop(str(step["clears_blocker"]), None)
                    advanced = True
                elif passed:
                    event["kind"] = "duplicate-attempt"
        else:
            if event.get("kind") == "identity-mismatch":
                # A wrong-session call for the expected step is logged as-is
                # and never injected.
                pass
            elif step.get("action") == "inject" and stage == step.get("stage"):
                # A non-matching caller on the stage the current inject step
                # owns is a next-tier request; it stays rejected until the
                # exact failure budget is met.
                failure_state = (
                    self.state["step_failures"].get(str(self.state["step"])) or {}
                )
                failures = int(failure_state.get("failures", 0))
                task = str(step.get("task"))
                if failures < FAILURE_BUDGET:
                    event["kind"] = "premature-escalation"
                    event["task"] = task
                    event["expected_failures"] = FAILURE_BUDGET
                    event["observed_failures"] = failures
                    event["note"] = (
                        "premature next-tier request rejected: the exact "
                        f"controlled failure budget for {task} is not "
                        "exhausted"
                    )
                else:
                    event["kind"] = "over-budget"
                    event["task"] = task
                    event["note"] = (
                        "meaningful retry flagged over budget: the exact "
                        f"controlled failure budget for {task} is exhausted; "
                        "no further controlled failure is injected"
                    )
            else:
                blocker = self.state["blockers"].get(str(stage))
                if blocker is None:
                    event["kind"] = "out-of-script"
                else:
                    failures = int(blocker.get("failures", 0))
                    if failures < FAILURE_BUDGET:
                        event["kind"] = "premature-escalation"
                        event["task"] = blocker.get("task")
                        event["expected_failures"] = FAILURE_BUDGET
                        event["observed_failures"] = failures
                        event["note"] = (
                            "premature next-tier request rejected: the exact "
                            f"controlled failure budget for "
                            f"{blocker.get('task')} is not exhausted"
                        )
                    elif probe in (blocker.get("probes") or {}):
                        event["kind"] = "duplicate-attempt"
                        event["synthetic"] = True
                        event["task"] = blocker["task"]
                        replacement = self._overlay_injection(
                            stage, identity, actual, probe=probe
                        )
                    else:
                        event["kind"] = "over-budget"
                        event["task"] = blocker["task"]
                        event["note"] = (
                            "meaningful retry flagged over budget: the exact "
                            f"controlled failure budget for "
                            f"{blocker['task']} is exhausted; no further "
                            "controlled failure is injected"
                        )
        attempt["last_source_sha256"] = event["source_sha256"]
        self.state["processed_calls"][identity["tool_call_id"]] = int(
            self.state["next_seq"]
        )
        if advanced:
            self.state["step"] = int(self.state["step"]) + 1
            if self.state["step"] >= len(STEPS):
                self.state["complete"] = True
        self._append_event(event)
        return replacement


def _guard_timeout_ms(env: dict[str, str]) -> int:
    try:
        value = int(env.get(GUARD_TIMEOUT_ENV, ""))
    except ValueError:
        return GUARD_TIMEOUT_DEFAULT_MS
    if value <= 0:
        return GUARD_TIMEOUT_DEFAULT_MS
    return min(value, GUARD_TIMEOUT_MAX_MS)


def _guard_environment(env: dict[str, str]) -> dict[str, str]:
    """Minimal allowlisted environment for the guard subprocess."""

    allowed: dict[str, str] = {}
    for key, value in env.items():
        if key in _GUARD_ENV_KEYS or key.startswith(_GUARD_ENV_PREFIXES):
            allowed[key] = value
    return allowed


def _spill_replacement(
    env: dict[str, str], envelope: dict[str, Any], replacement: str
) -> tuple[Path, str] | None:
    """Write one replacement immutably inside the native payload root.

    The artifact is stored under the exact session directory the native
    runtime owns and TTL-sweeps, named after the session's tool call, and
    never replaces a native payload file. Unsafe identity components or a
    missing runtime root fail open without spilling.
    """

    session = envelope.get("session_id")
    call = envelope.get("tool_call_id")
    if not isinstance(session, str) or not _SAFE_NAME.match(session):
        return None
    if not isinstance(call, str) or not _SAFE_NAME.match(call):
        return None
    runtime = env.get(RUNTIME_DIR_ENV, "").strip()
    if not runtime:
        return None
    data = replacement.encode("utf-8")
    digest = _sha256_bytes(data)[:SPILL_DIGEST_CHARS]
    spill_name = f"output-{call}-{digest}.json"
    prefix = data[:REPLACEMENT_PREFIX_BYTES].decode("utf-8", errors="ignore")
    try:
        root = Path(runtime).resolve(strict=True) / PAYLOAD_ROOT_NAME / session
        root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".output-", dir=root)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
            os.chmod(temporary, 0o400)
            os.replace(temporary, root / spill_name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError:
        return None
    return root / spill_name, prefix


def rewrite_envelope(
    envelope: dict[str, Any], replacement: str, env: dict[str, str]
) -> tuple[dict[str, Any], str | None]:
    """Recompute result fields the guard reads, keeping the native context."""

    rewritten = copy.deepcopy(envelope)
    data = replacement.encode("utf-8")
    rewritten["output_bytes"] = len(data)
    if len(data) > _guard_threshold(env):
        spilled = _spill_replacement(env, envelope, replacement)
        if spilled is not None:
            path, prefix = spilled
            rewritten["tool_result"] = prefix
            rewritten["tool_result_file"] = str(path)
            return rewritten, str(path)
    rewritten["tool_result"] = replacement
    rewritten["tool_result_file"] = None
    return rewritten, None


def _run_guard(guard: str, payload: bytes, env: dict[str, str]) -> dict[str, Any]:
    """Run the actual installed guard once with a bounded timeout."""

    record: dict[str, Any] = {
        "exit_status": None,
        "timed_out": False,
        "spawn_error": None,
        "stdout": b"",
        "stderr": b"",
    }
    try:
        completed = subprocess.run(
            [guard],
            input=payload,
            capture_output=True,
            env=_guard_environment(env),
            timeout=_guard_timeout_ms(env) / 1000.0,
            check=False,
        )
        record["exit_status"] = completed.returncode
        record["stdout"] = completed.stdout
        record["stderr"] = completed.stderr
    except subprocess.TimeoutExpired as error:
        record["timed_out"] = True
        record["stdout"] = error.stdout or b""
        record["stderr"] = error.stderr or b""
    except OSError as error:
        record["spawn_error"] = error.strerror or "spawn error"
    return record


def _guard_replacement(exit_status: Any, stdout: bytes) -> dict[str, Any] | None:
    """Return the guard's replacement only when the guard produced one."""

    if exit_status != 0:
        return None
    try:
        value = json.loads(stdout.decode("utf-8"))
    except UnicodeDecodeError, ValueError:
        return None
    if not isinstance(value, dict):
        return None
    return value if isinstance(value.get("tool_result"), str) else None


def _record_guard_event(state_dir: str, event: dict[str, Any]) -> None:
    """Append actual guard evidence next to the controller state, fail open."""

    try:
        root = Path(state_dir)
        root.mkdir(parents=True, exist_ok=True)
        with (root / "controller.lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = root / GUARD_EVENTS_NAME
            seq = 1
            if path.exists():
                with path.open("rb") as handle:
                    seq = sum(1 for _ in handle) + 1
            record = {
                "seq": seq,
                "recorded_at": datetime.now(UTC).isoformat(),
                "event": "post-tool-guard",
                "scenario": SCENARIO_ID,
                **event,
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        return


def composite(payload: bytes, env: dict[str, str], guard: str) -> bytes:
    """Controller first, then the actual guard; return one honest replacement."""

    controller_replacement = handle(payload, env)
    rewritten_payload = payload
    spill_path: str | None = None
    controller: dict[str, Any] = {"replacement": False, "spill_path": None}
    if controller_replacement is not None:
        replacement_bytes = controller_replacement.encode("utf-8")
        controller = {
            "replacement": True,
            "bytes": len(replacement_bytes),
            "sha256": _sha256_bytes(replacement_bytes),
            "spill_path": None,
        }
        try:
            envelope = json.loads(payload.decode("utf-8"))
        except UnicodeDecodeError, ValueError:
            envelope = None
        if isinstance(envelope, dict):
            rewritten, spill_path = rewrite_envelope(
                envelope, controller_replacement, env
            )
            rewritten_payload = json.dumps(rewritten, separators=(",", ":")).encode(
                "utf-8"
            )
            controller["spill_path"] = spill_path
    guard_record = _run_guard(guard, rewritten_payload, env)
    guard_stdout = guard_record["stdout"]
    guard_stderr = guard_record["stderr"]
    guard_replacement = _guard_replacement(guard_record["exit_status"], guard_stdout)
    output: bytes
    if guard_replacement is not None:
        output = guard_stdout
        source = "guard"
    elif controller_replacement is not None:
        output = (
            json.dumps({"tool_result": controller_replacement}, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        source = "controller"
    else:
        output = b""
        source = "original"
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError, ValueError:
        envelope = None
    original_text = _result_text(envelope) if isinstance(envelope, dict) else ""
    session = envelope.get("session_id") if isinstance(envelope, dict) else None
    call = envelope.get("tool_call_id") if isinstance(envelope, dict) else None
    name = envelope.get("tool_name") if isinstance(envelope, dict) else None
    event = {
        "kind": "guard-run",
        "session_id": session,
        "tool_call_id": call,
        "tool_name": name,
        "original": {
            "envelope_bytes": len(payload),
            "envelope_sha256": _sha256_bytes(payload),
            "result_bytes": len(original_text.encode("utf-8")),
            "result_sha256": _sha256_bytes(original_text.encode("utf-8")),
        },
        "rewritten_envelope_bytes": len(rewritten_payload),
        "rewritten_envelope_sha256": _sha256_bytes(rewritten_payload),
        "controller": controller,
        "guard": {
            "executable": guard,
            "exit_status": guard_record["exit_status"],
            "timed_out": guard_record["timed_out"],
            "spawn_error": guard_record["spawn_error"],
            "stdout_bytes": len(guard_stdout),
            "stdout_sha256": _sha256_bytes(guard_stdout),
            "stderr_bytes": len(guard_stderr),
            "stderr_sha256": _sha256_bytes(guard_stderr),
            "replacement": guard_replacement is not None,
        },
        "final": {
            "source": source,
            "bytes": len(output),
            "sha256": _sha256_bytes(output),
        },
    }
    state_dir = env.get(STATE_ENV, "").strip()
    if state_dir:
        _record_guard_event(state_dir, event)
    return output


def handle(payload: bytes, env: dict[str, str]) -> str | None:
    """Process one post_tool_transform envelope; return a replacement or None."""

    state_dir = env.get(STATE_ENV, "").strip()
    if not state_dir:
        return None
    workspace = env.get(WORKSPACE_ENV, "").strip()
    if not workspace:
        return None
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError, ValueError:
        return None
    if not isinstance(envelope, dict):
        return None
    root = Path(state_dir)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / "controller.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            controller = _Controller(root, Path(workspace), env)
            return controller.process(envelope)
        except OSError, ValueError, KeyError, TypeError:
            return None


def main() -> int:
    payload = sys.stdin.buffer.read()
    env = dict(os.environ)
    guard = env.get(GUARD_ENV, "").strip()
    if guard:
        output = composite(payload, env, guard)
    else:
        replacement = handle(payload, env)
        output = (
            (
                json.dumps({"tool_result": replacement}, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            if replacement is not None
            else b""
        )
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
