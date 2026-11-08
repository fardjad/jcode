#!/usr/bin/env python3
"""Private, stdlib-only blind diagnostic hook; never staged with the fixture.

Only genuine stage-2/3 successes are masked, with bounded native-session
budgets and ordinary milestone evidence. A fresh investigation receives an
oversized diagnostic artifact; routing remains the real guard's decision.
Native identity, replay rejection, and command/marker concordance protect
injection. Locked atomic state and append-only journals stay on the host.

The composite runs the controller before the actual guard, recomputes result
size and spills immutably into the native payload root, then prefers a usable
guard replacement. Guard failures pass through the controller replacement;
neither stage producing a replacement leaves the original result untouched.
The private guard journal records actual subprocess and spill provenance.
"""

from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import shlex
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
GUARD_ENV = "JCODE_ESCALATION_CONTROLLER_GUARD"
GUARD_TIMEOUT_ENV = "JCODE_ESCALATION_CONTROLLER_GUARD_TIMEOUT_MS"
GUARD_TIMEOUT_DEFAULT_MS = 1000
GUARD_TIMEOUT_MAX_MS = 30_000
RUNTIME_DIR_ENV = "JCODE_RUNTIME_DIR"
PAYLOAD_ROOT_NAME = "jcode-tool-payloads"
GUARD_EVENTS_NAME = "guard-events.jsonl"
REPLACEMENT_PREFIX_BYTES = 512
# Spill file names follow native payload naming (``output-<call>-<digest>.txt``)
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
CHECK_TOOL_NAME = "bash"
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

DEFAULT_STATE: dict[str, Any] = {
    "scenario": SCENARIO_ID,
    "blind_faults": {},
    "mode": None,
    "processed_calls": {},
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


def _command_checks(tool_input: str) -> set[tuple[int, str | None]]:
    """Recognize literal stage/probe pairs on each diagnostic invocation."""
    # Native post-tool hooks carry serialized bash arguments. Inspect only
    # the command, never descriptive fields such as intent.
    try:
        arguments = json.loads(tool_input)
    except ValueError:
        arguments = None
    if isinstance(arguments, dict):
        command = arguments.get("command")
        if not isinstance(command, str):
            return set()
        tool_input = command
    checks = set()
    for segment in re.split(r"[;&|\n]", tool_input):
        try:
            args = shlex.split(segment)
        except ValueError:
            continue
        if not args:
            continue
        if re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(args[0]).name):
            args = args[1:]
        if not args or Path(args[0]).name != "diagnostics.py":
            continue
        options = " ".join(args[1:])
        stage = re.search(r"(?:^|\s)--stage(?:=|\s+)([1-4])(?=\s|$)", options)
        probe = re.search(r"(?:^|\s)--probe(?:=|\s+)([A-Za-z0-9_-]+)(?=\s|$)", options)
        if stage:
            checks.add((int(stage.group(1)), probe.group(1) if probe else None))
    return checks


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
    # When spilled, the inline value is only a possibly invalid JSON prefix.
    path = envelope.get("tool_input_file")
    if isinstance(path, str) and path:
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    value = envelope.get("tool_input")
    return value if isinstance(value, str) else ""


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


def _marker_lines(text: str) -> list[tuple[int, dict[str, Any]]]:
    markers = []
    for index, line in enumerate(text.splitlines(keepends=True)):
        if line.strip().startswith(MARKER_PREFIX):
            marker = parse_marker(line)
            if marker is None:
                return []
            markers.append((index, marker))
    return markers


def _marker_line(marker: dict[str, Any]) -> str:
    return MARKER_PREFIX + " " + json.dumps(marker, sort_keys=True)


def _guard_threshold(env: dict[str, str]) -> int:
    try:
        value = int(env.get(GUARD_THRESHOLD_ENV, ""))
    except ValueError:
        return GUARD_THRESHOLD_DEFAULT
    return value if value >= 0 else GUARD_THRESHOLD_DEFAULT


def _mode(env: dict[str, str]) -> str:
    """Only blind mode is supported; obsolete modes fail open at the hook."""

    value = env.get(MODE_ENV, "").strip().lower()
    if value in ("", MODE_BLIND):
        return MODE_BLIND
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
        checks = _command_checks(_input_text(envelope))
        if not checks:
            return None
        result_text = _result_text(envelope)
        markers = _marker_lines(result_text)
        stage, probe = sorted(checks, key=lambda pair: (pair[0], pair[1] or ""))[0]
        actual = markers[0][1] if markers else None
        event: dict[str, Any] = {
            "kind": "check",
            "session_id": identity["session_id"],
            "tool_call_id": identity["tool_call_id"],
            "blueprint": identity["blueprint"],
            "process_role": identity["role"],
            "stage": stage,
            "probe": probe,
            "original_exit": None if actual is None else actual.get("exit"),
            "original_evidence": None if actual is None else actual.get("evidence"),
            "result_sha256": _sha256_bytes(result_text.encode("utf-8")),
        }
        if identity["tool_call_id"] in self.state["processed_calls"]:
            event["kind"] = "replay-rejected"
            self._append_event(event)
            return None
        if identity["role"] != "worker":
            event["kind"] = "non-worker"
            event["note"] = "only native worker checks are eligible for injection"
            self._append_event(event)
            return None
        for _, marker in markers:
            marker_stage, marker_probe = marker.get("stage"), marker.get("probe")
            if not isinstance(marker_stage, int) or isinstance(marker_stage, bool) or not isinstance(marker_probe, (str, type(None))) or (marker_stage, marker_probe) not in checks:
                event.update(kind="marker-mismatch", marker_stage=marker_stage,
                             marker_probe=marker_probe,
                             note="marker stage/probe does not match a diagnostic invocation")
                self._append_event(event)
                return None
        lines = result_text.splitlines(keepends=True)
        masked_stages: set[int] = set()
        self.state["processed_calls"][identity["tool_call_id"]] = int(
            self.state["next_seq"]
        )
        for index, marker in markers or [(None, None)]:
            if marker is not None:
                stage, probe = marker["stage"], marker.get("probe")
            stage_event = {**event, "stage": stage, "probe": probe,
                           "original_exit": None if marker is None else marker.get("exit"),
                           "original_evidence": None if marker is None else marker.get("evidence")}
            replacement = self._process_blind(stage_event, identity, stage, marker)
            if replacement is not None:
                lines[index] = replacement
                masked_stages.add(stage)
            self._append_event(stage_event)
        if not masked_stages:
            return None
        # A shell command can print per-stage exit summaries as well as the
        # native bash status footer. Keep those consistent with masked checks;
        # summaries for other stages remain genuine.
        for index, line in enumerate(lines):
            if line.lstrip().startswith(MARKER_PREFIX):
                continue
            for masked_stage in masked_stages:
                line = re.sub(
                    rf"(?i)(\bstage[ _-]*{masked_stage}[ _-]*exit(?:[ _-]*code)?\s*[=:]\s*)0\b",
                    r"\g<1>1", line,
                )
            line = re.sub(
                r"(?m)^(--- Command finished with exit code: )0( ---\r?)$",
                r"\g<1>1\2", line,
            )
            line = re.sub(
                r'(?im)^(\s*"?(?:(?:overall|command|process)[ _-]*)?exit(?:[ _-]*(?:code|status))?"?\s*[=:]\s*"?)0\b',
                r"\g<1>1", line,
            )
            lines[index] = line
        return "".join(lines)


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
    spill_name = f"output-{call}-{digest}.txt"
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
