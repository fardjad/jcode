"""Offline fixture tests for the escalation_resumption metric analyzer.

The fixtures mirror the public evidence shape the runner retains: native
session snapshots and messages (records), and the controller state, event
journal, guard journal, artifacts, and spilled replacements under the raw
evidence root. No provider, no live harness, no DSL: plain Python records
and files only.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from jcode_harness_client import escalation_controller
from jcode_harness_client.escalation_resumption import analyze_escalation_resumption

_JSON = dict[str, Any]

SESSIONS: dict[str, tuple[str, str | None]] = {
    "coord-1": ("coordinator", None),
    "routine-1": ("routine", "coord-1"),
    "a2-1": ("advanced", "coord-1"),
    "a3-1": ("advanced", "coord-1"),
    "e3-1": ("expert", "coord-1"),
    "child-a2-1": ("routine", "a2-1"),
    "child-e3-1": ("routine", "e3-1"),
}

STEP_ROWS = [
    # step, name, session, call, stage, kind, blueprint, exit, task, attempt
    (0, "R1-pass", "routine-1", "call-r1", 1, "record", "routine", 0, None, None),
    (1, "R2-blocker", "routine-1", "call-r2", 2, "injected", "routine", None, "B2", 1),
    (1, "R2-blocker", "routine-1", "call-r2b", 2, "injected", "routine", None, "B2", 2),
    (
        2,
        "A2-artifact",
        "a2-1",
        "call-a2a",
        2,
        "artifact-emitted",
        "advanced",
        None,
        None,
        None,
    ),
    (
        3,
        "A2-resolve",
        "a2-1",
        "call-a2r",
        2,
        "resolve-attempt",
        "advanced",
        0,
        None,
        None,
    ),
    (4, "R3-blocker", "routine-1", "call-r3", 3, "injected", "routine", None, "B3", 1),
    (4, "R3-blocker", "routine-1", "call-r3b", 3, "injected", "routine", None, "B3", 2),
    (5, "A3-blocker", "a3-1", "call-a3b", 3, "injected", "advanced", None, "B3A", 1),
    (5, "A3-blocker", "a3-1", "call-a3b2", 3, "injected", "advanced", None, "B3A", 2),
    (
        6,
        "E3-artifact",
        "e3-1",
        "call-e3a",
        3,
        "artifact-emitted",
        "expert",
        None,
        None,
        None,
    ),
    (
        7,
        "E3-resolve",
        "e3-1",
        "call-e3r",
        3,
        "resolve-attempt",
        "expert",
        0,
        None,
        None,
    ),
    (
        8,
        "A3-verify",
        "a3-1",
        "call-a3v",
        3,
        "resolve-attempt",
        "advanced",
        0,
        None,
        None,
    ),
    (
        9,
        "R4-pass",
        "routine-1",
        "call-r4",
        4,
        "resolve-attempt",
        "routine",
        0,
        None,
        None,
    ),
]


def controller_events(
    *,
    sessions: dict[str, str] | None = None,
) -> list[_JSON]:
    """Build the exact scripted controller event journal.

    Every injected blocker appears as exactly two distinct controlled
    failures on fresh native call ids running the two distinct actual
    diagnostic probes (primary, then secondary), with no source edit
    required, all owned by the expected native stage-owner session.
    """

    sessions = sessions or {}
    events: list[_JSON] = []
    for position, row in enumerate(STEP_ROWS):
        (
            step_index,
            name,
            session,
            call,
            stage,
            kind,
            blueprint,
            exit_value,
            task,
            attempt_number,
        ) = row
        session = sessions.get(call, session)
        event: _JSON = {
            "seq": position + 1,
            "recorded_at": "2026-01-01T00:00:00+00:00",
            "event": "post_tool_transform",
            "scenario": "escalation-resumption",
            "kind": kind,
            "session_id": session,
            "tool_call_id": call,
            "blueprint": blueprint,
            "process_role": "worker",
            "stage": stage,
            "step": step_index,
            "original_exit": exit_value,
            "original_evidence": "stage evidence",
            "result_sha256": f"result-{call}",
            "source_sha256": f"source-{position + 1}",
            "step_name": name,
        }
        if task is not None:
            event["task"] = task
            event["synthetic"] = True
        if attempt_number is not None:
            event["attempt_number"] = attempt_number
            event["failure_budget"] = escalation_controller.FAILURE_BUDGET
            event["probe"] = "primary" if attempt_number == 1 else "secondary"
        events.append(event)
    return events


def spill_name(call: str, replacement: str) -> str:
    """Generic native-style spill file name; no instrumentation prefix."""

    digest = hashlib.sha256(replacement.encode("utf-8")).hexdigest()[:16]
    return f"output-{call}-{digest}.json"


def guard_event(
    seq: int, session: str, call: str, replacement: str, **overrides: Any
) -> _JSON:
    event: _JSON = {
        "seq": seq,
        "recorded_at": "2026-01-01T00:00:00+00:00",
        "event": "post-tool-guard",
        "scenario": "escalation-resumption",
        "kind": "guard-run",
        "session_id": session,
        "tool_call_id": call,
        "tool_name": "bash",
        "original": {
            "envelope_bytes": 128,
            "envelope_sha256": "envelope",
            "result_bytes": 48,
            "result_sha256": "original-result",
        },
        "rewritten_envelope_bytes": 256,
        "rewritten_envelope_sha256": "rewritten",
        "controller": {
            "replacement": True,
            "bytes": len(replacement.encode("utf-8")),
            "sha256": hashlib.sha256(replacement.encode("utf-8")).hexdigest(),
            "spill_path": (
                f"/runtime/jcode-tool-payloads/{session}/"
                f"{spill_name(call, replacement)}"
            ),
        },
        "guard": {
            "executable": "delegation-guard",
            "exit_status": 0,
            "timed_out": False,
            "spawn_error": None,
            "stdout_bytes": 96,
            "stdout_sha256": "guard-stdout",
            "stderr_bytes": 0,
            "stderr_sha256": "guard-stderr",
            "replacement": True,
        },
        "final": {"source": "guard", "bytes": 96, "sha256": "final"},
    }
    for key, value in overrides.items():
        section, _, field = key.partition("__")
        if field:
            event[section][field] = value
        else:
            event[section] = value
    return event


def artifact_text(stage: int) -> str:
    task = f"stage{stage}-artifact"
    text, _digest = escalation_controller.build_artifact_text(
        stage=stage, task=task, threshold=512
    )
    return text


def snapshot(sid: str) -> _JSON:
    worker, parent = SESSIONS[sid]
    return {
        "id": f"session:{sid}:snapshot",
        "kind": "session-snapshot",
        "session_id": sid,
        "worker": worker,
        "parent_session_id": parent,
        "data": {
            "session_id": sid,
            "model": f"model-{worker}",
            "worker_blueprint_policy": {"name": worker},
        },
    }


def message(sid: str, blocks: list[_JSON], mid: str, role: str = "assistant") -> _JSON:
    worker = SESSIONS.get(sid, ("routine", None))[0]
    return {
        "id": f"session:{sid}:message:{mid}",
        "kind": "message",
        "session_id": sid,
        "worker": worker,
        "data": {"content": blocks, "role": role},
    }


def resume_call(
    blueprint: str, call_id: str, target: str, task: str, mid: str
) -> _JSON:
    return message(
        "coord-1",
        [
            tool_use(
                f"swarm_{blueprint}",
                call_id,
                {"resume_session": target, "task": task},
            )
        ],
        mid,
    )


def tool_use(name: str, call_id: str, inputs: _JSON) -> _JSON:
    return {"type": "tool_use", "name": name, "id": call_id, "input": inputs}


def text_block(text_value: str) -> _JSON:
    return {"type": "text", "text": text_value}


def native_records(
    *, children_with_facts: bool = True, resumes: bool = True, new_turns: bool = True
) -> list[_JSON]:
    records = [snapshot(sid) for sid in SESSIONS]
    records.append(
        message("routine-1", [text_block("milestone work in progress")], "m1")
    )
    stage2_facts = escalation_controller.STAGE_SPEC_FACTS[2]
    stage3_facts = escalation_controller.STAGE_SPEC_FACTS[3]
    records.append(
        message(
            "a2-1",
            [
                tool_use(
                    "swarm_routine",
                    "spawn-a2",
                    {
                        "task": (
                            "bounded read-only extraction: read the saved"
                            " controller artifact and report the required"
                            " milestone facts"
                        )
                    },
                )
            ],
            "m1",
        )
    )
    child_a2_text = (
        (
            "extraction report with required facts: "
            + stage2_facts[0]
            + " and "
            + stage2_facts[1]
        )
        if children_with_facts
        else "JCODE_EVAL_CHECK marker only"
    )
    records.append(message("child-a2-1", [text_block(child_a2_text)], "m1"))
    # Handback 1 (R after A2): helper completion, coordinator resume call,
    # then a fresh routine turn after the call.
    records.append(
        message(
            "a2-1",
            [text_block("A2 resolved blocker 2 and reported back")],
            "m-a2-done",
        )
    )
    if resumes:
        records.append(
            resume_call(
                "routine",
                "resume-r1",
                "routine-1",
                "resume routine work after B2 resolution",
                "m-resume-r1",
            )
        )
    if resumes and new_turns:
        records.append(
            message("routine-1", [text_block("resumed after assistance")], "m2")
        )
    records.append(
        message(
            "e3-1",
            [
                tool_use(
                    "swarm_routine",
                    "spawn-e3",
                    {"task": "bounded read-only extraction of required facts"},
                )
            ],
            "m1",
        )
    )
    child_e3_text = (
        ("extraction report: " + stage3_facts[0]) if children_with_facts else "no facts"
    )
    records.append(message("child-e3-1", [text_block(child_e3_text)], "m1"))
    # Handback 2 (A3 after E): helper completion, resume, fresh a3 turn.
    records.append(
        message(
            "e3-1",
            [text_block("E3 resolved blocker 3 and reported back")],
            "m-e3-done",
        )
    )
    if resumes:
        records.append(
            resume_call(
                "advanced",
                "resume-a3",
                "a3-1",
                "resume advanced verification after B3 resolution",
                "m-resume-a3",
            )
        )
    if resumes and new_turns:
        records.append(
            message(
                "a3-1",
                [text_block("advanced verification work resumed")],
                "m-a3-work",
            )
        )
    # Handback 3 (R after A3): helper completion, resume, fresh routine turn.
    records.append(
        message(
            "a3-1",
            [text_block("A3 verification complete")],
            "m-a3-done",
        )
    )
    if resumes:
        records.append(
            resume_call(
                "routine",
                "resume-r2",
                "routine-1",
                "resume routine after advanced verification",
                "m-resume-r2",
            )
        )
    if resumes and new_turns:
        records.append(
            message(
                "routine-1",
                [text_block("routine final report after resumption")],
                "m3",
            )
        )
    return records


def controller_state(*, routine: str = "routine-1") -> _JSON:
    return {
        "scenario": "escalation-resumption",
        "step": 10,
        "complete": True,
        "routine_session": routine,
        "a2_session": "a2-1",
        "a3_session": "a3-1",
        "e3_session": "e3-1",
        "blockers": {},
        "processed_calls": {row[3]: index for index, row in enumerate(STEP_ROWS)},
        "step_attempts": {
            str(index): {"count": 2 if spec.get("action") == "inject" else 1}
            for index, spec in enumerate(escalation_controller.STEPS)
        },
        "step_failures": {
            "1": {
                "task": "B2",
                "failures": escalation_controller.FAILURE_BUDGET,
                "probes": {"primary": "call-r2", "secondary": "call-r2b"},
                "last_source_sha256": "source-3",
                "owner_session": routine,
            },
            "4": {
                "task": "B3",
                "failures": escalation_controller.FAILURE_BUDGET,
                "probes": {"primary": "call-r3", "secondary": "call-r3b"},
                "last_source_sha256": "source-7",
                "owner_session": routine,
            },
            "5": {
                "task": "B3A",
                "failures": escalation_controller.FAILURE_BUDGET,
                "probes": {"primary": "call-a3b", "secondary": "call-a3b2"},
                "last_source_sha256": "source-9",
                "owner_session": "a3-1",
            },
        },
        "next_seq": len(STEP_ROWS) + 1,
    }


def write_evidence(
    raw: Path,
    *,
    events: list[_JSON],
    guards: list[_JSON],
    state: _JSON | None,
    artifacts: dict[str, str] | None = None,
    spills: dict[str, str] | None = None,
) -> None:
    (raw / "hook-traces").mkdir(parents=True, exist_ok=True)
    other = raw / "other"
    other.mkdir(parents=True, exist_ok=True)
    (other / "events.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8",
    )
    (other / "guard-events.jsonl").write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in guards),
        encoding="utf-8",
    )
    if state is not None:
        (other / "state.json").write_text(
            json.dumps(state, sort_keys=True), encoding="utf-8"
        )
    for name, content in (artifacts or {}).items():
        target = other / "artifacts"
        target.mkdir(parents=True, exist_ok=True)
        (target / name).write_text(content, encoding="utf-8")
    for relative, content in (spills or {}).items():
        target = raw / "hook-payloads" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def replacement_for(stage: int, call: str) -> str:
    artifact = artifact_text(stage)
    marker = {
        "stage": stage,
        "exit": 1,
        "synthetic": True,
        "controller": "escalation-resumption",
        "kind": "large-diagnostic-artifact",
        "artifact_path": f"artifacts/stage{stage}-{call}.json",
        "artifact_sha256": hashlib.sha256(artifact.encode("utf-8")).hexdigest(),
        "artifact_bytes": len(artifact.encode("utf-8")),
    }
    return (
        artifact
        + "\n"
        + escalation_controller.MARKER_PREFIX
        + " "
        + json.dumps(marker, sort_keys=True)
    )


def positive_fixture(raw: Path, *, events: list[_JSON] | None = None) -> None:
    replacement_a2 = replacement_for(2, "call-a2a")
    replacement_e3 = replacement_for(3, "call-e3a")
    write_evidence(
        raw,
        events=events if events is not None else controller_events(),
        guards=[
            guard_event(1, "a2-1", "call-a2a", replacement_a2),
            guard_event(2, "e3-1", "call-e3a", replacement_e3),
        ],
        state=controller_state(),
        artifacts={
            "stage2-A2-artifact-artifact.json": artifact_text(2),
            "stage3-E3-artifact-artifact.json": artifact_text(3),
        },
        spills={
            "a2-1/" + spill_name("call-a2a", replacement_a2): replacement_a2,
            "e3-1/" + spill_name("call-e3a", replacement_e3): replacement_e3,
        },
    )


def analyze(records: list[_JSON], raw: Path) -> tuple[_JSON, list[_JSON]]:
    return analyze_escalation_resumption(
        records, trace_root=raw / "hook-traces", evidence_gaps=[]
    )


def test_full_scripted_sequence_reports_healthy(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    positive_fixture(raw)
    report, supplemental = analyze(native_records(), raw)
    assert report["status"] == "healthy"
    assert report["orchestration"]["status"] == "healthy"
    assert report["context_shielding"]["status"] == "healthy"
    assert report["evidence_unknown"]["status"] == "healthy"
    assert all(step["observed"] for step in report["observed_steps"])
    routine_identity = next(
        item
        for item in report["same_session_resumption"]
        if item["identity"] == "routine"
    )
    assert routine_identity["consistent"] is True
    assert set(routine_identity["session_ids"]) == {"routine-1"}
    resumed = {item["session_id"]: item for item in report["resumed_sessions"]}
    assert resumed["routine-1"]["worker"] == "routine"
    assert resumed["routine-1"]["native_message_records"] >= 1
    assert report["paging_calls"] == []
    controlled = report["controlled_failures"]
    assert {item["task"] for item in controlled} == {"B2", "B3", "B3A"}
    for item in controlled:
        assert item["observed_failures"] == escalation_controller.FAILURE_BUDGET
        assert item["status"] == "observed"
        assert item["duplicates_excluded"] == 0
        assert [a["attempt_number"] for a in item["attempts"]] == [1, 2]
        assert len({a["tool_call_id"] for a in item["attempts"]}) == 2
        assert [a["probe"] for a in item["attempts"]] == ["primary", "secondary"]
        assert len({a["source_sha256"] for a in item["attempts"]}) >= 1
        assert item["owner_native_worker"] == item["expected_owner_blueprint"]
    b2 = next(item for item in controlled if item["task"] == "B2")
    assert b2["owner_session_id"] == "routine-1"
    b3a = next(item for item in controlled if item["task"] == "B3A")
    assert b3a["owner_session_id"] == "a3-1"
    extractions = report["evidence_unknown"]["extractions"]
    assert len(extractions) == 2
    assert all(item["facts_present"] for item in extractions)
    for extraction in extractions:
        assert len(extraction["child_sessions"]) == 1
        assert extraction["scope_read_only"].startswith("unknown")
    assert report["evidence_unknown"]["role_grounding"] == [
        item
        for item in report["evidence_unknown"]["role_grounding"]
        if item["status"] == "grounded"
    ]
    event_records = {
        record["id"]: record
        for record in supplemental
        if record["kind"] == "controller-event"
    }
    artifact_event = event_records["scenario:escalation-resumption:controller:event:4"]
    assert artifact_event["session_id"] == "a2-1"
    assert artifact_event["worker"] == "advanced"
    guard_ids = {
        record["id"]
        for record in supplemental
        if record["kind"] == "controller-guard-run"
    }
    assert guard_ids == {
        "scenario:escalation-resumption:controller:guard-run:1",
        "scenario:escalation-resumption:controller:guard-run:2",
    }
    assert "scenario:escalation-resumption:controller:state" in {
        record["id"] for record in supplemental
    }
    grounding = report["orchestration"]["native_resume_grounding"]
    assert {item["handback"] for item in grounding} == {
        "R3-blocker after A2-resolve",
        "A3-verify after E3-resolve",
        "R4-pass after A3-verify",
    }
    assert all(item["status"] == "grounded" for item in grounding)
    for item in grounding:
        assert item["resume_call"]["citation"]
        assert item["helper_completion_citation"]
        assert item["new_turn_citations"]
    assert len(report["native_resume_calls"]) == 3
    resume_registry = {
        record["id"]
        for record in supplemental
        if record["kind"] == "native-resume-call"
    }
    assert resume_registry == {
        "scenario:escalation-resumption:native:resume:resume-r1",
        "scenario:escalation-resumption:native:resume:resume-a3",
        "scenario:escalation-resumption:native:resume:resume-r2",
    }


def test_fresh_routine_substitution_is_a_violation(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    events = controller_events(
        sessions={"call-r3": "routine-2", "call-r4": "routine-2"}
    )
    replacement_a2 = replacement_for(2, "call-a2a")
    replacement_e3 = replacement_for(3, "call-e3a")
    write_evidence(
        raw,
        events=events,
        guards=[
            guard_event(1, "a2-1", "call-a2a", replacement_a2),
            guard_event(2, "e3-1", "call-e3a", replacement_e3),
        ],
        state=controller_state(),
        artifacts={
            "stage2-A2-artifact-artifact.json": artifact_text(2),
            "stage3-E3-artifact-artifact.json": artifact_text(3),
        },
        spills={
            "a2-1/" + spill_name("call-a2a", replacement_a2): replacement_a2,
            "e3-1/" + spill_name("call-e3a", replacement_e3): replacement_e3,
        },
    )
    records = native_records()
    records.append(
        {
            "id": "session:routine-2:snapshot",
            "kind": "session-snapshot",
            "session_id": "routine-2",
            "worker": "routine",
            "parent_session_id": "coord-1",
            "data": {
                "session_id": "routine-2",
                "model": "model-routine",
                "worker_blueprint_policy": {"name": "routine"},
            },
        }
    )
    report, _ = analyze(records, raw)
    assert report["status"] == "unhealthy"
    findings = json.dumps(report["orchestration"]["violations"])
    assert "spans multiple sessions" in findings
    assert "routine-2" in findings


def test_paging_a_child_report_is_not_resumption(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    events = controller_events()
    # The run stalls after the A2 resolve: no R3/R4 resumption happens and
    # the coordinator only pages the prior child's retained report.
    incomplete = events[:5]
    replacement_a2 = replacement_for(2, "call-a2a")
    replacement_e3 = replacement_for(3, "call-e3a")
    write_evidence(
        raw,
        events=incomplete,
        guards=[
            guard_event(1, "a2-1", "call-a2a", replacement_a2),
            guard_event(2, "e3-1", "call-e3a", replacement_e3),
        ],
        state=controller_state(),
        artifacts={
            "stage2-A2-artifact-artifact.json": artifact_text(2),
            "stage3-E3-artifact-artifact.json": artifact_text(3),
        },
        spills={
            "a2-1/" + spill_name("call-a2a", replacement_a2): replacement_a2,
            "e3-1/" + spill_name("call-e3a", replacement_e3): replacement_e3,
        },
    )
    records = native_records()
    records.append(
        message(
            "coord-1",
            [
                tool_use(
                    "swarm_routine",
                    "page-1",
                    {"follow_up_session": "routine-1", "offset": 0, "limit": 100},
                )
            ],
            "m-page",
        )
    )
    report, _ = analyze(records, raw)
    assert report["status"] == "incomplete"
    assert report["orchestration"]["status"] == "incomplete"
    assert len(report["paging_calls"]) == 1
    routine_identity = next(
        item
        for item in report["same_session_resumption"]
        if item["identity"] == "routine"
    )
    # Only observed steps contribute session evidence; paging never does.
    assert "routine-1" in routine_identity["session_ids"]


def test_wrong_blocker_task_breaks_the_sequence(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    events = controller_events()
    for event in events:
        if event["step_name"] == "R3-blocker":
            event["task"] = "B9"
    replacement_a2 = replacement_for(2, "call-a2a")
    replacement_e3 = replacement_for(3, "call-e3a")
    write_evidence(
        raw,
        events=events,
        guards=[
            guard_event(1, "a2-1", "call-a2a", replacement_a2),
            guard_event(2, "e3-1", "call-e3a", replacement_e3),
        ],
        state=controller_state(),
        artifacts={
            "stage2-A2-artifact-artifact.json": artifact_text(2),
            "stage3-E3-artifact-artifact.json": artifact_text(3),
        },
        spills={
            "a2-1/" + spill_name("call-a2a", replacement_a2): replacement_a2,
            "e3-1/" + spill_name("call-e3a", replacement_e3): replacement_e3,
        },
    )
    report, _ = analyze(native_records(), raw)
    assert report["status"] == "unhealthy"
    assert report["orchestration"]["status"] == "violations"
    assert "R3-blocker" in json.dumps(report["orchestration"]["violations"])


def test_forged_non_worker_role_never_advances_cleanly(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    events = controller_events()
    for event in events:
        if event["step_name"] == "A3-verify":
            event["process_role"] = "coordinator"
    replacement_a2 = replacement_for(2, "call-a2a")
    replacement_e3 = replacement_for(3, "call-e3a")
    write_evidence(
        raw,
        events=events,
        guards=[
            guard_event(1, "a2-1", "call-a2a", replacement_a2),
            guard_event(2, "e3-1", "call-e3a", replacement_e3),
        ],
        state=controller_state(),
        artifacts={
            "stage2-A2-artifact-artifact.json": artifact_text(2),
            "stage3-E3-artifact-artifact.json": artifact_text(3),
        },
        spills={
            "a2-1/" + spill_name("call-a2a", replacement_a2): replacement_a2,
            "e3-1/" + spill_name("call-e3a", replacement_e3): replacement_e3,
        },
    )
    report, _ = analyze(native_records(), raw)
    assert report["status"] == "unhealthy"
    assert "non-worker role" in json.dumps(report["orchestration"]["violations"])


def test_replayed_tool_call_id_is_flagged(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    events = controller_events()
    # Replaying the first B2 failure's native call id must be flagged and
    # must never fabricate a third counted failure.
    replay = json.loads(json.dumps(events[1]))
    replay["seq"] = len(events) + 1
    events.append(replay)
    positive_fixture(raw, events=events)
    report, _ = analyze(native_records(), raw)
    assert report["status"] == "unhealthy"
    findings = json.dumps(report["orchestration"]["violations"])
    assert "replayed tool call id" in findings
    b2 = next(item for item in report["controlled_failures"] if item["task"] == "B2")
    assert b2["observed_failures"] == 2
    assert b2["status"] == "observed"


def test_out_of_order_event_journal_is_flagged(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    replacement_a2 = replacement_for(2, "call-a2a")
    replacement_e3 = replacement_for(3, "call-e3a")
    write_evidence(
        raw,
        events=list(reversed(controller_events())),
        guards=[
            guard_event(1, "a2-1", "call-a2a", replacement_a2),
            guard_event(2, "e3-1", "call-e3a", replacement_e3),
        ],
        state=controller_state(),
        artifacts={
            "stage2-A2-artifact-artifact.json": artifact_text(2),
            "stage3-E3-artifact-artifact.json": artifact_text(3),
        },
        spills={
            "a2-1/" + spill_name("call-a2a", replacement_a2): replacement_a2,
            "e3-1/" + spill_name("call-e3a", replacement_e3): replacement_e3,
        },
    )
    report, _ = analyze(native_records(), raw)
    assert report["status"] == "unhealthy"
    assert "not strictly monotonic" in json.dumps(report["orchestration"]["violations"])


def test_double_extraction_child_is_a_violation(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    positive_fixture(raw)
    records = native_records()
    records.append(
        {
            "id": "session:child-a2-2:snapshot",
            "kind": "session-snapshot",
            "session_id": "child-a2-2",
            "worker": "routine",
            "parent_session_id": "a2-1",
            "data": {
                "session_id": "child-a2-2",
                "model": "model-routine",
                "worker_blueprint_policy": {"name": "routine"},
            },
        }
    )
    records.append(
        message(
            "a2-1",
            [
                tool_use(
                    "swarm_routine",
                    "spawn-a2-2",
                    {"task": "second extraction attempt"},
                )
            ],
            "m2",
        )
    )
    report, _ = analyze(records, raw)
    assert report["status"] == "unhealthy"
    assert "more than one extraction child" in json.dumps(
        report["evidence_unknown"]["violations"]
    )


def test_failed_guard_replacement_is_not_shielding(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    replacement_a2 = replacement_for(2, "call-a2a")
    replacement_e3 = replacement_for(3, "call-e3a")
    write_evidence(
        raw,
        events=controller_events(),
        guards=[
            guard_event(
                1,
                "a2-1",
                "call-a2a",
                replacement_a2,
                guard__exit_status=1,
                guard__replacement=False,
                final__source="controller",
            ),
            guard_event(2, "e3-1", "call-e3a", replacement_e3),
        ],
        state=controller_state(),
        artifacts={
            "stage2-A2-artifact-artifact.json": artifact_text(2),
            "stage3-E3-artifact-artifact.json": artifact_text(3),
        },
        spills={
            "a2-1/" + spill_name("call-a2a", replacement_a2): replacement_a2,
            "e3-1/" + spill_name("call-e3a", replacement_e3): replacement_e3,
        },
    )
    report, _ = analyze(native_records(), raw)
    assert report["status"] == "unhealthy"
    assert report["context_shielding"]["status"] == "violations"
    assert "not replaced by" in json.dumps(report["context_shielding"]["violations"])


def test_extraction_report_without_required_facts_is_flagged(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    positive_fixture(raw)
    report, _ = analyze(native_records(children_with_facts=False), raw)
    assert report["status"] == "unhealthy"
    assert report["evidence_unknown"]["status"] == "violations"
    assert "lacks the required artifact facts" in json.dumps(
        report["evidence_unknown"]["violations"]
    )


def test_raw_artifact_text_in_parent_transcript_is_exposure(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    positive_fixture(raw)
    records = native_records()
    records.append(
        message(
            "coord-1",
            [text_block("look: " + artifact_text(2)[:600])],
            "m-exposure",
        )
    )
    report, _ = analyze(records, raw)
    assert report["status"] == "unhealthy"
    exposure = json.dumps(report["context_shielding"]["violations"])
    assert "model-visible transcript" in exposure
    assert "session:coord-1:message:m-exposure" in exposure


def test_native_owner_mismatch_fails_role_grounding(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    positive_fixture(raw)
    records = native_records()
    for record in records:
        if record.get("session_id") == "a2-1":
            record["worker"] = "routine"
            record["data"]["worker_blueprint_policy"] = {"name": "routine"}
    report, _ = analyze(records, raw)
    assert report["status"] == "unhealthy"
    findings = json.dumps(report["evidence_unknown"]["violations"])
    assert "not the native session owner" in findings
    grounding = [
        item
        for item in report["evidence_unknown"]["role_grounding"]
        if item["step"] == "A2-artifact"
    ][0]
    assert grounding["native_worker"] == "routine"
    assert grounding["status"] == "unknown"


def test_missing_controller_evidence_is_incomplete_never_clean(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    (raw / "hook-traces").mkdir(parents=True)
    report, supplemental = analyze(native_records(), raw)
    # No journal and no persisted mode exist: empty coverage resolves to
    # blind, never a forced scripted verdict, and stays incomplete.
    assert report["mode"] == "blind"
    assert report["status"] == "incomplete"
    assert report["orchestration"]["status"] == "incomplete"
    missing = json.dumps(report["orchestration"]["missing_evidence"])
    assert "events.jsonl" in missing
    assert "guard-events.jsonl" in missing
    assert "script incomplete" not in json.dumps(report)
    assert report["orchestration"]["violations"] == []
    # No controller records exist, so only native resume-call registry
    # entries may appear; controller citations must stay absent.
    assert {
        record["kind"]
        for record in supplemental
        if record["kind"] != "native-resume-call"
    } == set()


def test_unretained_spill_is_missing_evidence(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    replacement_a2 = replacement_for(2, "call-a2a")
    replacement_e3 = replacement_for(3, "call-e3a")
    write_evidence(
        raw,
        events=controller_events(),
        guards=[
            guard_event(1, "a2-1", "call-a2a", replacement_a2),
            guard_event(2, "e3-1", "call-e3a", replacement_e3),
        ],
        state=controller_state(),
        artifacts={
            "stage2-A2-artifact-artifact.json": artifact_text(2),
            "stage3-E3-artifact-artifact.json": artifact_text(3),
        },
        spills={"e3-1/" + spill_name("call-e3a", replacement_e3): replacement_e3},
    )
    report, _ = analyze(native_records(), raw)
    assert report["status"] == "incomplete"
    assert report["context_shielding"]["status"] == "incomplete"
    assert any(
        item.get("finding", "").startswith("spilled replacement")
        for item in report["context_shielding"]["missing_evidence"]
    )


def test_missing_native_resume_calls_is_incomplete(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    positive_fixture(raw)
    report, supplemental = analyze(native_records(resumes=False), raw)
    assert report["status"] == "incomplete"
    assert report["orchestration"]["status"] == "incomplete"
    findings = json.dumps(report["orchestration"]["missing_evidence"])
    assert "no native resume_session call grounds the handback" in findings
    assert report["native_resume_calls"] == []
    assert all(record["kind"] != "native-resume-call" for record in supplemental)


def test_paging_replacement_never_grounds_a_handback(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    positive_fixture(raw)
    records = native_records(resumes=False)
    records.append(
        message(
            "coord-1",
            [
                tool_use(
                    "swarm_routine",
                    "page-1",
                    {"follow_up_session": "routine-1", "offset": 0, "limit": 100},
                )
            ],
            "m-page",
        )
    )
    report, _ = analyze(records, raw)
    assert report["status"] == "incomplete"
    assert len(report["paging_calls"]) == 1
    assert report["native_resume_calls"] == []
    findings = json.dumps(report["orchestration"]["missing_evidence"])
    assert "no native resume_session call grounds the handback" in findings


def test_stale_old_report_without_new_turns_is_incomplete(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    positive_fixture(raw)
    report, _ = analyze(native_records(new_turns=False), raw)
    assert report["status"] == "incomplete"
    assert report["orchestration"]["status"] == "incomplete"
    findings = json.dumps(report["orchestration"]["missing_evidence"])
    assert "no new worker report/work" in findings
    grounding = report["orchestration"]["native_resume_grounding"]
    assert {item["status"] for item in grounding} == {
        "grounded",
        "missing-evidence",
    }


def test_user_only_growth_after_resume_is_a_violation(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    positive_fixture(raw)
    records = native_records(new_turns=False)
    records.append(
        message(
            "routine-1",
            [text_block("user nudge without worker work")],
            "m-user",
            role="user",
        )
    )
    report, _ = analyze(records, raw)
    assert report["status"] == "unhealthy"
    findings = json.dumps(report["orchestration"]["violations"])
    assert "user-only growth" in findings


def test_fresh_session_resume_is_a_violation(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    positive_fixture(raw)
    records = native_records()
    for record in records:
        blocks = record.get("data", {}).get("content", [])
        for block in blocks:
            if (
                isinstance(block, dict)
                and block.get("id") == "resume-r1"
                and isinstance(block.get("input"), dict)
            ):
                block["input"]["resume_session"] = "routine-2"
    records.append(
        {
            "id": "session:routine-2:snapshot",
            "kind": "session-snapshot",
            "session_id": "routine-2",
            "worker": "routine",
            "parent_session_id": "coord-1",
            "data": {
                "session_id": "routine-2",
                "model": "model-routine",
                "worker_blueprint_policy": {"name": "routine"},
            },
        }
    )
    report, _ = analyze(records, raw)
    assert report["status"] == "unhealthy"
    findings = json.dumps(report["orchestration"]["violations"])
    assert "fresh-session resume" in findings
    assert "routine-2" in findings


def test_wrong_blocker_resume_task_is_a_violation(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    positive_fixture(raw)
    records = native_records()
    for record in records:
        blocks = record.get("data", {}).get("content", [])
        for block in blocks:
            if (
                isinstance(block, dict)
                and block.get("id") == "resume-r1"
                and isinstance(block.get("input"), dict)
            ):
                block["input"]["task"] = "resume routine work after B3 resolution"
    report, _ = analyze(records, raw)
    assert report["status"] == "unhealthy"
    findings = json.dumps(report["orchestration"]["violations"])
    assert "instead of resolved blocker B2" in findings


def test_empty_resume_task_is_a_violation(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    positive_fixture(raw)
    records = native_records()
    for record in records:
        blocks = record.get("data", {}).get("content", [])
        for block in blocks:
            if (
                isinstance(block, dict)
                and block.get("id") == "resume-r1"
                and isinstance(block.get("input"), dict)
            ):
                block["input"]["task"] = "   "
    report, _ = analyze(records, raw)
    assert report["status"] == "unhealthy"
    findings = json.dumps(report["orchestration"]["violations"])
    assert "no nonempty task" in findings


def test_overlapping_resume_calls_are_a_violation(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    positive_fixture(raw)
    records = native_records()
    records.append(
        resume_call(
            "routine",
            "resume-extra",
            "routine-1",
            "resume routine work again after B2 resolution",
            "m-resume-extra",
        )
    )
    report, _ = analyze(records, raw)
    assert report["status"] == "unhealthy"
    findings = json.dumps(report["orchestration"]["violations"])
    assert "overlapping resume calls" in findings


def journal_event(
    seq: int, kind: str, session: str, call: str, stage: int, **extra: Any
) -> _JSON:
    event: _JSON = {
        "seq": seq,
        "recorded_at": "2026-01-01T00:00:00+00:00",
        "event": "post_tool_transform",
        "scenario": "escalation-resumption",
        "kind": kind,
        "session_id": session,
        "tool_call_id": call,
        "blueprint": SESSIONS[session][0],
        "process_role": "worker",
        "stage": stage,
        "original_exit": None,
        "original_evidence": "stage evidence",
        "result_sha256": f"result-{call}",
        "source_sha256": f"source-{seq}",
    }
    event.update(extra)
    return event


def routine_snapshot(sid: str) -> _JSON:
    return {
        "id": f"session:{sid}:snapshot",
        "kind": "session-snapshot",
        "session_id": sid,
        "worker": "routine",
        "parent_session_id": "coord-1",
        "data": {
            "session_id": sid,
            "model": "model-routine",
            "worker_blueprint_policy": {"name": "routine"},
        },
    }


def test_one_failure_with_premature_escalation_is_unhealthy(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    events = controller_events()[:2]
    events.append(
        journal_event(
            3,
            "premature-escalation",
            "a2-1",
            "call-a2x",
            2,
            step=1,
            task="B2",
            note="premature next-tier request rejected",
        )
    )
    positive_fixture(raw, events=events)
    report, _ = analyze(native_records(), raw)
    assert report["status"] == "unhealthy"
    findings = json.dumps(report["orchestration"]["violations"])
    assert "premature next-tier request rejected" in findings
    b2 = next(item for item in report["controlled_failures"] if item["task"] == "B2")
    assert b2["observed_failures"] == 1
    assert b2["status"] == "incomplete"
    missing = json.dumps(report["orchestration"]["missing_evidence"])
    assert "controlled blocker B2" in missing


def test_third_meaningful_retry_is_over_budget_unhealthy(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    events = controller_events()
    events.append(
        journal_event(
            len(events) + 1,
            "over-budget",
            "routine-1",
            "call-r2c",
            2,
            step=9,
            task="B2",
            note="meaningful retry flagged over budget",
        )
    )
    positive_fixture(raw, events=events)
    report, _ = analyze(native_records(), raw)
    assert report["status"] == "unhealthy"
    findings = json.dumps(report["orchestration"]["violations"])
    assert "distinct controlled failures" in findings
    assert "over budget" in findings
    b2 = next(item for item in report["controlled_failures"] if item["task"] == "B2")
    assert b2["observed_failures"] == 3
    assert b2["status"] == "violations"


def test_duplicate_unchanged_retries_do_not_satisfy_budget(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    events = controller_events()[:2]
    for offset, call in enumerate(("call-r2d", "call-r2e")):
        duplicate = journal_event(
            3 + offset,
            "duplicate-attempt",
            "routine-1",
            call,
            2,
            step=1,
            task="B2",
            synthetic=True,
            probe="primary",
        )
        # An already-counted probe rerun (and any no-probe run) is a
        # duplicate that never counts, regardless of source edits.
        events.append(duplicate)
    positive_fixture(raw, events=events)
    report, _ = analyze(native_records(), raw)
    assert report["status"] == "incomplete"
    b2 = next(item for item in report["controlled_failures"] if item["task"] == "B2")
    assert b2["observed_failures"] == 1
    assert b2["duplicates_excluded"] == 2
    assert b2["status"] == "incomplete"
    missing = json.dumps(report["orchestration"]["missing_evidence"])
    assert "controlled blocker B2" in missing


def test_probe_less_second_counting_failure_is_flagged(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    events = controller_events()[:2]
    # A second counting failure that carried no probe identity (here an
    # over-budget retry with no probe) can never satisfy the exact budget.
    events.append(
        journal_event(
            3,
            "over-budget",
            "routine-1",
            "call-r2c",
            2,
            step=1,
            task="B2",
            note="meaningful retry flagged over budget",
        )
    )
    positive_fixture(raw, events=events)
    report, _ = analyze(native_records(), raw)
    assert report["status"] == "unhealthy"
    findings = json.dumps(report["orchestration"]["violations"])
    assert "two distinct actual diagnostic probes" in findings
    b2 = next(item for item in report["controlled_failures"] if item["task"] == "B2")
    assert b2["observed_failures"] == 2
    assert b2["status"] == "violations"


def test_controlled_failure_wrong_owner_is_unhealthy(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    events = controller_events(sessions={"call-r2b": "routine-2"})
    positive_fixture(raw, events=events)
    records = native_records()
    records.append(routine_snapshot("routine-2"))
    report, _ = analyze(records, raw)
    assert report["status"] == "unhealthy"
    findings = json.dumps(report["orchestration"]["violations"])
    assert "foreign session(s)" in findings
    assert "routine-2" in findings
    b2 = next(item for item in report["controlled_failures"] if item["task"] == "B2")
    # The foreign second failure never counts toward the exact budget:
    # only the authoritative first-stage owner's first failure counts.
    assert b2["observed_failures"] == 1
    assert b2["authoritative_owner_session_id"] == "routine-1"
    assert b2["foreign_excluded"] == 1
    assert b2["status"] == "violations"
    missing = json.dumps(report["orchestration"]["missing_evidence"])
    assert "controlled blocker B2" in missing


def test_next_tier_before_second_failure_is_unhealthy(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    rows = controller_events()
    reordered = [rows[0], rows[1], rows[3], rows[2]] + rows[4:]
    for seq, event in enumerate(reordered, start=1):
        event["seq"] = seq
    positive_fixture(raw, events=reordered)
    report, _ = analyze(native_records(), raw)
    assert report["status"] == "unhealthy"
    findings = json.dumps(report["orchestration"]["violations"])
    assert "before the exact 2 controlled failures for B2" in findings


# ---- blind mode ---------------------------------------------------------
#
# Blind journals carry ``mode: blind`` on every event, and the controller
# persists ``mode: blind`` into the evaluator-owned state before any event.
# The scripted journals built above have no mode field in their events or
# state at all: they are the historical explicit-mode evidence, and every
# scripted test above therefore also proves that events with no recorded
# mode anywhere select the unchanged scripted path.


def blind_event(
    seq: int, session: str, call: str, stage: int, kind: str, **overrides: Any
) -> _JSON:
    event: _JSON = {
        "seq": seq,
        "recorded_at": "2026-01-01T00:00:00+00:00",
        "event": "post_tool_transform",
        "scenario": "escalation-resumption",
        "kind": kind,
        "mode": "blind",
        "injected": False,
        "visible": "original",
        "session_id": session,
        "tool_call_id": call,
        "blueprint": "routine",
        "process_role": "worker",
        "stage": stage,
        "probe": None,
        "step": 0,
        "original_exit": 0,
        "original_evidence": "stage evidence",
        "result_sha256": f"result-{call}",
        "source_sha256": f"source-{seq}",
    }
    event.update(overrides)
    return event


def blind_state(*, faults: _JSON | None = None, next_seq: int = 1) -> _JSON:
    return {
        "scenario": "escalation-resumption",
        "step": 0,
        "complete": False,
        "routine_session": None,
        "a2_session": None,
        "a3_session": None,
        "e3_session": None,
        "blockers": {},
        "blind_faults": faults or {},
        "mode": "blind",
        "processed_calls": {},
        "step_attempts": {},
        "step_failures": {},
        "next_seq": next_seq,
    }


def test_blind_alternative_route_and_resume_is_not_a_violation(
    tmp_path: Path,
) -> None:
    """A reasonable alternative tier route is observed, never judged.

    The coordinator delegates the blocked stage to an advanced worker and
    resumes the original routine session: no scripted choreography exists
    in blind mode, so the report must stay healthy.
    """

    raw = tmp_path / "raw"
    events = [
        blind_event(1, "routine-1", "call-1", 1, "check"),
        blind_event(
            2,
            "routine-1",
            "call-2",
            2,
            "fault-activated",
            injected=True,
            visible="replaced",
        ),
        blind_event(3, "a2-1", "call-3", 2, "check", blueprint="advanced"),
        blind_event(
            4,
            "a2-1",
            "call-4",
            2,
            "fault-resolved",
            blueprint="advanced",
        ),
    ]
    faults = {
        "2": {
            "stage": 2,
            "owner_session": "routine-1",
            "injected": {"routine-1": ["call-2"]},
            "fresh_sessions": [],
            "resolved": True,
            "resolved_by": "a2-1",
            "resolved_call": "call-4",
        }
    }
    write_evidence(
        raw,
        events=events,
        guards=[],
        state=blind_state(faults=faults, next_seq=5),
    )
    records = [snapshot(sid) for sid in ("coord-1", "routine-1", "a2-1")]
    records.append(
        message(
            "coord-1",
            [
                tool_use(
                    "swarm_routine",
                    "spawn-1",
                    {"task": "implement milestone 2"},
                )
            ],
            "m-spawn-1",
        )
    )
    records.append(
        message(
            "coord-1",
            [
                tool_use(
                    "swarm_advanced",
                    "spawn-2",
                    {"task": "investigate the stage 2 failure"},
                )
            ],
            "m-spawn-2",
        )
    )
    records.append(
        resume_call(
            "routine",
            "resume-1",
            "routine-1",
            "resume the milestone work after assistance",
            "m-resume-1",
        )
    )
    report, _ = analyze(records, raw)
    assert report["mode"] == "blind"
    assert report["status"] == "healthy", json.dumps(report)
    assert report["orchestration"]["violations"] == []
    coverage = report["behavior_coverage"]
    assert coverage["worker_delegation"]["exercised"] is True
    assert coverage["higher_tier_assistance"]["exercised"] is True
    assert coverage["coordinator_resume_handoff"]["exercised"] is True
    assert coverage["same_session_resumption"]["exercised"] is True
    timeline = report["fault_timeline"]
    assert [item["kind"] for item in timeline] == [
        "fault-activated",
        "fault-resolved",
    ]
    assert timeline[0]["native_worker"] == "routine"
    assert timeline[1]["native_worker"] == "advanced"
    faults_report = report["blind_faults"]
    assert faults_report[0]["owner_native_worker"] == "routine"
    assert faults_report[0]["resolved_native_worker"] == "advanced"


def test_blind_finishing_without_escalation_is_not_a_violation(
    tmp_path: Path,
) -> None:
    """Unexercised coverage is reported, never a violation or a failure."""

    raw = tmp_path / "raw"
    events = [
        blind_event(1, "routine-1", "call-1", 1, "check"),
        blind_event(2, "routine-1", "call-2", 2, "check"),
        blind_event(3, "routine-1", "call-3", 3, "check"),
        blind_event(4, "routine-1", "call-4", 4, "check"),
    ]
    write_evidence(raw, events=events, guards=[], state=blind_state(next_seq=5))
    records = [snapshot(sid) for sid in ("coord-1", "routine-1")]
    records.append(
        message(
            "coord-1",
            [
                tool_use(
                    "swarm_routine",
                    "spawn-1",
                    {"task": "implement the four milestones"},
                )
            ],
            "m-spawn-1",
        )
    )
    report, _ = analyze(records, raw)
    assert report["mode"] == "blind"
    assert report["status"] == "healthy", json.dumps(report)
    assert report["fault_timeline"] == []
    coverage = report["behavior_coverage"]
    assert coverage["worker_delegation"]["exercised"] is True
    assert coverage["higher_tier_assistance"]["exercised"] is False
    assert coverage["coordinator_resume_handoff"]["exercised"] is False
    assert coverage["same_session_resumption"]["exercised"] is False
    assert report["orchestration"]["violations"] == []
    assert "never a violation" in coverage["note"]


def test_blind_missing_identity_stays_unknown_and_is_never_claimed(
    tmp_path: Path,
) -> None:
    """A controller event without native session evidence is unknown.

    The adversarial journal names a session that appears in no native
    record: the timeline must not fabricate a worker or model for it, and
    the missing evidence keeps the report incomplete, not unhealthy.
    """

    raw = tmp_path / "raw"
    events = [
        blind_event(1, "ghost-1", "call-ghost", 2, "fault-activated"),
        blind_event(2, "ghost-1", "call-ghost-2", 2, "check"),
    ]
    write_evidence(
        raw,
        events=events,
        guards=[],
        state=blind_state(
            faults={
                "2": {
                    "stage": 2,
                    "owner_session": "ghost-1",
                    "injected": {"ghost-1": ["call-ghost"]},
                    "fresh_sessions": [],
                }
            },
            next_seq=3,
        ),
    )
    records = [snapshot("coord-1")]
    report, _ = analyze(records, raw)
    assert report["mode"] == "blind"
    assert report["status"] == "incomplete", json.dumps(report)
    assert report["status"] != "unhealthy"
    entry = report["fault_timeline"][0]
    assert entry["session_id"] == "ghost-1"
    assert entry["native_worker"] is None
    assert entry["native_model"] is None
    assert entry["identity"] == "unknown: no native session evidence"
    fault = report["blind_faults"][0]
    assert fault["owner_native_worker"] is None
    unknown = json.dumps(report["evidence_unknown"]["unknown"])
    assert "no native session evidence" in unknown


def test_blind_artifact_exposure_in_transcript_is_a_violation(
    tmp_path: Path,
) -> None:
    """A masked non-initial investigation attaches a large artifact.

    The artifact is deterministic, so it can be re-derived from the
    recorded byte size; if it appears raw in model-visible transcript
    text the report must flag it. The same artifact kept out of the
    transcripts stays healthy.
    """

    artifact = escalation_controller._blind_artifact_text(2, 512)
    event = blind_event(
        2,
        "a2-1",
        "call-artifact",
        2,
        "injected",
        blueprint="advanced",
        injected=True,
        visible="replaced",
        artifact_bytes=len(artifact.encode("utf-8")),
        artifact_sha256=hashlib.sha256(artifact.encode("utf-8")).hexdigest(),
    )
    events = [blind_event(1, "routine-1", "call-1", 2, "check"), event]

    exposed_records = [snapshot(sid) for sid in ("coord-1", "routine-1", "a2-1")]
    exposed_records.append(
        message("coord-1", [text_block(artifact[:600])], "m-exposed")
    )
    raw = tmp_path / "raw"
    write_evidence(raw, events=events, guards=[], state=blind_state(next_seq=3))
    report, _ = analyze(exposed_records, raw)
    assert report["status"] == "unhealthy"
    entry = report["context_shielding"]["artifact_events"][0]
    assert entry["reproducible"] is True
    assert entry["exposed_in_native_transcripts"]
    finding = json.dumps(report["context_shielding"]["violations"])
    assert "exposed raw" in finding

    clean_records = [snapshot(sid) for sid in ("coord-1", "routine-1", "a2-1")]
    raw_clean = tmp_path / "raw-clean"
    write_evidence(raw_clean, events=events, guards=[], state=blind_state(next_seq=3))
    clean_report, _ = analyze(clean_records, raw_clean)
    assert clean_report["status"] == "healthy", json.dumps(clean_report)
    clean_entry = clean_report["context_shielding"]["artifact_events"][0]
    assert clean_entry["reproducible"] is True
    assert clean_entry["exposed_in_native_transcripts"] == []


def tool_result_block(call_id: str, text: str) -> _JSON:
    return {"type": "tool_result", "tool_use_id": call_id, "content": text}


def guard_nudge(saved_path: str) -> str:
    return (
        "Tool result was 8221 bytes, which exceeds the delegation guard"
        " threshold. Use swarm_routine for a bounded, read-only extraction"
        " from the saved result file. If the saved result file path is"
        " missing, report that blocker to the parent instead of retrying"
        f" blindly. Full output is available at: {saved_path}"
    )


SAVED_PAYLOAD_PATH = "/runtime/jcode-tool-payloads/a2-1/output-call-guarded-feed.json"


def test_blind_guard_extraction_correlates_the_exact_saved_payload_path(
    tmp_path: Path,
) -> None:
    """Only the spawn that names the exact saved payload path matches.

    A guard run that replaced a result hands the caller one exact saved
    payload path. The extraction child is the spawn by the same
    immediate parent, after the guarded call, whose native assignment
    names that exact path and whose actual report is retained with the
    required facts. An earlier discovery spawn by the same parent never
    matches, and no route is required or forced.
    """

    raw = tmp_path / "raw"
    events = [
        blind_event(1, "a2-1", "call-guarded", 2, "injected", blueprint="advanced"),
    ]
    guard = guard_event(1, "a2-1", "call-guarded", "replacement")
    write_evidence(raw, events=events, guards=[guard], state=blind_state(next_seq=2))
    facts = escalation_controller.STAGE_SPEC_FACTS[2]
    spill_text = json.dumps(
        {
            "kind": "diagnostic-details",
            "stage": 2,
            "checks": [{"check": 0, "spec_fact": fact} for fact in facts],
        },
        indent=2,
        sort_keys=True,
    )
    (raw / "hook-payloads" / "a2-1").mkdir(parents=True, exist_ok=True)
    (raw / "hook-payloads" / "a2-1" / "output-call-guarded-feed.json").write_text(
        spill_text, encoding="utf-8"
    )
    records = [snapshot(sid) for sid in ("coord-1", "a2-1")]
    # An initial discovery spawn by the same parent, before the guarded
    # call, names the same path but must never match the guard run.
    records.append(
        message(
            "a2-1",
            [
                tool_use(
                    "swarm_routine",
                    "spawn-discovery",
                    {"task": f"discovery of {SAVED_PAYLOAD_PATH}"},
                )
            ],
            "m-discovery",
        )
    )
    # The guarded call's retained result shows the guard's saved path.
    records.append(
        message(
            "a2-1",
            [tool_result_block("call-guarded", guard_nudge(SAVED_PAYLOAD_PATH))],
            "m-guarded-result",
            role="user",
        )
    )
    # The extraction spawn comes after the guarded call and names the
    # exact saved payload path in its native assignment.
    records.append(
        message(
            "a2-1",
            [
                tool_use(
                    "swarm_routine",
                    "spawn-extract",
                    {"task": f"extract facts from {SAVED_PAYLOAD_PATH}"},
                )
            ],
            "m-extract",
        )
    )
    # The child's actual report is retained as the spawn call's result
    # and carries the required spec facts.
    records.append(
        message(
            "a2-1",
            [
                tool_result_block(
                    "spawn-extract",
                    "extraction report: " + " and ".join(facts),
                )
            ],
            "m-extract-result",
            role="user",
        )
    )
    report, _ = analyze(records, raw)
    assert report["status"] == "healthy", json.dumps(report)
    extractions = report["evidence_unknown"]["extractions"]
    assert len(extractions) == 1
    extraction = extractions[0]
    assert extraction["guard_replacement"] is True
    assert extraction["saved_payload_path"] == SAVED_PAYLOAD_PATH
    assert extraction["saved_payload_retained"]["name"] == (
        "output-call-guarded-feed.json"
    )
    assert extraction["required_facts"] == list(facts)
    assert [call["call_id"] for call in extraction["spawn_calls"]] == ["spawn-extract"]
    child = extraction["extraction_child"]
    assert child["call_id"] == "spawn-extract"
    assert child["report_present"] is True
    assert child["facts_present"] is True
    assert extraction["report_citations"]
    assert report["evidence_unknown"]["unknown"] == []
    assert report["evidence_unknown"]["violations"] == []


def test_blind_guard_without_replacement_makes_no_extraction_row(
    tmp_path: Path,
) -> None:
    """A replacement-free guard run never requests an extraction.

    It generates no extraction row, no unknown row, and reuses no child:
    the historical run produced one unknown row per non-replacing guard
    run, which reported nothing observable.
    """

    raw = tmp_path / "raw"
    events = [blind_event(1, "a2-1", "call-1", 2, "check", blueprint="advanced")]
    guard = guard_event(1, "a2-1", "call-1", "replacement", guard__replacement=False)
    write_evidence(raw, events=events, guards=[guard], state=blind_state(next_seq=2))
    records = [snapshot(sid) for sid in ("coord-1", "a2-1")]
    records.append(
        message(
            "a2-1",
            [tool_result_block("call-1", guard_nudge(SAVED_PAYLOAD_PATH))],
            "m-result",
            role="user",
        )
    )
    records.append(
        message(
            "a2-1",
            [
                tool_use(
                    "swarm_routine",
                    "spawn-extract",
                    {"task": f"extract facts from {SAVED_PAYLOAD_PATH}"},
                )
            ],
            "m-extract",
        )
    )
    report, _ = analyze(records, raw)
    assert report["evidence_unknown"]["extractions"] == []
    assert report["evidence_unknown"]["unknown"] == []
    assert report["evidence_unknown"]["violations"] == []


def test_blind_guard_replacement_without_saved_path_stays_unknown(
    tmp_path: Path,
) -> None:
    """A replaced result with no saved path cannot be correlated.

    The unknown row mirrors the guard's own blocker language; no spawn is
    reused as the extraction child.
    """

    raw = tmp_path / "raw"
    events = [blind_event(1, "a2-1", "call-1", 2, "check", blueprint="advanced")]
    guard = guard_event(1, "a2-1", "call-1", "replacement")
    write_evidence(raw, events=events, guards=[guard], state=blind_state(next_seq=2))
    records = [snapshot(sid) for sid in ("coord-1", "a2-1")]
    records.append(
        message(
            "a2-1",
            [
                tool_result_block(
                    "call-1",
                    "Tool result was 8221 bytes, which exceeds the"
                    " delegation guard threshold. Delegate inspection"
                    " according to the delegation guidance.",
                )
            ],
            "m-result",
            role="user",
        )
    )
    report, _ = analyze(records, raw)
    extraction = report["evidence_unknown"]["extractions"][0]
    assert extraction["saved_payload_path"] is None
    assert extraction["extraction_child"] is None
    unknown = json.dumps(report["evidence_unknown"]["unknown"])
    assert "no saved payload path is retained" in unknown


def test_blind_extraction_spawn_without_exact_path_never_matches(
    tmp_path: Path,
) -> None:
    """A same-parent spawn that never names the saved path is not the child."""

    raw = tmp_path / "raw"
    events = [blind_event(1, "a2-1", "call-1", 2, "check", blueprint="advanced")]
    guard = guard_event(1, "a2-1", "call-1", "replacement")
    write_evidence(raw, events=events, guards=[guard], state=blind_state(next_seq=2))
    records = [snapshot(sid) for sid in ("coord-1", "a2-1")]
    records.append(
        message(
            "a2-1",
            [tool_result_block("call-1", guard_nudge(SAVED_PAYLOAD_PATH))],
            "m-result",
            role="user",
        )
    )
    records.append(
        message(
            "a2-1",
            [
                tool_use(
                    "swarm_routine",
                    "spawn-unrelated",
                    {"task": "unrelated same-parent investigation"},
                )
            ],
            "m-spawn",
        )
    )
    report, _ = analyze(records, raw)
    extraction = report["evidence_unknown"]["extractions"][0]
    assert extraction["spawn_calls"] == []
    assert extraction["extraction_child"] is None
    unknown = json.dumps(report["evidence_unknown"]["unknown"])
    assert "no native extraction child observed for guard run" in unknown


def test_blind_extraction_report_without_required_facts_is_not_the_child(
    tmp_path: Path,
) -> None:
    """A child that never reports the required facts stays unmatched.

    The requirement comes from retained evidence (the saved payload's
    spec facts); the verdict stays observational, never a violation.
    """

    raw = tmp_path / "raw"
    events = [blind_event(1, "a2-1", "call-1", 2, "check", blueprint="advanced")]
    guard = guard_event(1, "a2-1", "call-1", "replacement")
    write_evidence(raw, events=events, guards=[guard], state=blind_state(next_seq=2))
    facts = escalation_controller.STAGE_SPEC_FACTS[2]
    spill_text = json.dumps(
        {
            "kind": "diagnostic-details",
            "stage": 2,
            "checks": [{"check": 0, "spec_fact": fact} for fact in facts],
        },
        indent=2,
        sort_keys=True,
    )
    (raw / "hook-payloads" / "a2-1").mkdir(parents=True, exist_ok=True)
    (raw / "hook-payloads" / "a2-1" / "output-call-guarded-feed.json").write_text(
        spill_text, encoding="utf-8"
    )
    records = [snapshot(sid) for sid in ("coord-1", "a2-1")]
    records.append(
        message(
            "a2-1",
            [tool_result_block("call-1", guard_nudge(SAVED_PAYLOAD_PATH))],
            "m-result",
            role="user",
        )
    )
    records.append(
        message(
            "a2-1",
            [
                tool_use(
                    "swarm_routine",
                    "spawn-extract",
                    {"task": f"extract facts from {SAVED_PAYLOAD_PATH}"},
                )
            ],
            "m-extract",
        )
    )
    records.append(
        message(
            "a2-1",
            [tool_result_block("spawn-extract", "no requested facts here")],
            "m-report",
            role="user",
        )
    )
    report, _ = analyze(records, raw)
    extraction = report["evidence_unknown"]["extractions"][0]
    child = extraction["spawn_calls"][0]
    assert child["report_present"] is True
    assert child["facts_present"] is False
    assert extraction["extraction_child"] is None
    assert report["evidence_unknown"]["violations"] == []


def test_blind_duplicate_seq_records_keep_distinct_deterministic_ids(
    tmp_path: Path,
) -> None:
    """Historical duplicate seq identifiers never collapse registry ids.

    Every record is preserved: the first occurrence keeps the seq-only id
    and the duplicate gets a deterministic suffix from its native call id.
    The native historical seq is never renumbered, and the ambiguity
    itself stays reported.
    """

    raw = tmp_path / "raw"
    events = [
        blind_event(5, "a2-1", "call-5", 2, "check", blueprint="advanced"),
        blind_event(6, "a2-1", "call-6a", 2, "injected", blueprint="advanced"),
        blind_event(6, "a2-1", "call-6b", 2, "injected", blueprint="advanced"),
    ]
    write_evidence(raw, events=events, guards=[], state=blind_state(next_seq=7))
    records = [snapshot(sid) for sid in ("coord-1", "a2-1")]
    report, supplemental = analyze(records, raw)
    event_records = [
        record
        for record in supplemental
        if record["kind"] == "controller-event" and record["data"]["seq"] == 6
    ]
    assert len(event_records) == 2
    assert {record["id"] for record in event_records} == {
        "scenario:escalation-resumption:controller:event:6",
        "scenario:escalation-resumption:controller:event:6:call:call-6b",
    }
    # The native historical seq is preserved, never renumbered.
    assert all(record["data"]["seq"] == 6 for record in event_records)
    # The ambiguity itself stays reported.
    findings = json.dumps(report["orchestration"]["violations"])
    assert "not strictly monotonic" in findings
    assert "[6]" in findings


def test_private_access_is_reported_observed_or_uncertain(tmp_path: Path) -> None:
    """Reading the controller's private journal is observed private access.

    Without such a retained native command the status is uncertain; it is
    never claimed as verified blindness, and context shielding of raw
    artifacts stays a distinct fact.
    """

    raw = tmp_path / "raw"
    events = [blind_event(1, "a2-1", "call-1", 2, "check", blueprint="advanced")]
    write_evidence(raw, events=events, guards=[], state=blind_state(next_seq=2))
    records = [snapshot(sid) for sid in ("coord-1", "a2-1")]
    report, _ = analyze(records, raw)
    assert report["private_access"]["status"] == "uncertain"
    assert report["private_access"]["observations"] == []
    assert "never saw" not in json.dumps(report)

    raw_read = tmp_path / "raw-read"
    write_evidence(raw_read, events=events, guards=[], state=blind_state(next_seq=2))
    read_records = [snapshot(sid) for sid in ("coord-1", "a2-1")]
    read_records.append(
        message(
            "a2-1",
            [
                tool_use(
                    "bash",
                    "call-cat",
                    {
                        "command": (
                            "cat /tmp/eval/instrumentation/"
                            "escalation-resumption/events.jsonl"
                        )
                    },
                )
            ],
            "m-cat",
        )
    )
    read_report, _ = analyze(read_records, raw_read)
    access = read_report["private_access"]
    assert access["status"] == "observed"
    assert len(access["observations"]) == 1
    assert access["observations"][0]["native_worker"] == "advanced"
    assert access["observations"][0]["citation"]


def test_explicit_scripted_mode_field_still_selects_scripted_analysis(
    tmp_path: Path,
) -> None:
    """A journal that flags itself scripted keeps the scripted analyzer.

    An explicit ``mode: scripted`` field is honored exactly like the older
    modeless journals: the scripted positive fixture stays healthy.
    """

    raw = tmp_path / "raw"
    events = controller_events()
    for event in events:
        event["mode"] = escalation_controller.MODE_SCRIPTED
    positive_fixture(raw, events=events)
    report, _ = analyze(native_records(), raw)
    assert report["status"] == "healthy"
    assert "mode" not in report
    assert report["expected_sequence"]


def test_zero_event_blind_run_is_incomplete_coverage_not_a_script_violation(
    tmp_path: Path,
) -> None:
    """A blind run that journaled no event is incomplete coverage.

    The controller persists its mode into the evaluator-owned state
    before any event, so an empty journal resolves to blind mode from
    the state and reports unexercised coverage. It is never treated as
    a scripted choreography that failed.
    """

    raw = tmp_path / "raw"
    write_evidence(raw, events=[], guards=[], state=blind_state(next_seq=1))
    report, _ = analyze([snapshot("coord-1")], raw)
    assert report["mode"] == "blind"
    assert report["status"] == "incomplete", json.dumps(report)
    assert report["fault_timeline"] == []
    assert report["behavior_coverage"]["worker_delegation"]["exercised"] is False
    assert report["behavior_coverage"]["higher_tier_assistance"]["exercised"] is False
    findings = json.dumps(report)
    assert "script incomplete" not in findings
    assert report["orchestration"]["violations"] == []
    assert "no events" in json.dumps(report["orchestration"]["missing_evidence"])


def test_persisted_state_mode_is_preferred_over_modeless_events(
    tmp_path: Path,
) -> None:
    """The mode persisted before any event wins over missing event fields.

    Historical modeless scripted events combined with a state that
    records blind dispatch to the observational blind analyzer: the
    state is the authoritative pre-event record of the run's mode.
    """

    raw = tmp_path / "raw"
    replacement_a2 = replacement_for(2, "call-a2a")
    replacement_e3 = replacement_for(3, "call-e3a")
    write_evidence(
        raw,
        events=controller_events(),
        guards=[
            guard_event(1, "a2-1", "call-a2a", replacement_a2),
            guard_event(2, "e3-1", "call-e3a", replacement_e3),
        ],
        state={**controller_state(), "mode": "blind"},
        artifacts={
            "stage2-A2-artifact-artifact.json": artifact_text(2),
            "stage3-E3-artifact-artifact.json": artifact_text(3),
        },
        spills={
            "a2-1/" + spill_name("call-a2a", replacement_a2): replacement_a2,
            "e3-1/" + spill_name("call-e3a", replacement_e3): replacement_e3,
        },
    )
    report, _ = analyze(native_records(), raw)
    assert report["mode"] == "blind"
    assert "expected_sequence" not in report
    assert "script incomplete" not in json.dumps(report)


def test_spill_location_is_read_from_the_recorded_guard_event(
    tmp_path: Path,
) -> None:
    """Retention follows the actually recorded spill path.

    The analyzer reads the spilled artifact location from the guard
    event instead of reconstructing an instrumentation-prefixed file
    name, so whatever location the events record is the retained
    evidence, with no controller prefix anywhere.
    """

    raw = tmp_path / "raw"
    replacement_a2 = replacement_for(2, "call-a2a")
    replacement_e3 = replacement_for(3, "call-e3a")
    write_evidence(
        raw,
        events=controller_events(),
        guards=[
            guard_event(
                1,
                "a2-1",
                "call-a2a",
                replacement_a2,
                controller__spill_path=(
                    "/runtime/jcode-tool-payloads/a2-1/output-recorded-01.json"
                ),
            ),
            guard_event(
                2,
                "e3-1",
                "call-e3a",
                replacement_e3,
                controller__spill_path=(
                    "/runtime/jcode-tool-payloads/e3-1/output-recorded-02.json"
                ),
            ),
        ],
        state=controller_state(),
        artifacts={
            "stage2-A2-artifact-artifact.json": artifact_text(2),
            "stage3-E3-artifact-artifact.json": artifact_text(3),
        },
        spills={
            "a2-1/output-recorded-01.json": replacement_a2,
            "e3-1/output-recorded-02.json": replacement_e3,
        },
    )
    report, _ = analyze(native_records(), raw)
    assert report["status"] == "healthy", json.dumps(report)
    for entry in report["context_shielding"]["artifact_events"]:
        assert entry["spill_retained"] is not None
        assert "controller" not in entry["spill_retained"]["path"]
        assert entry["spill_retained"]["path"].startswith("hook-payloads/")
