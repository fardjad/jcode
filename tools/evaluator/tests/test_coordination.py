from __future__ import annotations

import json
from pathlib import Path

import pytest

from jcode_harness_client.coordination import analyze_coordination


def record(
    sid: str,
    worker: str,
    event: str,
    source: object = None,
    *,
    task_id: object = "repair",
    blocker_id: object = "parse",
    include_task: bool = True,
    include_blocker: bool = True,
    parent: str = "parent",
) -> dict:
    packet = {"event": event}
    if include_task:
        packet["task_id"] = task_id
    if include_blocker:
        packet["blocker_id"] = blocker_id
    if source is not None:
        packet["source_session_id"] = source
    return {
        "id": f"session:{sid}:message:{event}",
        "kind": "message",
        "session_id": sid,
        "parent_session_id": parent,
        "worker": worker,
        "data": {
            "content": [
                {
                    "type": "text",
                    "text": "```coordination\n" + json.dumps(packet) + "\n```",
                }
            ]
        },
    }


def call(
    worker: str,
    *,
    session_id: str = "parent",
    parent_session_id: str | None = None,
    task: str = "repair parse source sessions a b; bounded repair",
) -> dict:
    entry: dict = {
        "id": f"session:{session_id}:message:{worker}",
        "kind": "message",
        "session_id": session_id,
        "worker": "coordinator",
        "data": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "swarm_" + worker,
                    "id": worker,
                    "input": {"task": task},
                }
            ]
        },
    }
    if parent_session_id is not None:
        entry["parent_session_id"] = parent_session_id
    return entry


def analyze(records: list[dict], tmp_path: Path) -> dict:
    return analyze_coordination(records, trace_root=tmp_path, evidence_gaps=[])[0]


def test_correlates_assistance_and_fresh_worker_handback(tmp_path: Path) -> None:
    report = analyze(
        [
            call("builder"),
            call("diagnostician"),
            record("a", "builder", "blocked"),
            record("b", "diagnostician", "assistance", "a"),
            record("c", "builder", "handback", "b"),
        ],
        tmp_path,
    )
    assert report["status"] == "healthy"
    assert [link["status"] for link in report["links"]] == ["correlated", "correlated"]


@pytest.mark.parametrize("source", [None, "missing", "b"])
def test_claim_alone_does_not_prove_escalation(
    tmp_path: Path, source: str | None
) -> None:
    report = analyze([record("b", "helper", "assistance", source)], tmp_path)
    assert report["status"] == "incomplete"
    assert report["links"][0]["status"] == "unknown"


def test_native_call_required_and_repeated_blocker_reported(tmp_path: Path) -> None:
    report = analyze(
        [
            record("a", "builder", "blocked"),
            record("b", "helper", "assistance", "a"),
            record("c", "builder", "blocked"),
        ],
        tmp_path,
    )
    assert report["status"] == "incomplete"
    assert report["repeated_blockers"][0]["blocker_id"] == "parse"


def test_no_escalation_or_small_finish_is_not_a_failure(tmp_path: Path) -> None:
    report = analyze([call("builder"), record("a", "builder", "done")], tmp_path)
    assert report["status"] == "healthy"
    assert report["links"] == []


def test_missing_packets_are_unknown(tmp_path: Path) -> None:
    assert analyze([call("builder")], tmp_path)["status"] == "incomplete"


def test_malformed_packet_is_evidence_gap(tmp_path: Path) -> None:
    item = record("a", "builder", "done")
    item["data"]["content"][0]["text"] = "```coordination\nnot json\n```"
    assert analyze([item], tmp_path)["status"] == "incomplete"


@pytest.mark.parametrize(
    ("task_id", "include_task"),
    [(None, False), ("", True), (17, True), (["repair"], True)],
)
def test_missing_or_malformed_task_identity_is_not_reported_as_a_link(
    tmp_path: Path, task_id: object, include_task: bool
) -> None:
    report = analyze(
        [
            record(
                "a",
                "builder",
                "assistance",
                "missing-source",
                task_id=task_id,
                include_task=include_task,
            )
        ],
        tmp_path,
    )

    assert report["status"] == "incomplete"
    assert report["reported_events"] == []
    assert report["links"] == []


@pytest.mark.parametrize(
    ("blocker_id", "include_blocker"),
    [(None, False), (None, True), (17, True), (["parse"], True)],
)
def test_missing_or_malformed_blocker_identity_keeps_assistance_unknown(
    tmp_path: Path, blocker_id: object, include_blocker: bool
) -> None:
    report = analyze(
        [
            call("helper", task="repair a parse; bounded assistance"),
            record("a", "builder", "blocked"),
            record(
                "b",
                "helper",
                "assistance",
                "a",
                blocker_id=blocker_id,
                include_blocker=include_blocker,
            ),
        ],
        tmp_path,
    )

    assert report["status"] == "incomplete"
    assert report["links"][0]["status"] == "unknown"
    assert report["links"][0]["source_citations"] == []


@pytest.mark.parametrize(("blocker_id", "include_blocker"), [(None, False), (17, True)])
def test_blocked_event_without_valid_blocker_does_not_invent_a_link(
    tmp_path: Path, blocker_id: object, include_blocker: bool
) -> None:
    report = analyze(
        [
            record(
                "a",
                "builder",
                "blocked",
                blocker_id=blocker_id,
                include_blocker=include_blocker,
            )
        ],
        tmp_path,
    )

    # Escalation notes are optional, and this analyzer does not require a
    # blocker ID for an otherwise standalone blocked note.
    assert report["status"] == "healthy"
    assert report["links"] == []


@pytest.mark.parametrize(
    ("task_id", "blocker_id"),
    [("different-task", "parse"), ("repair", "different-blocker")],
)
def test_mismatched_claimed_lineage_does_not_correlate(
    tmp_path: Path, task_id: str, blocker_id: str
) -> None:
    report = analyze(
        [
            call("helper", task=f"{task_id} a {blocker_id}; bounded assistance"),
            record("a", "builder", "blocked"),
            record(
                "b",
                "helper",
                "assistance",
                "a",
                task_id=task_id,
                blocker_id=blocker_id,
            ),
        ],
        tmp_path,
    )

    assert report["status"] == "incomplete"
    assert report["links"][0]["status"] == "unknown"
    assert report["links"][0]["source_citations"] == []


@pytest.mark.parametrize("source", [None, "unobserved-session", 17])
def test_missing_or_unknown_native_source_identity_is_not_fabricated(
    tmp_path: Path, source: object
) -> None:
    report = analyze(
        [
            call("helper"),
            record("b", "helper", "assistance", source),
        ],
        tmp_path,
    )

    assert report["status"] == "incomplete"
    assert report["links"][0]["status"] == "unknown"
    assert report["links"][0]["source_session_id"] == (
        source if isinstance(source, str) else None
    )
    assert report["links"][0]["source_citations"] == []


def test_mismatched_source_session_id_does_not_correlate(
    tmp_path: Path,
) -> None:
    report = analyze(
        [
            call("helper", task="repair parse source sessions a stranger"),
            record("a", "builder", "blocked"),
            record("b", "helper", "assistance", "stranger"),
        ],
        tmp_path,
    )

    assert report["status"] == "incomplete"
    assert report["links"][0]["status"] == "unknown"
    assert report["links"][0]["source_session_id"] == "stranger"
    assert report["links"][0]["source_citations"] == []


@pytest.mark.parametrize(
    ("event", "blocker_id"), [("assistance", 17), ("handback", "")]
)
def test_malformed_blocker_on_link_claim_does_not_correlate(
    tmp_path: Path, event: str, blocker_id: object
) -> None:
    report = analyze(
        [
            record(
                "b",
                "helper",
                event,
                "a",
                blocker_id=blocker_id,
            )
        ],
        tmp_path,
    )

    assert report["status"] == "incomplete"
    assert report["links"][0]["status"] == "unknown"
    assert report["links"][0]["blocker_id"] == blocker_id


@pytest.mark.parametrize(
    "native_call",
    [
        call("other-worker"),
        call("helper", session_id="other-parent"),
        call("helper", task="repair a; bounded assistance"),
    ],
)
def test_mismatched_native_delegation_identity_keeps_link_unknown(
    tmp_path: Path, native_call: dict
) -> None:
    report = analyze(
        [
            native_call,
            record("a", "builder", "blocked"),
            record("b", "helper", "assistance", "a"),
        ],
        tmp_path,
    )

    assert report["status"] == "incomplete"
    assert report["links"][0]["status"] == "unknown"
    assert report["links"][0]["source_citations"] == ["session:a:message:blocked"]


def test_sibling_source_spawn_must_carry_shared_task(tmp_path: Path) -> None:
    report = analyze(
        [
            call("builder", task="different-task only"),
            call("helper", task="targettask sourceid blocker"),
            record(
                "sourceid",
                "builder",
                "blocked",
                task_id="targettask",
                blocker_id="blocker",
            ),
            record(
                "helper",
                "helper",
                "assistance",
                "sourceid",
                task_id="targettask",
                blocker_id="blocker",
            ),
        ],
        tmp_path,
    )

    assert report["status"] == "incomplete"
    assert report["links"][0]["status"] == "unknown"


def test_task_identity_prefix_collision_keeps_link_unknown(tmp_path: Path) -> None:
    report = analyze(
        [
            call("helper", task="task10 parse sessions a; bounded assistance"),
            record("a", "builder", "blocked", task_id="task1"),
            record("b", "helper", "assistance", "a", task_id="task1"),
        ],
        tmp_path,
    )

    # ``task1`` inside ``task10`` is a prefix substring, not the same task.
    assert report["status"] == "incomplete"
    assert report["links"][0]["status"] == "unknown"
    assert report["links"][0]["source_citations"] == ["session:a:message:blocked"]


def test_source_identity_prefix_collision_keeps_link_unknown(
    tmp_path: Path,
) -> None:
    report = analyze(
        [
            call(
                "helper",
                task="repair parse sessions abc123 extended; bounded assistance",
            ),
            record("abc12", "builder", "blocked"),
            record("b", "helper", "assistance", "abc12"),
        ],
        tmp_path,
    )

    # ``abc12`` inside ``abc123`` is a prefix substring, not the same source.
    assert report["status"] == "incomplete"
    assert report["links"][0]["status"] == "unknown"
    assert report["links"][0]["source_session_id"] == "abc12"
    assert report["links"][0]["source_citations"] == ["session:abc12:message:blocked"]


def test_three_level_lineage_correlates_descendant_spawn_and_handback(
    tmp_path: Path,
) -> None:
    task_id = "cap-descendant-lineage"
    blocker_id = "nested-extraction"
    report = analyze(
        [
            call(
                "advanced",
                session_id="root",
                task=f"objective; task_id={task_id} blocker_id=null bounded",
            ),
            call(
                "routine",
                session_id="advanced",
                parent_session_id="root",
                task=f"read-only extraction; task_id={task_id} bounded",
            ),
            record(
                "routine-child",
                "routine",
                "blocked",
                task_id=task_id,
                blocker_id=blocker_id,
                parent="advanced",
            ),
            record(
                "advanced",
                "advanced",
                "assistance",
                "routine-child",
                task_id=task_id,
                blocker_id=blocker_id,
                parent="root",
            ),
            record(
                "routine-child",
                "routine",
                "handback",
                "advanced",
                task_id=task_id,
                blocker_id=blocker_id,
                parent="advanced",
            ),
        ],
        tmp_path,
    )

    assert report["status"] == "healthy", report["missing_evidence"]
    assert [link["status"] for link in report["links"]] == ["correlated", "correlated"]
    spawns = {(spawn["session_id"], spawn["worker"]) for spawn in report["delegations"]}
    assert spawns == {("root", "advanced"), ("advanced", "routine")}
    parents = {
        session["session_id"]: session.get("parent_session_id")
        for session in report["worker_sessions"]
    }
    assert parents["root"] is None
    assert parents["advanced"] == "root"
    assert parents["routine-child"] == "advanced"


def test_three_level_unknown_source_never_fabricates_a_link(tmp_path: Path) -> None:
    task_id = "cap-descendant-lineage"
    report = analyze(
        [
            call(
                "advanced",
                session_id="root",
                task=f"objective; task_id={task_id} bounded",
            ),
            call(
                "routine",
                session_id="advanced",
                parent_session_id="root",
                task=f"read-only extraction; task_id={task_id} bounded",
            ),
            record(
                "routine-child",
                "routine",
                "handback",
                "stranger-session",
                task_id=task_id,
                blocker_id="nested-extraction",
                parent="advanced",
            ),
        ],
        tmp_path,
    )

    # The claimed source is not the native parent, so nothing correlates.
    assert report["status"] == "incomplete"
    assert report["links"][0]["status"] == "unknown"
