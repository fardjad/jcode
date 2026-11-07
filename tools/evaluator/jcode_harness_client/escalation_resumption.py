"""Observed escalation/resumption facts for the escalation-resumption scenario.

This metric is not a correctness gate: independent acceptance remains the
only correctness gate. The controller mode is resolved from the private
evidence, preferring the mode the controller persists into its
evaluator-owned state before any event: a zero-event journal is blind
coverage, never a forced scripted verdict. Blind journals also carry
``mode: blind`` on their events; the older scripted journals predate both
the state field and the event field, so a journal with events and no
recorded mode anywhere stays scripted (historical explicit-mode
evidence).

In blind mode the analysis is purely observational and prescribes no route:
any reasonable tier delegation or finishing without escalation is acceptable,
unexercised behavior coverage is reported without judgment, and missing
evidence stays unknown, never clean and never a failure. The fault timeline
and masking facts are controller-private journal evidence for the judge;
whether any native caller retained access to them is reported as observed or
uncertain, never claimed as verified blindness. Native worker names and models
come only from native
session evidence joined by session id, never from claimed roles in prose.

In scripted mode the analyzer reports three distinct dimensions from retained
evidence:

- ``orchestration``: the scripted controller stage sequence, the exact
  two-distinct-probe budget per injected blocker (each counted failure is a
  fresh native tool call running one of the two distinct actual diagnostic
  probes, so source edits are never required to satisfy the synthetic
  budget; the first injection and any probe-distinct reinjection both
  count, while an already-counted probe, a no-probe run, duplicates, and
  replays never do; only failures from the authoritative first-stage owner
  session count toward the budget, and the two counting failures must use
  two distinct probe identities on one native session that matches the
  expected stage owner; a foreign-owner failure never counts and stays an
  ownership violation, a missing second failure is incomplete, and a
  premature next-tier request or a third meaningful retry is a violation),
  same-session resumption identities, native
  coordinator resume calls grounding each scripted handback (exact
  original session id, nonempty task, helper completion before the call,
  and new worker turns after it), observed replays, out-of-script calls,
  and failed attempts with their native citations.
- ``context_shielding``: whether each large diagnostic artifact actually
  exceeded the guard threshold, was spilled by the controller, replaced by
  the real delegation guard, and never exposed raw in any model-visible
  native transcript text.
- ``evidence_unknown``: bounded extraction children per guard event, the
  required artifact facts present in each child report (not merely marker
  lines), role grounding of controller records against native session
  evidence, and the limits that retained evidence cannot verify.

Evidence locations (retained by the runner into the safe raw root before
temporary-root cleanup): controller state/events/artifacts under
``<raw>/other/`` (from the instrumentation tree) and spilled replacements
under ``<raw>/hook-payloads/<session>/``. Worker names, models, and parents
are resolved only from native session evidence (snapshots, listings,
messages), never from claimed roles in report prose. Unknown or missing
evidence leaves the report incomplete, never clean.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from .escalation_controller import (
    FAILURE_BUDGET,
    MODE_BLIND,
    MODE_SCRIPTED,
    PROBES,
    SCENARIO_ID,
    STAGES,
    STEPS,
    _blind_artifact_text,
)

_JSON = dict[str, Any]
_ADVANCING_ACTIONS = {
    "record": "record",
    "injected": "inject",
    "artifact-emitted": "artifact",
    "resolve-attempt": "resolve",
}
# Controller event kinds that carry one controlled failure for a scripted
# blocker. The first injection and any meaningful reinjection both count;
# duplicate-attempt and replay-rejected events never do.
_FAILURE_KINDS = {"injected", "blocker-reinjected", "over-budget"}
_FAILED_KINDS = {"record", "resolve-attempt"}
_REJECTED_KINDS = {
    "out-of-script",
    "identity-mismatch",
    "untrusted-identity",
    "premature-escalation",
    "over-budget",
}
_EXPOSURE_PROBE_BYTES = 512
_CITATION = f"scenario:{SCENARIO_ID}"
_BLIND_TIMELINE_KINDS = {"fault-activated", "injected", "fault-resolved"}
# The guard nudge hands the caller one exact saved result file path; the
# extraction child's assignment must name that exact path.
_SAVED_PAYLOAD_PATH = re.compile(r"Full output is available at: (\S+)")
# A native caller reading the controller's private journal or state files
# is observed private access, never assumed away.
_PRIVATE_EVIDENCE_PATH = re.compile(
    r"(?:instrumentation|escalation-resumption)[^\n]*"
    r"(?:events\.jsonl|guard-events\.jsonl|state\.json)"
)


def _read_jsonl(path: Path) -> tuple[list[_JSON], list[str]]:
    entries: list[_JSON] = []
    problems: list[str] = []
    if not path.is_file():
        return entries, [f"controller evidence file is missing: {path.name}"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        return entries, [f"unreadable controller evidence {path.name}: {error}"]
    for number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            problems.append(f"invalid JSON in {path.name} line {number}")
            continue
        if isinstance(value, dict):
            entries.append(value)
        else:
            problems.append(f"non-object entry in {path.name} line {number}")
    return entries, problems


def _native_sessions(records: list[_JSON]) -> dict[str, _JSON]:
    """Join native worker names, models, and parents by session id."""

    sessions: dict[str, _JSON] = {}
    for record in records:
        sid = record.get("session_id")
        if not isinstance(sid, str):
            continue
        data = record.get("data")
        if not isinstance(data, dict):
            continue
        entry = sessions.setdefault(sid, {"session_id": sid})
        worker = record.get("worker")
        if isinstance(worker, str) and worker:
            entry["worker"] = worker
        policy = data.get("worker_blueprint_policy")
        if isinstance(policy, dict) and isinstance(policy.get("name"), str):
            entry.setdefault("worker", str(policy["name"]))
        if isinstance(data.get("model"), str):
            entry["model"] = str(data["model"])
        parent = (
            record.get("parent_session_id")
            or data.get("parent_session_id")
            or data.get("parent_id")
        )
        if isinstance(parent, str):
            entry["parent_session_id"] = parent
    return sessions


def _scan_messages(records: list[_JSON]) -> _JSON:
    """Collect native message texts, spawns, paging, resumes, and commands.

    Tool result blocks are retained with their session, call id, record
    order, and text: the guarded call's result is where the guard's saved
    payload path was actually shown to the caller, and its record order
    bounds which later spawn calls can be extraction children.
    """

    texts: dict[str, list[_JSON]] = {}
    calls: list[_JSON] = []
    paging: list[_JSON] = []
    resumes: list[_JSON] = []
    commands: dict[str, list[str]] = {}
    command_events: list[_JSON] = []
    tool_results: list[_JSON] = []
    for order, record in enumerate(records):
        if record.get("kind") != "message":
            continue
        sid = record.get("session_id")
        data = record.get("data")
        if not isinstance(sid, str) or not isinstance(data, dict):
            continue
        citation = str(record.get("id"))
        role = data.get("role") if isinstance(data.get("role"), str) else None
        for block in data.get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.setdefault(sid, []).append(
                    {
                        "citation": citation,
                        "text": str(block["text"]),
                        "order": order,
                        "role": role,
                    }
                )
            elif block.get("type") == "tool_use":
                name = block.get("name")
                inputs = block.get("input")
                if not isinstance(name, str) or not isinstance(inputs, dict):
                    continue
                if name.startswith("swarm_"):
                    if "follow_up_session" in inputs:
                        # Paging a prior child's retained report is never
                        # resumption or a fresh extraction child.
                        paging.append(
                            {
                                "session_id": sid,
                                "tool": name,
                                "call_id": block.get("id"),
                                "citation": citation,
                                "order": order,
                            }
                        )
                    elif "resume_session" in inputs:
                        # A native coordinator resume call grounds a scripted
                        # handback; paging never does.
                        resumes.append(
                            {
                                "session_id": sid,
                                "tool": name,
                                "call_id": block.get("id"),
                                "citation": citation,
                                "order": order,
                                "resume_session": inputs.get("resume_session"),
                                "task": inputs.get("task"),
                            }
                        )
                    elif isinstance(inputs.get("task"), str):
                        calls.append(
                            {
                                "session_id": sid,
                                "worker": name.removeprefix("swarm_"),
                                "call_id": block.get("id"),
                                "citation": citation,
                                "order": order,
                                "assignment": str(inputs["task"]),
                            }
                        )
                if name == "bash" and isinstance(inputs.get("command"), str):
                    command = str(inputs["command"])
                    commands.setdefault(sid, []).append(command)
                    command_events.append(
                        {
                            "session_id": sid,
                            "command": command,
                            "citation": citation,
                            "order": order,
                        }
                    )
            elif block.get("type") == "tool_result":
                call_id = (
                    block.get("tool_use_id") or block.get("call_id") or block.get("id")
                )
                content = block.get("content")
                if isinstance(content, str):
                    result_text = content
                elif isinstance(content, list):
                    result_text = "\n".join(
                        str(item.get("text", ""))
                        for item in content
                        if isinstance(item, dict) and isinstance(item.get("text"), str)
                    )
                else:
                    result_text = "" if content is None else json.dumps(content)
                tool_results.append(
                    {
                        "session_id": sid,
                        "call_id": call_id if isinstance(call_id, str) else None,
                        "citation": citation,
                        "order": order,
                        "text": result_text,
                        "is_error": block.get("is_error") is True,
                    }
                )
    return {
        "texts": texts,
        "calls": calls,
        "paging": paging,
        "resumes": resumes,
        "commands": commands,
        "command_events": command_events,
        "tool_results": tool_results,
    }


def _artifact_facts(path: Path) -> tuple[list[str], list[str]]:
    """Return distinct spec facts carried by a retained artifact file."""

    if not path.is_file():
        return [], [f"retained controller artifact is missing: {path.name}"]
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return [], [f"unreadable controller artifact: {path.name}"]
    facts: list[str] = []
    if not isinstance(document, dict):
        return [], [f"controller artifact is not an object: {path.name}"]
    for item in document.get("distributed_facts", []):
        if isinstance(item, dict) and isinstance(item.get("spec_fact"), str):
            fact = str(item["spec_fact"])
            if fact not in facts:
                facts.append(fact)
    return facts, []


def _expected_sequence() -> list[_JSON]:
    return [
        {
            "step": index,
            "name": str(step["name"]),
            "blueprint": str(step["blueprint"]),
            "stage": int(step["stage"]),
            "action": str(step["action"]),
            "task": step.get("task"),
            "same_as": step.get("same_as"),
        }
        for index, step in enumerate(STEPS)
    ]


def _step_identities() -> dict[int, str]:
    """Resolve the resume identity of every scripted step index.

    The identity follows the same resolution as the same-session
    resumption groups: an explicit ``same_as`` or ``record`` identity,
    otherwise the identity inherited from the blueprint's earlier steps,
    otherwise the step's own name.
    """

    identities: dict[int, str] = {}
    inherited: dict[str, str] = {}
    for index, spec in enumerate(STEPS):
        identity = spec.get("same_as") or spec.get("record")
        if not identity:
            identity = inherited.get(str(spec["blueprint"]))
        if not identity:
            identity = str(spec["name"])
        identities[index] = str(identity)
        inherited[str(spec["blueprint"])] = str(identity)
    return identities


def _task_owner_identities() -> dict[str, str]:
    """Map each injected blocker task to its authoritative stage identity."""

    step_identities = _step_identities()
    tasks: dict[str, str] = {}
    for index, spec in enumerate(STEPS):
        if spec.get("action") == "inject" and isinstance(spec.get("task"), str):
            tasks[str(spec["task"])] = step_identities[index]
    return tasks


def _scripted_entry(event: _JSON) -> _JSON:
    """One advancing or failed step entry with its native citation."""

    return {
        "kind": str(event.get("kind")),
        "session_id": event.get("session_id"),
        "tool_call_id": event.get("tool_call_id"),
        "blueprint": event.get("blueprint"),
        "process_role": event.get("process_role"),
        "stage": event.get("stage"),
        "step_name": event.get("step_name"),
        "task": event.get("task"),
        "original_exit": event.get("original_exit"),
        "artifact_bytes": event.get("artifact_bytes"),
        "source_sha256": event.get("source_sha256"),
        "probe": event.get("probe"),
        "seq": event.get("seq"),
        "citation": event.get("_citation"),
        "_native_worker": event.get("_native_worker"),
    }


def _journal_mode(events: list[_JSON], state: _JSON) -> str:
    """Resolve the controller mode from the private retained evidence.

    The mode persisted by the controller into its evaluator-owned state
    before any event is preferred: a zero-event run still reports its
    mode, and a missing state file never forces a scripted verdict. Blind
    journals also carry ``mode: blind`` on their events. The older scripted
    journals predate both fields, so events with no recorded mode anywhere
    stay scripted: historical explicit-mode evidence, analyzed by the
    unchanged scripted path.
    """

    if isinstance(state, dict) and state.get("mode") in (MODE_BLIND, MODE_SCRIPTED):
        return str(state["mode"])
    for event in events:
        if str(event.get("mode")) == MODE_BLIND:
            return MODE_BLIND
    if not events:
        # Nothing was journaled and no mode was persisted: the controller
        # is blind by default, and empty coverage is never a script
        # violation.
        return MODE_BLIND
    return MODE_SCRIPTED


def _duplicate_seq_values(seq_values: list[int]) -> list[int]:
    """Return the sorted seq identifiers that occur more than once."""

    counts: dict[int, int] = {}
    for value in seq_values:
        counts[value] = counts.get(value, 0) + 1
    return sorted(value for value, count in counts.items() if count > 1)


def _unique_registry_id(base: str, record: _JSON, used: set[str]) -> str:
    """Return a collision-free supplemental registry id for one record.

    Historical journals can repeat a native sequence number when fresh
    hook processes once persisted the ledger out of step. Every record is
    preserved: the first occurrence keeps the seq-only id and later
    duplicates get a deterministic suffix from the native tool call id,
    then from the duplicate ordinal. The native historical seq inside the
    record data is never renumbered or overwritten; the ambiguity itself
    stays reported by the sequence checks.
    """

    if base not in used:
        used.add(base)
        return base
    call = record.get("tool_call_id")
    if isinstance(call, str) and call:
        suffixed = f"{base}:call:{call}"
        if suffixed not in used:
            used.add(suffixed)
            return suffixed
    ordinal = 2
    while f"{base}:record-{ordinal}" in used:
        ordinal += 1
    unique = f"{base}:record-{ordinal}"
    used.add(unique)
    return unique


def _blind_artifact_text_for(event: _JSON) -> tuple[str | None, str | None]:
    """Re-derive the blind artifact text recorded on one injected event.

    The blind artifact is deterministic given the stage and the guard
    threshold, so the recorded byte size identifies one reproducible text.
    When nothing reproduces the recorded size, exposure stays unknown rather
    than guessed.
    """

    stage = event.get("stage")
    recorded = event.get("artifact_bytes")
    if not isinstance(stage, int) or not isinstance(recorded, int) or recorded <= 0:
        return None, "injected blind event lacks a usable artifact size"
    low = 0
    high = recorded
    best: str | None = None
    while low <= high:
        middle = (low + high) // 2
        text = _blind_artifact_text(stage, middle)
        size = len(text.encode("utf-8"))
        if size >= recorded:
            best = text
            high = middle - 1
        else:
            low = middle + 1
    if best is None or len(best.encode("utf-8")) != recorded:
        return None, "recorded artifact bytes match no reproducible blind artifact"
    return best, None


def _result_of(observed: _JSON, session_id: Any, call_id: Any) -> _JSON | None:
    """Return the retained native tool result for one session and call."""

    if not isinstance(session_id, str) or not isinstance(call_id, str):
        return None
    for result in observed["tool_results"]:
        if not isinstance(result, dict):
            continue
        if result.get("session_id") == session_id and result.get("call_id") == call_id:
            return result
    return None


def _load_json_document(text: str) -> Any:
    """Parse one JSON document, tolerating a leading marker line."""

    try:
        return json.loads(text)
    except ValueError:
        pass
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("{"):
            try:
                return json.loads("\n".join(lines[index:]))
            except ValueError:
                return None
    return None


def _spec_facts_of(document: Any) -> list[str] | None:
    """Distinct spec facts carried by a diagnostic-details document."""

    if not isinstance(document, dict) or not isinstance(document.get("checks"), list):
        return None
    facts: list[str] = []
    for item in document["checks"]:
        if isinstance(item, dict) and isinstance(item.get("spec_fact"), str):
            fact = str(item["spec_fact"])
            if fact not in facts:
                facts.append(fact)
    return facts or None


def _required_facts_for(
    *,
    payload_root: Path,
    session_id: Any,
    saved_path: str,
    controller_event: _JSON | None,
) -> list[str] | None:
    """Distinct spec facts the extraction child must report, or None.

    Facts come from retained evidence only: the saved payload when it is
    the blind diagnostic artifact, otherwise the artifact re-derived from
    the controller's own injected event for the guarded call. When
    neither source yields facts the requirement stays unknown rather
    than guessed, and the extraction entry records report presence only.
    """

    if isinstance(session_id, str):
        candidate = payload_root / session_id / Path(saved_path).name
        if candidate.is_file():
            try:
                text = candidate.read_text(encoding="utf-8")
            except OSError:
                text = None
            if text is not None:
                facts = _spec_facts_of(_load_json_document(text))
                if facts:
                    return facts
    if isinstance(controller_event, dict):
        artifact, _problem = _blind_artifact_text_for(controller_event)
        if artifact is not None:
            facts = _spec_facts_of(_load_json_document(artifact))
            if facts:
                return facts
    return None


def _analyze_blind(
    *,
    events: list[_JSON],
    guard_events: list[_JSON],
    state: _JSON,
    native: dict[str, _JSON],
    observed: _JSON,
    payload_root: Path,
    missing: list[Any],
    violations: list[_JSON],
    supplemental: list[_JSON],
) -> tuple[_JSON, list[_JSON]]:
    """Observational blind-mode facts; never a correctness gate.

    Blind mode prescribes no route: any reasonable tier delegation or
    finishing without escalation is acceptable, unexercised behavior
    coverage is reported without judgment, and missing evidence stays
    unknown, never clean and never a failure. The fault timeline and
    masking facts are controller-private journal facts for the judge;
    whether any native caller retained access to them is reported in
    ``private_access`` as observed or uncertain, never claimed as
    verified blindness. Session identities are grounded only against
    native session evidence; a session without native evidence is
    reported as unknown, never claimed.
    """

    unknown: list[_JSON] = []
    if not events:
        # A zero-event journal is incomplete blind coverage, never a
        # scripted choreography that failed: no route was required and
        # nothing was observed.
        missing.append(
            {
                "dimension": "orchestration",
                "finding": (
                    "controller journaled no events: blind coverage is"
                    " incomplete, never a script violation"
                ),
            }
        )
    file_order = [event for event in events if isinstance(event.get("seq"), int)]
    seq_values = [int(event["seq"]) for event in file_order]
    if seq_values != sorted(seq_values) or len(set(seq_values)) != len(seq_values):
        duplicates = _duplicate_seq_values(seq_values)
        violations.append(
            {
                "dimension": "orchestration",
                "finding": (
                    "controller event sequence is not strictly monotonic"
                    + (
                        f"; duplicate seq identifiers {duplicates} keep all"
                        " records under deterministic supplemental id suffixes"
                        " and the native seq is never renumbered"
                        if duplicates
                        else ""
                    )
                ),
            }
        )
    ordered = sorted(file_order, key=lambda event: int(event["seq"]))

    def native_of(session_id: Any) -> _JSON | None:
        if not isinstance(session_id, str):
            return None
        return native.get(session_id)

    ungrounded: set[str] = set()
    for event in ordered:
        sid = event.get("session_id")
        if isinstance(sid, str) and sid not in ungrounded and native_of(sid) is None:
            ungrounded.add(sid)
            unknown.append(
                {
                    "dimension": "evidence_unknown",
                    "finding": (
                        f"controller event {event.get('seq')} names session"
                        f" {sid} with no native session evidence; the identity"
                        " stays unknown and is never claimed"
                    ),
                }
            )

    # Private, judge-aware fault timeline from the blind journal. Only the
    # controller's own journaled events and state feed this; no tier is ever
    # fabricated from prose.
    timeline: list[_JSON] = []
    for event in ordered:
        if str(event.get("kind")) not in _BLIND_TIMELINE_KINDS:
            continue
        session = native_of(event.get("session_id"))
        entry: _JSON = {
            "seq": event.get("seq"),
            "kind": event.get("kind"),
            "stage": event.get("stage"),
            "session_id": event.get("session_id"),
            "native_worker": (session or {}).get("worker"),
            "native_model": (session or {}).get("model"),
            "tool_call_id": event.get("tool_call_id"),
            "blueprint_claim": event.get("blueprint"),
            "visible": event.get("visible"),
            "original_exit": event.get("original_exit"),
            "original_evidence": event.get("original_evidence"),
            "artifact_bytes": event.get("artifact_bytes"),
            "artifact_sha256": event.get("artifact_sha256"),
            "citation": event.get("_citation"),
        }
        if session is None:
            entry["identity"] = "unknown: no native session evidence"
        timeline.append(entry)
    faults_state = state.get("blind_faults") if isinstance(state, dict) else None
    fault_summary: list[_JSON] = []
    if isinstance(faults_state, dict):
        for key in sorted(faults_state, key=str):
            fault = faults_state[key]
            if not isinstance(fault, dict):
                continue
            owner_session = native_of(fault.get("owner_session"))
            resolved_session = native_of(fault.get("resolved_by"))
            fault_summary.append(
                {
                    "stage": fault.get("stage", key),
                    "owner_session": fault.get("owner_session"),
                    "owner_native_worker": (owner_session or {}).get("worker"),
                    "injected_calls_per_session": {
                        str(sid): len(calls) if isinstance(calls, list) else None
                        for sid, calls in (fault.get("injected") or {}).items()
                    },
                    "fresh_sessions": list(fault.get("fresh_sessions") or []),
                    "resolved": bool(fault.get("resolved")),
                    "resolved_by": fault.get("resolved_by"),
                    "resolved_native_worker": (resolved_session or {}).get("worker"),
                    "resolved_call": fault.get("resolved_call"),
                }
            )
    elif state:
        unknown.append(
            {
                "dimension": "evidence_unknown",
                "finding": "controller state lacks blind fault bookkeeping",
            }
        )

    # Behavior coverage: observed coordination facts, with no required route.
    # Delegated worker names are the actual native swarm tool policy ids from
    # the tool calls, and every session identity is matched only against the
    # native session records, never against generic runtime guesses.
    spawn_observations: list[_JSON] = [
        {
            "session_id": call["session_id"],
            "caller_native_worker": (native_of(call["session_id"]) or {}).get("worker"),
            "delegated_worker": call["worker"],
            "call_id": call["call_id"],
            "citation": call["citation"],
        }
        for call in observed["calls"]
    ]
    resume_observations: list[_JSON] = [
        {
            "session_id": call["session_id"],
            "resume_session": call.get("resume_session"),
            "resumed_native_worker": (native_of(call.get("resume_session")) or {}).get(
                "worker"
            ),
            "task": call.get("task"),
            "call_id": call["call_id"],
            "citation": call["citation"],
        }
        for call in observed["resumes"]
    ]
    grounded_resumes = [
        item for item in resume_observations if item["resumed_native_worker"]
    ]
    higher_tier = [
        item
        for item in spawn_observations
        if item["delegated_worker"] in ("advanced", "expert")
    ]
    behavior_coverage: _JSON = {
        "worker_delegation": {
            "exercised": bool(spawn_observations),
            "observations": spawn_observations,
        },
        "higher_tier_assistance": {
            "exercised": bool(higher_tier),
            "observations": higher_tier,
        },
        "coordinator_resume_handoff": {
            "exercised": bool(resume_observations),
            "observations": resume_observations,
        },
        "same_session_resumption": {
            "exercised": bool(grounded_resumes),
            "observations": grounded_resumes,
        },
        "note": (
            "Observed coverage only. The routine -> advanced -> expert trace"
            " is a historical reference, never a required route: any"
            " reasonable tier route or finishing without escalation is"
            " acceptable, and an unexercised entry is reported, never a"
            " violation."
        ),
    }

    # Shielding: a masked non-initial investigation attaches a large
    # deterministic artifact; the real guard keeps its own selection. The
    # artifact must never appear raw in model-visible transcript text.
    artifact_events: list[_JSON] = []
    for event in ordered:
        if str(event.get("kind")) != "injected" or not isinstance(
            event.get("artifact_bytes"), int
        ):
            continue
        text, problem = _blind_artifact_text_for(event)
        session = native_of(event.get("session_id"))
        entry = {
            "seq": event.get("seq"),
            "stage": event.get("stage"),
            "session_id": event.get("session_id"),
            "native_worker": (session or {}).get("worker"),
            "artifact_bytes": event.get("artifact_bytes"),
            "artifact_sha256": event.get("artifact_sha256"),
            "reproducible": text is not None,
            "citation": event.get("_citation"),
        }
        if text is None:
            entry["exposure"] = "unknown: " + str(problem)
            unknown.append(
                {
                    "dimension": "context_shielding",
                    "finding": (
                        f"blind artifact for event {event.get('seq')} cannot"
                        " be re-derived; exposure stays unknown"
                    ),
                }
            )
        else:
            probe = text[:_EXPOSURE_PROBE_BYTES]
            exposed = [
                item["citation"]
                for texts in observed["texts"].values()
                for item in texts
                if probe in str(item["text"])
            ]
            entry["exposed_in_native_transcripts"] = exposed
            if exposed:
                violations.append(
                    {
                        "dimension": "context_shielding",
                        "finding": (
                            f"blind artifact from event {event.get('seq')} is"
                            " exposed raw in model-visible transcript text"
                        ),
                        "citations": exposed,
                    }
                )
        artifact_events.append(entry)

    # Guard extractions: only a guard run that actually replaced a result
    # made an extraction request; a replacement-free guard run generates
    # no row and reuses no child. The extraction child is a spawn call by
    # the same immediate parent, spawned after the guarded call, whose
    # native assignment names the exact saved payload path the guard
    # showed the caller, and whose actual report is retained with the
    # required facts whenever retained evidence can derive them. The
    # initial discovery spawn by the same parent never matches, and the
    # verdict stays observational: no route is required or forced.
    controller_by_call = {
        str(event.get("tool_call_id")): event
        for event in ordered
        if isinstance(event.get("tool_call_id"), str)
    }
    extractions: list[_JSON] = []
    for guard in guard_events:
        guard_section = guard.get("guard")
        guard_info = guard_section if isinstance(guard_section, dict) else {}
        if guard_info.get("replacement") is not True:
            continue
        sid = guard.get("session_id")
        session = native_of(sid)
        call = guard.get("tool_call_id")
        entry = {
            "guard_seq": guard.get("seq"),
            "session_id": sid,
            "native_worker": (session or {}).get("worker"),
            "tool_call_id": call,
            "citation": guard.get("_registry_id")
            or f"{_CITATION}:controller:guard-run:{guard.get('seq')}",
            "guard_replacement": True,
        }
        guarded_result = _result_of(observed, sid, call)
        saved_path = None
        if guarded_result is not None:
            match = _SAVED_PAYLOAD_PATH.search(str(guarded_result.get("text") or ""))
            if match is not None:
                saved_path = match.group(1)
        entry["saved_payload_path"] = saved_path
        if saved_path is None:
            entry["extraction_child"] = None
            unknown.append(
                {
                    "dimension": "evidence_unknown",
                    "finding": (
                        f"guard run {guard.get('seq')} replaced a result but"
                        " no saved payload path is retained; the extraction"
                        " correlation stays unknown"
                    ),
                }
            )
            extractions.append(entry)
            continue
        retained_payload = None
        if isinstance(sid, str):
            candidate_path = payload_root / sid / Path(saved_path).name
            if candidate_path.is_file():
                try:
                    payload = candidate_path.read_bytes()
                    retained_payload = {
                        "name": candidate_path.name,
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                except OSError:
                    retained_payload = None
        entry["saved_payload_retained"] = retained_payload
        required_facts = _required_facts_for(
            payload_root=payload_root,
            session_id=sid,
            saved_path=saved_path,
            controller_event=controller_by_call.get(str(call)),
        )
        entry["required_facts"] = required_facts
        guard_order = (
            guarded_result.get("order") if guarded_result is not None else None
        )
        if not isinstance(guard_order, int):
            entry["extraction_child"] = None
            unknown.append(
                {
                    "dimension": "evidence_unknown",
                    "finding": (
                        f"guarded call {call} for guard run"
                        f" {guard.get('seq')} has no retained native result;"
                        " spawn chronology is unverifiable"
                    ),
                }
            )
            extractions.append(entry)
            continue
        spawn_calls: list[_JSON] = []
        for spawn in observed["calls"]:
            if spawn["session_id"] != sid or spawn["order"] <= guard_order:
                continue
            if saved_path not in str(spawn.get("assignment") or ""):
                continue
            child_result = _result_of(observed, sid, spawn["call_id"])
            report_text = (
                str(child_result.get("text") or "") if child_result is not None else ""
            )
            child: _JSON = {
                "worker": spawn["worker"],
                "call_id": spawn["call_id"],
                "citation": spawn["citation"],
                "report_citation": (
                    child_result.get("citation") if child_result is not None else None
                ),
                "report_present": bool(report_text.strip()),
            }
            if required_facts:
                matched_facts = [fact for fact in required_facts if fact in report_text]
                child["matched_facts"] = matched_facts
                child["facts_present"] = bool(matched_facts)
            else:
                child["matched_facts"] = None
                child["facts_present"] = None
            spawn_calls.append(child)
        entry["spawn_calls"] = spawn_calls
        entry["report_citations"] = [
            child["report_citation"]
            for child in spawn_calls
            if child["report_citation"]
        ]
        matched_children = [
            child
            for child in spawn_calls
            if child["report_present"] and child["facts_present"] is not False
        ]
        if len(matched_children) == 1:
            entry["extraction_child"] = matched_children[0]
        else:
            entry["extraction_child"] = None
            if len(matched_children) > 1:
                entry["note"] = (
                    "multiple extraction children observed; reported without judgment"
                )
            else:
                unknown.append(
                    {
                        "dimension": "evidence_unknown",
                        "finding": (
                            f"no native extraction child observed for guard"
                            f" run {guard.get('seq')}"
                        ),
                    }
                )
        extractions.append(entry)

    # Private controller evidence access: observed only when a retained
    # native command actually reads a controller-private journal or state
    # path, otherwise uncertain. Context shielding of raw artifacts is a
    # distinct fact and never implies controller-private blindness.
    private_observations: list[_JSON] = []
    for item in observed["command_events"]:
        command = str(item.get("command") or "")
        if _PRIVATE_EVIDENCE_PATH.search(command):
            private_observations.append(
                {
                    "session_id": item["session_id"],
                    "native_worker": (native_of(item["session_id"]) or {}).get(
                        "worker"
                    ),
                    "command": command,
                    "citation": item["citation"],
                }
            )
    private_access: _JSON = {
        "status": "observed" if private_observations else "uncertain",
        "observations": private_observations,
        "note": (
            "Observed access to controller-private evidence paths only;"
            " uncertain never means verified blindness, and context"
            " shielding of raw artifacts is a separate fact."
        ),
    }

    orchestration_status = "healthy"
    if any(item["dimension"] == "orchestration" for item in violations):
        orchestration_status = "violations"
    elif any(
        not isinstance(item, dict) or item.get("dimension") == "orchestration"
        for item in missing
    ):
        # Missing orchestration evidence, including a zero-event journal,
        # is incomplete coverage, never clean and never a violation.
        orchestration_status = "incomplete"
    shielding_status = "healthy"
    if any(item["dimension"] == "context_shielding" for item in violations):
        shielding_status = "violations"
    elif any(item.get("dimension") == "context_shielding" for item in unknown):
        shielding_status = "incomplete"
    evidence_status = "healthy"
    if any(item["dimension"] == "evidence_unknown" for item in violations):
        evidence_status = "violations"
    elif unknown:
        evidence_status = "incomplete"

    status = "healthy"
    if violations:
        status = "unhealthy"
    elif (
        orchestration_status != "healthy"
        or shielding_status != "healthy"
        or evidence_status != "healthy"
        or missing
    ):
        status = "incomplete"

    report_json: _JSON = {
        "status": status,
        "mode": MODE_BLIND,
        "scenario": SCENARIO_ID,
        "stages": list(STAGES),
        "fault_timeline": timeline,
        "blind_faults": fault_summary,
        "behavior_coverage": behavior_coverage,
        "orchestration": {
            "status": orchestration_status,
            "violations": [
                item for item in violations if item["dimension"] == "orchestration"
            ],
            "missing_evidence": [
                item
                for item in missing
                if not isinstance(item, dict)
                or item.get("dimension") == "orchestration"
            ],
        },
        "context_shielding": {
            "status": shielding_status,
            "artifact_events": artifact_events,
            "violations": [
                item for item in violations if item["dimension"] == "context_shielding"
            ],
            "missing_evidence": [
                item
                for item in missing
                if isinstance(item, dict)
                and item.get("dimension") == "context_shielding"
            ]
            + [
                item for item in unknown if item.get("dimension") == "context_shielding"
            ],
        },
        "evidence_unknown": {
            "status": evidence_status,
            "extractions": extractions,
            "unknown": unknown,
            "violations": [
                item for item in violations if item["dimension"] == "evidence_unknown"
            ],
        },
        "private_access": private_access,
        "note": (
            "Blind observational facts only; never a correctness gate and"
            " independent acceptance remains the sole gate. The fault timeline"
            " and masking evidence are controller-private journal facts for"
            " the judge; whether any native caller retained access to them"
            " is reported in private_access as observed or uncertain, never"
            " claimed as verified blindness, and context shielding of raw"
            " artifacts is a separate fact. Any reasonable tier route, or"
            " finishing without escalation, is acceptable; unexercised"
            " coverage is reported, never a violation; missing evidence"
            " stays unknown, never clean."
        ),
    }
    return report_json, supplemental


def analyze_escalation_resumption(
    records: list[_JSON],
    *,
    trace_root: Path,
    evidence_gaps: list[Any],
) -> tuple[_JSON, list[_JSON]]:
    """Report observed escalation/resumption facts, never task correctness."""

    raw_root = trace_root.parent
    controller_root = raw_root / "other"
    payload_root = raw_root / "hook-payloads"
    missing: list[Any] = list(evidence_gaps)
    unknown: list[Any] = []
    violations: list[_JSON] = []
    supplemental: list[_JSON] = []

    events, event_problems = _read_jsonl(controller_root / "events.jsonl")
    guard_events, guard_problems = _read_jsonl(controller_root / "guard-events.jsonl")
    missing.extend(event_problems)
    missing.extend(guard_problems)
    state: _JSON = {}
    state_path = controller_root / "state.json"
    if state_path.is_file():
        try:
            loaded = json.loads(state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
            else:
                missing.append("controller state is not an object")
        except OSError, ValueError:
            missing.append("unreadable controller state")
    else:
        missing.append("controller state file is missing")

    native = _native_sessions(records)
    observed = _scan_messages(records)
    guard_by_call = {
        str(event.get("tool_call_id")): event
        for event in guard_events
        if isinstance(event.get("tool_call_id"), str)
    }
    used_registry_ids: set[str] = set()

    # Native citations with exact session names for every controller record.
    for event in events:
        sid = event.get("session_id")
        session = native.get(sid) if isinstance(sid, str) else None
        base_id = f"{_CITATION}:controller:event:{event.get('seq')}"
        record: _JSON = {
            "id": _unique_registry_id(base_id, event, used_registry_ids),
            "kind": "controller-event",
            "session_id": sid,
            "worker": session.get("worker") if session else None,
            "data": {
                **event,
                "native_worker": session.get("worker") if session else None,
                "native_model": session.get("model") if session else None,
            },
        }
        supplemental.append(record)
        event["_citation"] = record["id"]
        event["_native_worker"] = record["data"]["native_worker"]
    for event in guard_events:
        sid = event.get("session_id")
        session = native.get(sid) if isinstance(sid, str) else None
        base_id = f"{_CITATION}:controller:guard-run:{event.get('seq')}"
        guard_registry_id = _unique_registry_id(base_id, event, used_registry_ids)
        event["_registry_id"] = guard_registry_id
        supplemental.append(
            {
                "id": guard_registry_id,
                "kind": "controller-guard-run",
                "session_id": sid,
                "worker": session.get("worker") if session else None,
                "data": {
                    **event,
                    "native_worker": session.get("worker") if session else None,
                },
            }
        )
    if state:
        supplemental.append(
            {
                "id": f"{_CITATION}:controller:state",
                "kind": "controller-state",
                "data": state,
            }
        )
    for call in observed["resumes"]:
        registry_id = (
            f"{_CITATION}:native:resume:{call['call_id']}"
            if isinstance(call["call_id"], str) and call["call_id"]
            else (f"{_CITATION}:native:resume:{call['citation']}:{call['order']}")
        )
        registry_id = _unique_registry_id(registry_id, call, used_registry_ids)
        supplemental.append(
            {
                "id": registry_id,
                "kind": "native-resume-call",
                "session_id": call["session_id"],
                "worker": native.get(call["session_id"], {}).get("worker"),
                "data": dict(call),
            }
        )

    # Mode dispatch prefers the mode persisted in the evaluator-owned
    # controller state before any event: blind journals carry
    # ``mode: blind`` on their events; the older scripted journals have no
    # mode field anywhere and stay on the unchanged scripted path below,
    # while a zero-event journal is blind coverage, never a forced script.
    if _journal_mode(events, state) == MODE_BLIND:
        return _analyze_blind(
            events=events,
            guard_events=guard_events,
            state=state,
            native=native,
            observed=observed,
            payload_root=payload_root,
            missing=missing,
            violations=violations,
            supplemental=supplemental,
        )

    # ---- orchestration dimension -------------------------------------
    # Historical explicit scripted mode: every assertion below describes the
    # scripted controller choreography and never applies to blind journals.
    file_order = [event for event in events if isinstance(event.get("seq"), int)]
    seq_values = [int(event["seq"]) for event in file_order]
    if seq_values != sorted(seq_values) or len(set(seq_values)) != len(seq_values):
        duplicates = _duplicate_seq_values(seq_values)
        violations.append(
            {
                "dimension": "orchestration",
                "finding": (
                    "controller event sequence is not strictly monotonic"
                    + (
                        f"; duplicate seq identifiers {duplicates} keep all"
                        " records under deterministic supplemental id suffixes"
                        " and the native seq is never renumbered"
                        if duplicates
                        else ""
                    )
                ),
            }
        )
    ordered = sorted(file_order, key=lambda event: int(event["seq"]))
    advancing: list[_JSON] = []
    failed_attempts: list[_JSON] = []
    replays: list[_JSON] = []
    seen_calls: dict[str, _JSON] = {}
    out_of_script: list[_JSON] = []
    counted_failures: dict[str, list[_JSON]] = {}
    duplicate_failures: dict[str, list[_JSON]] = {}
    foreign_failures: dict[str, list[_JSON]] = {}
    # The authoritative first-stage owner session per resume identity is
    # the first journaled worker event that matches its scripted step:
    # the initial Routine session for B2, the same Routine session for
    # R3's blocker, and the original Advanced session for A3's blocker.
    # A controlled failure from any other session never counts toward the
    # exact budget; it stays an ownership violation.
    step_identities = _step_identities()
    task_identities = _task_owner_identities()
    owner_sessions: dict[str, str] = {}
    for event in ordered:
        step_index = event.get("step")
        if not isinstance(step_index, int) or step_index not in step_identities:
            continue
        if str(event.get("kind")) in _REJECTED_KINDS:
            continue
        if event.get("process_role") != "worker":
            continue
        spec = STEPS[step_index]
        step_identity = step_identities[step_index]
        if step_identity in owner_sessions:
            continue
        if str(event.get("blueprint")) != str(spec["blueprint"]) or event.get(
            "stage"
        ) != int(spec["stage"]):
            continue
        sid = event.get("session_id")
        if isinstance(sid, str):
            owner_sessions[step_identity] = sid
    # Pass one: replays, rejected calls, and the exact controlled-failure
    # count per scripted blocker. A failure is meaningful when it runs one
    # of the two distinct actual diagnostic probes whose probe identity has
    # not been counted yet for the same task; the first injection and any
    # probe-distinct reinjection both count, while already-counted probes,
    # no-probe runs, duplicates, and replays never do. Source fingerprints
    # are retained evidence, never the budget discriminator. Only a failure
    # on the authoritative first-stage owner session can count at all: a
    # foreign failure is excluded and reported as an ownership violation
    # instead of satisfying or exceeding the budget.
    for event in ordered:
        kind = str(event.get("kind"))
        sid = event.get("session_id")
        call = event.get("tool_call_id")
        is_replay = False
        if isinstance(call, str) and call:
            is_replay = call in seen_calls and (
                kind in _ADVANCING_ACTIONS or kind in _FAILURE_KINDS
            )
            if is_replay:
                replays.append(
                    {
                        "tool_call_id": call,
                        "citations": [
                            seen_calls[call]["_citation"],
                            event["_citation"],
                        ],
                    }
                )
            seen_calls.setdefault(call, event)
        if kind in _REJECTED_KINDS:
            out_of_script.append(
                {
                    "kind": kind,
                    "session_id": sid,
                    "blueprint": event.get("blueprint"),
                    "stage": event.get("stage"),
                    "task": event.get("task"),
                    "expected_session": event.get("expected_session"),
                    "citation": event.get("_citation"),
                }
            )
        task = event.get("task")
        if kind in _FAILURE_KINDS and isinstance(task, str) and not is_replay:
            expected_owner = owner_sessions.get(task_identities.get(str(task), ""))
            if expected_owner is not None and sid != expected_owner:
                foreign_failures.setdefault(str(task), []).append(event)
                continue
            probe = event.get("probe")
            bucket = counted_failures.setdefault(str(task), [])
            counted_probe_set = {
                str(item.get("probe")) for item in bucket if item.get("probe") in PROBES
            }
            meaningful = kind == "over-budget" or (
                probe in PROBES and str(probe) not in counted_probe_set
            )
            if meaningful:
                bucket.append(event)
            else:
                duplicate_failures.setdefault(str(task), []).append(event)
        elif kind == "duplicate-attempt" and isinstance(task, str) and not is_replay:
            # The controller's own duplicate flagging is the excluded-
            # duplicate evidence for the same scripted blocker.
            duplicate_failures.setdefault(str(task), []).append(event)
    # Pass two: classify the events that advance or fail the scripted steps.
    # An injected blocker advances only on its exact second counted failure;
    # the first failure is retained as a controlled failure, never a step.
    for event in ordered:
        kind = str(event.get("kind"))
        task = event.get("task")
        if kind in {"injected", "blocker-reinjected"}:
            bucket = (
                counted_failures.get(str(task), []) if isinstance(task, str) else []
            )
            if len(bucket) >= FAILURE_BUDGET and bucket[FAILURE_BUDGET - 1] is event:
                entry = _scripted_entry(event)
                entry["action"] = "inject"
                advancing.append(entry)
            continue
        if kind not in _ADVANCING_ACTIONS:
            continue
        exit_value = event.get("original_exit")
        entry = _scripted_entry(event)
        entry["action"] = _ADVANCING_ACTIONS[kind]
        if kind == "artifact-emitted" or exit_value == 0:
            advancing.append(entry)
        elif kind in _FAILED_KINDS:
            failed_attempts.append(entry)
    expected = _expected_sequence()
    observed_steps: list[_JSON] = []
    step_violations: list[str] = []
    script_incomplete = len(advancing) < len(expected)
    if script_incomplete:
        missing.append(
            {
                "dimension": "orchestration",
                "finding": (
                    f"script incomplete: {len(advancing)} of {len(expected)}"
                    " scripted steps observed"
                ),
            }
        )
    elif len(advancing) > len(expected):
        step_violations.append(
            f"unexpected extra advancing events: {len(advancing)} observed for"
            f" {len(expected)} scripted steps"
        )
    for index, step in enumerate(expected):
        if index >= len(advancing):
            observed_steps.append({**step, "observed": False})
            continue
        entry = advancing[index]
        matches = (
            str(entry["blueprint"]) == step["blueprint"]
            and entry["stage"] == step["stage"]
            and entry["action"] == step["action"]
            and (step["task"] is None or (entry["task"] or None) == step["task"])
        )
        if not matches:
            step_violations.append(
                f"step {index} ({step['name']}) observed as "
                f"{entry['kind']} stage {entry['stage']} by {entry['blueprint']}"
            )
        if entry["process_role"] != "worker":
            step_violations.append(
                f"step {index} advanced by non-worker role {entry['process_role']}"
            )
        observed_steps.append({**step, "observed": True, "event": entry})
    for problem in step_violations:
        violations.append({"dimension": "orchestration", "finding": problem})
    for replay in replays:
        violations.append(
            {
                "dimension": "orchestration",
                "finding": "replayed tool call id in advancing controller events",
                "tool_call_id": replay["tool_call_id"],
                "citations": replay["citations"],
            }
        )
    identity_mismatch = [
        item for item in out_of_script if item["kind"] == "identity-mismatch"
    ]
    for item in identity_mismatch:
        violations.append(
            {
                "dimension": "orchestration",
                "finding": "session identity mismatch: fresh substitution or"
                " forgery attempt rejected by the controller",
                "citation": item["citation"],
            }
        )

    # ---- exact controlled-failure budget per scripted blocker -----------
    # Every injected blocker must observe exactly FAILURE_BUDGET distinct
    # meaningful controlled failures on one native session that matches the
    # expected stage owner. The first injection and any meaningful
    # reinjection both count; duplicates and replays never do. Only the
    # authoritative first-stage owner session's failures count, so a
    # foreign failure leaves the budget unsatisfied and is itself an
    # ownership violation. A missing second failure is incomplete
    # evidence; a premature next-tier request or a third meaningful retry
    # is a violation.
    controlled_failures: list[_JSON] = []
    inject_steps = [
        (index, spec)
        for index, spec in enumerate(STEPS)
        if spec.get("action") == "inject"
    ]
    for index, spec in inject_steps:
        task = str(spec["task"])
        bucket = counted_failures.get(task, [])
        foreign = foreign_failures.get(task, [])
        attempts: list[_JSON] = []
        for position, failure in enumerate(bucket, start=1):
            failure_sid = failure.get("session_id")
            failure_native = (
                native.get(failure_sid) if isinstance(failure_sid, str) else None
            )
            attempts.append(
                {
                    "attempt_number": position,
                    "session_id": failure_sid,
                    "tool_call_id": failure.get("tool_call_id"),
                    "blueprint": failure.get("blueprint"),
                    "probe": failure.get("probe"),
                    "source_sha256": failure.get("source_sha256"),
                    "native_worker": (
                        failure_native.get("worker") if failure_native else None
                    ),
                    "citation": failure.get("_citation"),
                }
            )
        owner = attempts[0]["session_id"] if attempts else None
        owner_native = native.get(owner) if isinstance(owner, str) else None
        expected_owner = owner_sessions.get(task_identities.get(task, ""))
        budget_item: _JSON = {
            "step": str(spec["name"]),
            "task": task,
            "expected_owner_blueprint": str(spec["blueprint"]),
            "expected_failures": FAILURE_BUDGET,
            "observed_failures": len(attempts),
            "owner_session_id": owner,
            "authoritative_owner_session_id": expected_owner,
            "owner_native_worker": (
                owner_native.get("worker") if owner_native else None
            ),
            "attempts": attempts,
            "duplicates_excluded": len(duplicate_failures.get(task, [])),
            "foreign_excluded": len(foreign),
            "foreign_failures": [
                {
                    "session_id": failure.get("session_id"),
                    "tool_call_id": failure.get("tool_call_id"),
                    "native_worker": failure.get("_native_worker"),
                    "citation": failure.get("_citation"),
                }
                for failure in foreign
            ],
        }
        budget_violation = bool(foreign)
        if len(attempts) < FAILURE_BUDGET:
            missing.append(
                {
                    "dimension": "orchestration",
                    "finding": (
                        f"controlled blocker {task} observed"
                        f" {len(attempts)} of exactly {FAILURE_BUDGET} distinct"
                        " controlled failures; the two-failure budget is not"
                        " satisfied"
                    ),
                }
            )
            budget_item["status"] = "violations" if budget_violation else "incomplete"
        else:
            if len(attempts) > FAILURE_BUDGET:
                budget_violation = True
                violations.append(
                    {
                        "dimension": "orchestration",
                        "finding": (
                            f"controlled blocker {task} observed"
                            f" {len(attempts)} distinct controlled failures;"
                            f" the exact budget is {FAILURE_BUDGET} and further"
                            " meaningful retries are over budget"
                        ),
                        "citations": [
                            attempt["citation"] for attempt in attempts[FAILURE_BUDGET:]
                        ],
                    }
                )
            sessions = sorted(
                {
                    str(attempt["session_id"])
                    for attempt in attempts
                    if isinstance(attempt["session_id"], str)
                }
            )
            if len(sessions) > 1:
                budget_violation = True
                violations.append(
                    {
                        "dimension": "orchestration",
                        "finding": (
                            f"controlled failures for {task} span multiple"
                            f" sessions {sessions}; the expected stage owner"
                            " must stay one native session"
                        ),
                        "citations": [attempt["citation"] for attempt in attempts],
                    }
                )
            counting_probes = [
                attempt.get("probe")
                for attempt in attempts[:FAILURE_BUDGET]
                if attempt.get("probe") in PROBES
            ]
            if (
                len(attempts) >= FAILURE_BUDGET
                and len(counting_probes) < FAILURE_BUDGET
            ):
                budget_violation = True
                violations.append(
                    {
                        "dimension": "orchestration",
                        "finding": (
                            f"controlled failures for {task} do not use two"
                            " distinct actual diagnostic probes; a probe"
                            " identity counts once and a no-probe or repeated"
                            " probe run never satisfies the budget"
                        ),
                        "citations": [
                            attempt["citation"] for attempt in attempts[:FAILURE_BUDGET]
                        ],
                    }
                )
            elif len(set(counting_probes)) != len(counting_probes):
                budget_violation = True
                violations.append(
                    {
                        "dimension": "orchestration",
                        "finding": (
                            f"controlled failures for {task} repeat one"
                            " diagnostic probe; the exact budget requires two"
                            " distinct probe identities"
                        ),
                        "citations": [
                            attempt["citation"] for attempt in attempts[:FAILURE_BUDGET]
                        ],
                    }
                )
            wrong_owner = [
                attempt
                for attempt in attempts
                if attempt["native_worker"] is not None
                and attempt["native_worker"] != str(spec["blueprint"])
            ]
            if wrong_owner:
                budget_violation = True
                violations.append(
                    {
                        "dimension": "orchestration",
                        "finding": (
                            f"controlled failure for {task} is not owned by"
                            f" the expected {spec['blueprint']} stage owner;"
                            " native session evidence disagrees"
                        ),
                        "citations": [attempt["citation"] for attempt in wrong_owner],
                    }
                )
            for attempt in attempts:
                if attempt["native_worker"] is None:
                    unknown.append(
                        {
                            "dimension": "orchestration",
                            "finding": (
                                f"controlled failure"
                                f" {attempt['attempt_number']} for {task} has"
                                " no native session evidence"
                            ),
                        }
                    )
            budget_item["status"] = "violations" if budget_violation else "observed"
        if foreign:
            foreign_sessions = sorted(
                {
                    str(failure.get("session_id"))
                    for failure in foreign
                    if isinstance(failure.get("session_id"), str)
                }
            )
            violations.append(
                {
                    "dimension": "orchestration",
                    "finding": (
                        f"controlled failure for {task} is claimed by"
                        f" foreign session(s) {foreign_sessions}; the"
                        " authoritative"
                        f" {spec['blueprint']} stage owner is"
                        f" {expected_owner} and a foreign failure never"
                        " counts toward the exact budget"
                    ),
                    "citations": [failure.get("_citation") for failure in foreign],
                }
            )
        # The next tier unlocks only after the exact second failure: the
        # first advancing event matching the next scripted step must not
        # precede the second counted failure of this blocker.
        if index + 1 < len(STEPS):
            next_spec = STEPS[index + 1]
            next_entries = [
                entry
                for entry in advancing
                if str(entry["blueprint"]) == str(next_spec["blueprint"])
                and entry["stage"] == int(next_spec["stage"])
                and entry["action"] == str(next_spec["action"])
                and (
                    next_spec.get("task") is None
                    or (entry.get("task") or None) == next_spec.get("task")
                )
            ]
            second_seq = (
                bucket[FAILURE_BUDGET - 1].get("seq")
                if len(bucket) >= FAILURE_BUDGET
                else None
            )
            for entry in next_entries:
                entry_seq = entry.get("seq")
                if second_seq is None or (
                    isinstance(entry_seq, int) and entry_seq < second_seq
                ):
                    budget_item["status"] = "violations"
                    violations.append(
                        {
                            "dimension": "orchestration",
                            "finding": (
                                f"next tier step {next_spec['name']} advanced"
                                f" before the exact {FAILURE_BUDGET} controlled"
                                f" failures for {task} were observed"
                            ),
                            "citation": entry.get("citation"),
                        }
                    )
        controlled_failures.append(budget_item)
    for rejected in out_of_script:
        if rejected["kind"] != "premature-escalation":
            continue
        task_name = f" for {rejected['task']}" if rejected.get("task") else ""
        violations.append(
            {
                "dimension": "orchestration",
                "finding": (
                    "premature next-tier request rejected: the exact"
                    f" {FAILURE_BUDGET}-failure controlled budget"
                    f"{task_name} was not exhausted"
                ),
                "citation": rejected["citation"],
            }
        )

    # Same-session resumption identities grounded in controller records.
    # Steps sharing a recorded or inherited identity must keep one session.
    resume_groups: dict[str, list[str]] = {}
    inherited: dict[str, str] = {}
    for step, spec in zip(observed_steps, STEPS, strict=False):
        if not step.get("observed"):
            continue
        identity = spec.get("same_as") or spec.get("record")
        if not identity:
            identity = inherited.get(str(spec["blueprint"]))
        if not identity:
            identity = str(spec["name"])
        resume_groups.setdefault(str(identity), []).append(step["event"]["session_id"])
        inherited[str(spec["blueprint"])] = str(identity)
    same_session: list[_JSON] = []
    for key, session_ids in resume_groups.items():
        distinct = sorted({sid for sid in session_ids if isinstance(sid, str)})
        consistent = len(distinct) <= 1 and all(
            isinstance(sid, str) for sid in session_ids
        )
        same_session.append(
            {
                "identity": key,
                "session_ids": session_ids,
                "consistent": consistent,
                "native": native.get(distinct[0]) if distinct else None,
            }
        )
        if not consistent:
            violations.append(
                {
                    "dimension": "orchestration",
                    "finding": f"identity {key} spans multiple sessions: {distinct}",
                }
            )
    state_identity_problems: list[str] = []
    for key, session_ids in resume_groups.items():
        recorded = state.get(f"{key}_session")
        if isinstance(recorded, str) and session_ids and recorded not in session_ids:
            state_identity_problems.append(
                f"state {key}_session {recorded} does not match observed"
                f" {sorted(set(session_ids))}"
            )
    for problem in state_identity_problems:
        violations.append(
            {
                "dimension": "orchestration",
                "finding": f"state/event mismatch: {problem}",
            }
        )
    for sid in sorted(
        {
            sid
            for session_ids in resume_groups.values()
            for sid in session_ids
            if isinstance(sid, str)
        }
    ):
        if sid not in native:
            unknown.append(
                {
                    "dimension": "orchestration",
                    "finding": f"resumed session {sid} has no native session evidence",
                }
            )

    # Native resume grounding for each scripted handback. Controller steps
    # stay scoped proof of the script, but never the sole proof: a healthy
    # handback additionally requires a native coordinator resume call for
    # the exact original session, a nonempty task corresponding to the
    # resolved blocker, helper completion before the call, and new worker
    # turns after it. Missing native evidence stays incomplete; a fresh
    # session, wrong blocker, or overlapping calls are contradictions.
    scripted_blockers = sorted(
        {
            f"B{int(spec['blocker'])}"
            for spec in STEPS
            if spec.get("action") == "inject" and isinstance(spec.get("blocker"), int)
        }
    )
    resume_calls = observed["resumes"]
    handback_specs: list[_JSON] = []
    for index in range(len(observed_steps) - 1):
        helper = observed_steps[index]
        target = observed_steps[index + 1]
        if not (helper.get("observed") and target.get("observed")):
            continue
        if helper["action"] != "resolve":
            continue
        if (helper.get("same_as") or helper["name"]) == (
            target.get("same_as") or target["name"]
        ):
            continue
        target_spec = STEPS[index + 1]
        target_sid = target["event"].get("session_id")
        target_native = native.get(target_sid) if isinstance(target_sid, str) else None
        coordinator_sid = (
            target_native.get("parent_session_id") if target_native else None
        )
        tool_name = f"swarm_{target_spec['blueprint']}"
        matching = [
            position
            for position, call in enumerate(resume_calls)
            if call["session_id"] == coordinator_sid
            and call["tool"] == tool_name
            and call.get("resume_session") == target_sid
        ]
        handback_specs.append(
            {
                "helper_index": index,
                "target_index": index + 1,
                "helper_spec": STEPS[index],
                "target_spec": target_spec,
                "helper_sid": helper["event"].get("session_id"),
                "target_sid": target_sid,
                "coordinator_sid": coordinator_sid,
                "tool_name": tool_name,
                "matching": matching,
            }
        )
    handback_reports: list[_JSON] = []
    resume_evidence_gap = False
    consumed: set[int] = set()
    for handback in handback_specs:
        helper_step = observed_steps[handback["helper_index"]]
        target_step = observed_steps[handback["target_index"]]
        helper_name = str(helper_step["name"])
        target_name = str(target_step["name"])
        helper_spec = handback["helper_spec"]
        expected_token = (
            f"B{int(helper_spec['clears_blocker'])}"
            if isinstance(helper_spec.get("clears_blocker"), int)
            else None
        )
        report_item: _JSON = {
            "handback": f"{target_name} after {helper_name}",
            "helper_step": helper_name,
            "target_step": target_name,
            "target_session_id": handback["target_sid"],
            "coordinator_session_id": handback["coordinator_sid"],
            "expected_blocker": expected_token,
            "resume_call": None,
            "new_turn_citations": [],
        }
        handback_reports.append(report_item)
        handback_violation = False
        handback_gap = False
        if not isinstance(handback["target_sid"], str) or not isinstance(
            handback["coordinator_sid"], str
        ):
            resume_evidence_gap = True
            missing.append(
                {
                    "dimension": "orchestration",
                    "finding": (
                        f"handback {target_name} has no native coordinator"
                        f" evidence for resumed session"
                        f" {handback['target_sid']}"
                    ),
                }
            )
            report_item["status"] = "unknown"
            continue
        for call in resume_calls:
            if (
                call["session_id"] != handback["coordinator_sid"]
                or call["tool"] != handback["tool_name"]
            ):
                continue
            claimed = call.get("resume_session")
            if not isinstance(claimed, str) or claimed == handback["target_sid"]:
                continue
            claimed_native = native.get(claimed)
            if claimed_native is None:
                resume_evidence_gap = True
                handback_gap = True
                missing.append(
                    {
                        "dimension": "orchestration",
                        "finding": (
                            f"resume call for {target_name} targets unknown"
                            f" session {claimed}"
                        ),
                        "citation": call["citation"],
                    }
                )
            elif claimed_native.get("worker") == str(
                handback["target_spec"]["blueprint"]
            ):
                handback_violation = True
                violations.append(
                    {
                        "dimension": "orchestration",
                        "finding": (
                            f"fresh-session resume for {target_name}: the"
                            f" coordinator resumed {claimed} instead of the"
                            f" original {handback['target_sid']}"
                        ),
                        "citation": call["citation"],
                    }
                )
        assigned = next(
            (position for position in handback["matching"] if position not in consumed),
            None,
        )
        if assigned is None:
            resume_evidence_gap = True
            missing.append(
                {
                    "dimension": "orchestration",
                    "finding": (
                        f"no native resume_session call grounds the handback"
                        f" {target_name} in {handback['target_sid']}"
                    ),
                }
            )
            report_item["status"] = "missing-resume-call"
            continue
        consumed.add(assigned)
        call = resume_calls[assigned]
        report_item["resume_call"] = {
            "citation": call["citation"],
            "call_id": call["call_id"],
            "tool": call["tool"],
            "resume_session": call.get("resume_session"),
            "task": call.get("task"),
        }
        task = call.get("task")
        if not isinstance(task, str) or not task.strip():
            handback_violation = True
            violations.append(
                {
                    "dimension": "orchestration",
                    "finding": (f"resume call for {target_name} has no nonempty task"),
                    "citation": call["citation"],
                }
            )
        elif expected_token is not None:
            folded = task.casefold()
            if expected_token.casefold() not in folded:
                handback_violation = True
                wrong = [
                    token for token in scripted_blockers if token.casefold() in folded
                ]
                violations.append(
                    {
                        "dimension": "orchestration",
                        "finding": (
                            f"resume task for {target_name} references"
                            f" {' or '.join(wrong)} instead of resolved"
                            f" blocker {expected_token}"
                            if wrong
                            else (
                                f"resume task for {target_name} does not"
                                f" reference resolved blocker {expected_token}"
                            )
                        ),
                        "citation": call["citation"],
                    }
                )
        helper_texts = (
            observed["texts"].get(str(handback["helper_sid"]), [])
            if isinstance(handback["helper_sid"], str)
            else []
        )
        if not helper_texts:
            resume_evidence_gap = True
            handback_gap = True
            missing.append(
                {
                    "dimension": "orchestration",
                    "finding": (
                        f"helper {helper_name} has no native completion"
                        f" evidence before the resume call for {target_name}"
                    ),
                }
            )
            report_item["helper_completion_citation"] = None
        else:
            completion = max(helper_texts, key=lambda item: item["order"])
            report_item["helper_completion_citation"] = completion["citation"]
            if call["order"] <= completion["order"]:
                handback_violation = True
                violations.append(
                    {
                        "dimension": "orchestration",
                        "finding": (
                            f"resume call for {target_name} precedes the"
                            f" native completion of {helper_name}"
                        ),
                        "citation": call["citation"],
                    }
                )
        target_texts = observed["texts"].get(str(handback["target_sid"]), [])
        new_turns = [item for item in target_texts if item["order"] > call["order"]]
        report_item["new_turn_citations"] = [item["citation"] for item in new_turns]
        if not new_turns:
            resume_evidence_gap = True
            handback_gap = True
            missing.append(
                {
                    "dimension": "orchestration",
                    "finding": (
                        f"no new worker report/work in"
                        f" {handback['target_sid']} after the resume call for"
                        f" {target_name}"
                    ),
                }
            )
        elif all(item.get("role") == "user" for item in new_turns):
            handback_violation = True
            violations.append(
                {
                    "dimension": "orchestration",
                    "finding": (
                        f"user-only growth in {handback['target_sid']} after"
                        f" the resume call for {target_name}"
                    ),
                    "citation": call["citation"],
                }
            )
        report_item["status"] = (
            "violations"
            if handback_violation
            else "missing-evidence"
            if handback_gap
            else "grounded"
        )
    for handback in handback_specs:
        for position in handback["matching"]:
            if position in consumed:
                continue
            consumed.add(position)
            violations.append(
                {
                    "dimension": "orchestration",
                    "finding": (
                        "overlapping resume calls for handback"
                        f" {observed_steps[handback['target_index']]['name']}"
                        f" in {handback['target_sid']}"
                    ),
                    "citation": resume_calls[position]["citation"],
                }
            )
    orchestration_status = "healthy"
    if any(item["dimension"] == "orchestration" for item in violations):
        orchestration_status = "violations"
    elif not events or script_incomplete or resume_evidence_gap:
        orchestration_status = "incomplete"

    # ---- context shielding dimension ---------------------------------
    artifact_steps = [
        step
        for step in observed_steps
        if step.get("observed") and step["action"] == "artifact"
    ]
    artifact_reports: list[_JSON] = []
    shielding_violations: list[_JSON] = []
    artifact_entries: list[tuple[_JSON, Path]] = []
    for step in artifact_steps:
        entry = step["event"]
        call = entry["tool_call_id"]
        guard = guard_by_call.get(str(call)) if isinstance(call, str) else None
        finding: _JSON = {
            "step": step["name"],
            "session_id": entry["session_id"],
            "worker": entry.get("blueprint"),
            "native_worker": entry.get("_native_worker"),
            "tool_call_id": call,
            "artifact_bytes": entry.get("artifact_bytes"),
            "citation": entry["citation"],
        }
        if guard is None:
            missing.append(
                {
                    "dimension": "context_shielding",
                    "finding": f"no guard-run record for artifact call {call}",
                }
            )
            finding["guard"] = None
            artifact_reports.append(finding)
            continue
        controller = guard.get("controller")
        guard_info = guard.get("guard")
        final = guard.get("final")
        finding["guard"] = {
            "exit_status": guard_info.get("exit_status")
            if isinstance(guard_info, dict)
            else None,
            "timed_out": guard_info.get("timed_out")
            if isinstance(guard_info, dict)
            else None,
            "replacement": guard_info.get("replacement")
            if isinstance(guard_info, dict)
            else None,
            "final_source": final.get("source") if isinstance(final, dict) else None,
            "citation": guard.get("_registry_id")
            or f"{_CITATION}:controller:guard-run:{guard.get('seq')}",
        }
        spill_path = (
            controller.get("spill_path") if isinstance(controller, dict) else None
        )
        finding["spill_path"] = spill_path
        retained = None
        # The spill location is read from the actual guard event, never
        # reconstructed from an instrumentation-specific file-name prefix.
        if (
            isinstance(spill_path, str)
            and spill_path
            and isinstance(entry["session_id"], str)
        ):
            candidate = payload_root / str(entry["session_id"]) / Path(spill_path).name
            if candidate.is_file():
                try:
                    payload = candidate.read_bytes()
                    retained = {
                        "path": candidate.relative_to(raw_root).as_posix(),
                        "bytes": len(payload),
                        "sha256": hashlib.sha256(payload).hexdigest(),
                    }
                except OSError:
                    retained = None
        finding["spill_retained"] = retained
        if (
            retained is not None
            and isinstance(controller, dict)
            and retained["sha256"] != controller.get("sha256")
        ):
            shielding_violations.append(
                {
                    "dimension": "context_shielding",
                    "finding": "retained spill digest does not match the"
                    " controller replacement digest",
                    "citation": entry["citation"],
                }
            )
        if retained is None:
            missing.append(
                {
                    "dimension": "context_shielding",
                    "finding": f"spilled replacement for call {call} was not"
                    " retained before cleanup",
                }
            )
        if not (
            isinstance(controller, dict)
            and controller.get("replacement") is True
            and isinstance(guard_info, dict)
            and guard_info.get("exit_status") == 0
            and guard_info.get("timed_out") is False
            and guard_info.get("replacement") is True
            and isinstance(final, dict)
            and final.get("source") == "guard"
        ):
            shielding_violations.append(
                {
                    "dimension": "context_shielding",
                    "finding": f"artifact at {step['name']} was not replaced by"
                    " the actual guard",
                    "citation": entry["citation"],
                }
            )
        artifact_name = None
        artifacts_dir = controller_root / "artifacts"
        if artifacts_dir.is_dir():
            for candidate in sorted(artifacts_dir.glob("*.json")):
                if f"stage{step['stage']}" in candidate.name:
                    artifact_name = candidate.name
                    break
        artifact_path = artifacts_dir / artifact_name if artifact_name else None
        if artifact_path is not None and artifact_path.is_file():
            finding["artifact"] = {
                "name": artifact_name,
                "bytes": artifact_path.stat().st_size,
            }
            artifact_entries.append((step, artifact_path))
        else:
            missing.append(
                {
                    "dimension": "context_shielding",
                    "finding": f"retained artifact for {step['name']} is missing",
                }
            )
        artifact_reports.append(finding)
    # Raw artifact text must never appear in model-visible transcripts.
    message_texts = [
        (sid, item["citation"], item["text"])
        for sid, items in observed["texts"].items()
        for item in items
    ]
    for _step, artifact_path in artifact_entries:
        try:
            probe = artifact_path.read_bytes()[:_EXPOSURE_PROBE_BYTES]
        except OSError:
            continue
        if len(probe) < 64:
            continue
        text_probe = probe.decode("utf-8", errors="replace")
        for sid, citation, text in message_texts:
            if text_probe in text:
                shielding_violations.append(
                    {
                        "dimension": "context_shielding",
                        "finding": "raw artifact text is present in a"
                        " model-visible transcript",
                        "session_id": sid,
                        "citation": citation,
                    }
                )
    for item in shielding_violations:
        violations.append(item)
    shielding_status = "healthy"
    if shielding_violations:
        shielding_status = "violations"
    elif artifact_steps and any(
        item.get("spill_retained") is None for item in artifact_reports
    ):
        shielding_status = "incomplete"
    elif not artifact_steps:
        shielding_status = "incomplete"

    # ---- evidence/unknown dimension ----------------------------------
    extraction_reports: list[_JSON] = []
    role_grounding: list[_JSON] = []
    for step in observed_steps:
        if not step.get("observed"):
            continue
        entry = step["event"]
        sid = entry["session_id"]
        session = native.get(sid) if isinstance(sid, str) else None
        native_worker = session.get("worker") if session else None
        grounded = native_worker is not None and native_worker == entry.get("blueprint")
        role_grounding.append(
            {
                "step": step["name"],
                "session_id": sid,
                "controller_blueprint": entry.get("blueprint"),
                "native_worker": native_worker,
                "status": "grounded" if grounded else "unknown",
                "citation": entry["citation"],
            }
        )
        if native_worker is None:
            unknown.append(
                {
                    "dimension": "evidence_unknown",
                    "finding": f"no native session evidence grounds"
                    f" {step['name']} session {sid}",
                }
            )
        elif native_worker != entry.get("blueprint"):
            violations.append(
                {
                    "dimension": "evidence_unknown",
                    "finding": "controller-recorded role is not the native"
                    f" session owner: {step['name']} claims"
                    f" {entry.get('blueprint')}, native evidence says"
                    f" {native_worker}",
                    "citation": entry["citation"],
                }
            )
    for step, artifact_path in artifact_entries:
        facts, fact_problems = _artifact_facts(artifact_path)
        for problem in fact_problems:
            missing.append({"dimension": "evidence_unknown", "finding": problem})
        if not facts:
            missing.append(
                {
                    "dimension": "evidence_unknown",
                    "finding": f"artifact for {step['name']} carries no"
                    " distributed facts",
                }
            )
        consumer = step["event"]["session_id"]
        children = [
            session
            for session_id, session in native.items()
            if session.get("parent_session_id") == consumer
            and session.get("worker") == "routine"
        ]
        spawns = [
            call
            for call in observed["calls"]
            if call["session_id"] == consumer and call["worker"] == "routine"
        ]
        report: _JSON = {
            "step": step["name"],
            "consumer_session_id": consumer,
            "child_sessions": [
                {"session_id": child["session_id"], "worker": child.get("worker")}
                for child in children
            ],
            "spawn_calls": [
                {"call_id": call["call_id"], "citation": call["citation"]}
                for call in spawns
            ],
            "required_facts": facts,
        }
        if len(children) > 1 or len(spawns) > 1:
            violations.append(
                {
                    "dimension": "evidence_unknown",
                    "finding": f"more than one extraction child for guard at"
                    f" {step['name']}",
                    "citations": [call["citation"] for call in spawns],
                }
            )
        if not children:
            unknown.append(
                {
                    "dimension": "evidence_unknown",
                    "finding": f"no native extraction child observed for the"
                    f" guard at {step['name']}",
                }
            )
            report["matched_facts"] = []
            report["facts_present"] = None
        else:
            child = children[0]
            child_texts = observed["texts"].get(str(child["session_id"]), [])
            joined = "\n".join(item["text"] for item in child_texts)
            matched = [fact for fact in facts if fact in joined]
            report["matched_facts"] = matched
            report["facts_present"] = bool(matched)
            report["report_citations"] = [item["citation"] for item in child_texts]
            report["child_commands"] = observed["commands"].get(
                str(child["session_id"]), []
            )
            report["scope_read_only"] = (
                "unknown: read-only scope is assignment discipline and is"
                " not hard-verifiable from retained evidence"
            )
            if not matched:
                violations.append(
                    {
                        "dimension": "evidence_unknown",
                        "finding": f"extraction child report for {step['name']}"
                        " lacks the required artifact facts",
                    }
                )
        extraction_reports.append(report)
    for fact_problem in [item for item in missing if isinstance(item, dict)]:
        if fact_problem.get("dimension") == "evidence_unknown":
            unknown.append(fact_problem)
    evidence_status = "healthy"
    if any(item["dimension"] == "evidence_unknown" for item in violations):
        evidence_status = "violations"
    elif unknown:
        evidence_status = "incomplete"

    status = "healthy"
    if violations:
        status = "unhealthy"
    elif (
        orchestration_status != "healthy"
        or shielding_status != "healthy"
        or evidence_status != "healthy"
        or missing
    ):
        status = "incomplete"

    resumed_sessions: list[_JSON] = []
    for item in same_session:
        native_session = item["native"]
        if not native_session:
            continue
        sid = str(native_session["session_id"])
        resumed_sessions.append(
            {
                "session_id": sid,
                "worker": native_session.get("worker"),
                "model": native_session.get("model"),
                "parent_session_id": native_session.get("parent_session_id"),
                "native_message_records": len(observed["texts"].get(sid, [])),
            }
        )
    report_json: _JSON = {
        "status": status,
        "scenario": SCENARIO_ID,
        "stages": list(STAGES),
        "expected_sequence": expected,
        "observed_steps": observed_steps,
        "controlled_failures": controlled_failures,
        "failed_attempts": failed_attempts,
        "out_of_script": out_of_script,
        "same_session_resumption": same_session,
        "resumed_sessions": resumed_sessions,
        "paging_calls": observed["paging"],
        "native_resume_calls": [
            {
                "session_id": call["session_id"],
                "tool": call["tool"],
                "call_id": call["call_id"],
                "citation": call["citation"],
                "resume_session": call.get("resume_session"),
                "task": call.get("task"),
            }
            for call in observed["resumes"]
        ],
        "orchestration": {
            "status": orchestration_status,
            "native_resume_grounding": handback_reports,
            "violations": [
                item for item in violations if item["dimension"] == "orchestration"
            ],
            "missing_evidence": [
                item
                for item in missing
                if not isinstance(item, dict)
                or item.get("dimension") == "orchestration"
            ],
        },
        "context_shielding": {
            "status": shielding_status,
            "artifact_events": artifact_reports,
            "violations": [
                item for item in violations if item["dimension"] == "context_shielding"
            ],
            "missing_evidence": [
                item
                for item in missing
                if isinstance(item, dict)
                and item.get("dimension") == "context_shielding"
            ],
        },
        "evidence_unknown": {
            "status": evidence_status,
            "extractions": extraction_reports,
            "role_grounding": role_grounding,
            "unknown": unknown,
            "violations": [
                item for item in violations if item["dimension"] == "evidence_unknown"
            ],
        },
        "note": (
            "Observed orchestration, shielding, and evidence facts only; not"
            " a correctness gate. Paging a child report is never resumption;"
            " each handback needs a native resume call with new worker turns;"
            " unknown or missing evidence stays incomplete, never clean."
        ),
    }
    return report_json, supplemental
