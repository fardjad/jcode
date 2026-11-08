from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from jcode_harness_client.coordination import analyze_coordination, build_delegation

BLUEPRINT = (
    Path(__file__).resolve().parents[3] / "customizations/workers/blueprints/routine.md"
)
METADATA = yaml.safe_load(BLUEPRINT.read_text().split("---", 2)[1])
OUTPUT = METADATA["output-schema"]
GENERATED = {
    "type": "object",
    "properties": {
        "assignment": METADATA["input-schema"],
        "resume_session": {"type": "string"},
        "follow_up_session": {"type": "string"},
    },
}


def snapshot(sid: str, tier: str, parent: str | None = None) -> dict:
    return {
        "id": f"session:{sid}:snapshot",
        "kind": "session-snapshot",
        "session_id": sid,
        "worker": tier,
        "data": {
            "parent_id": parent,
            "model": "observed-model",
            "reasoning_effort": "high",
            "worker_blueprint_policy": {"name": tier},
        },
    }


def tools(sid: str, tier: str = "builder") -> dict:
    return {
        "id": f"session:{sid}:request-tools:0",
        "kind": "request-tools",
        "session_id": sid,
        "snapshot_index": 0,
        "data": {
            "tools": [
                {"name": f"swarm_{tier}", "input_schema": GENERATED},
                {"name": "submit_report", "input_schema": OUTPUT},
            ]
        },
    }


def message(
    sid: str, mid: str, blocks: list[dict], *, second: int = 1, role: str = "assistant"
) -> dict:
    return {
        "id": f"session:{sid}:message:{mid}",
        "kind": "message",
        "session_id": sid,
        "data": {
            "role": role,
            "timestamp": f"2026-11-08T00:00:{second:02d}Z",
            "request_snapshot": {"tools": 0},
            "content": blocks,
        },
    }


def use(sid: str, cid: str, tool: str, arguments: dict, second: int = 1) -> dict:
    return message(
        sid,
        cid,
        [{"type": "tool_use", "id": cid, "name": tool, "input": arguments}],
        second=second,
    )


def result(
    sid: str,
    cid: str,
    text: str = "Completion report accepted.",
    *,
    error: bool = False,
) -> dict:
    return message(
        sid,
        cid + "-result",
        [
            {
                "type": "tool_result",
                "tool_use_id": cid,
                "content": text,
                "is_error": error,
            }
        ],
        role="user",
        second=50,
    )


def assignment(**extra: object) -> dict:
    controls = {
        key: extra.pop(key)
        for key in ("resume_session", "follow_up_session")
        if key in extra
    }
    return {
        **controls,
        "assignment": {
            "task_id": "repair",
            "blocker_id": None,
            "source_session_id": None,
            "objective": "Repair the parser",
            "mode": "edit",
            "inputs": ["parser.py"],
            "allowed_paths": ["parser.py"],
            "non_goals": [],
            "acceptance": "pytest",
            "time_budget_minutes": 5,
            "attempt_cap": 2,
            **extra,
        },
    }


def report(**extra: object) -> dict:
    return {
        "status": "done",
        "outcome": "Repaired",
        "findings": "evidence\n" * 2000,
        "files_changed": ["parser.py"],
        "unresolved": "",
        "escalation": None,
        "artifacts": ["/workspace/result.txt"],
        "checks": [{"command": "pytest", "exit_code": 0, "result": "passed"}],
        "coordination": {
            "event": "done",
            "task_id": "repair",
            "blocker_id": None,
            "source_session_id": None,
        },
        **extra,
    }


def base() -> list[dict]:
    return [
        snapshot("root", "coordinator"),
        tools("root"),
        snapshot("child", "builder", "root"),
        tools("child"),
        use("root", "spawn", "swarm_builder", assignment()),
        result("root", "spawn", "Worker session: child"),
    ]


def rows(data: dict) -> dict:
    return {r["session"]: r for r in data["delegation"]}


def test_native_schema_report_and_all_material_fields_survive(tmp_path: Path) -> None:
    arguments = report()
    data = build_delegation(
        [
            *base(),
            use("child", "finish", "submit_report", arguments),
            result("child", "finish"),
        ],
        trace_root=tmp_path,
    )
    child = rows(data)["child"]
    assert child == {
        "session": "child",
        "tier": "builder",
        "parent": "root",
        "model": "observed-model",
        "effort": "high",
        "status": "done",
        "resumed": False,
        "checks": arguments["checks"],
        "native_refs": child["native_refs"],
    }
    assert data["assignments"][0]["input"] == assignment()
    assert data["assignments"][0]["input_schema"] == GENERATED
    assert data["reports"][0]["arguments"] == arguments
    assert data["reports"][0]["output_schema"] == OUTPUT
    assert data["reports"][0]["accepted_or_rejected"] == "accepted"
    assert "tool:child:finish" in child["native_refs"]
    assert not data["evidence_gaps"]


@pytest.mark.parametrize(
    "outcome",
    ["rejected", "missing", "wrong-call", "wrong-session", "no-schema", "prose"],
)
def test_only_schema_backed_matching_native_acceptance_sets_status(
    tmp_path: Path, outcome: str
) -> None:
    records = base()
    if outcome == "no-schema":
        records = [r for r in records if r["id"] != "session:child:request-tools:0"]
    records.append(use("child", "finish", "submit_report", report()))
    if outcome != "missing":
        records.append(
            result(
                "stranger" if outcome == "wrong-session" else "child",
                "different" if outcome == "wrong-call" else "finish",
                "invalid schema"
                if outcome == "rejected"
                else "done"
                if outcome == "prose"
                else "Completion report accepted.",
                error=outcome == "rejected",
            )
        )
    data = build_delegation(records, trace_root=tmp_path)
    assert rows(data)["child"]["status"] == rows(data)["child"]["checks"] == "unknown"
    assert data["evidence_gaps"]
    if outcome == "rejected":
        assert data["reports"][0]["accepted_or_rejected"] == "rejected"
        assert data["tool_errors"][0]["input"] == report()
        assert data["tool_errors"][0]["effective_schema"] == OUTPUT


def test_fenced_report_prose_has_no_authority(tmp_path: Path) -> None:
    data = build_delegation(
        [
            *base(),
            message(
                "child",
                "prose",
                [
                    {
                        "type": "text",
                        "text": (
                            '```coordination\n{"event":"done","task_id":"repair"}\n```'
                        ),
                    }
                ],
            ),
        ],
        trace_root=tmp_path,
    )
    assert not data["reports"]
    assert rows(data)["child"]["status"] == "unknown"


def test_rejected_correction_and_unfinished_resume_do_not_reuse_old_status(
    tmp_path: Path,
) -> None:
    records = [
        *base(),
        use("child", "old", "submit_report", report(), 5),
        result("child", "old"),
    ]
    rejected = build_delegation(
        [
            *records,
            use("child", "new", "submit_report", report(), 20),
            result("child", "new", "schema invalid", error=True),
        ],
        trace_root=tmp_path,
    )
    assert rows(rejected)["child"]["status"] == "unknown"
    assert [r["accepted_or_rejected"] for r in rejected["reports"]] == [
        "accepted",
        "rejected",
    ]
    resumed = build_delegation(
        [
            *records,
            use(
                "root",
                "resume",
                "swarm_builder",
                assignment(resume_session="child"),
                10,
            ),
            result("root", "resume", "Worker session: child"),
            message(
                "child", "active", [{"type": "text", "text": "working"}], second=20
            ),
        ],
        trace_root=tmp_path,
    )
    assert rows(resumed)["child"]["resumed"] is True
    assert (
        rows(resumed)["child"]["status"]
        == rows(resumed)["child"]["checks"]
        == "unknown"
    )


def test_rejected_assignment_preserves_original_input_and_correlated_error(
    tmp_path: Path,
) -> None:
    records = base()[:4]
    bad = assignment(attempt_cap=99)
    data = build_delegation(
        [
            *records,
            use("root", "bad", "swarm_builder", bad),
            result("root", "bad", "attempt_cap exceeds maximum", error=True),
        ],
        trace_root=tmp_path,
    )
    assert data["assignments"][0]["input"] == bad
    assert data["assignments"][0]["tool_result_validation"]["outcome"] == "error"
    assert data["assignments"][0]["tool_result_validation"]["target_session_id"] is None
    assert data["tool_errors"][0]["effective_schema"] == GENERATED
    assert data["tool_errors"][0]["input"] == bad


@pytest.mark.parametrize(
    "mode,activity,confirmed,expected",
    [
        ("resume_session", "assistant", True, True),
        ("resume_session", "tool", True, True),
        ("resume_session", "user", True, False),
        ("resume_session", "before", True, False),
        ("resume_session", "no-time", True, False),
        ("resume_session", "assistant", False, False),
        ("follow_up_session", "assistant", True, False),
    ],
)
def test_resume_requires_native_target_and_later_execution_not_paging(
    tmp_path: Path,
    mode: str,
    activity: str,
    confirmed: bool,
    expected: bool,
) -> None:
    blocks = (
        [{"type": "tool_use", "id": "read", "name": "read", "input": {}}]
        if activity == "tool"
        else [{"type": "text", "text": "continuing"}]
    )
    later = message(
        "child",
        "later",
        blocks,
        second=5 if activity == "before" else 30,
        role="user" if activity == "user" else "assistant",
    )
    if activity == "no-time":
        later["data"].pop("timestamp")
    records = [
        *base(),
        use("root", "continue", "swarm_builder", assignment(**{mode: "child"}), 10),
        later,
    ]
    if confirmed:
        records.append(result("root", "continue", "Worker session: child"))
    # Collection is grouped by session, so list position cannot prove chronology.
    records.sort(key=lambda r: r["session_id"])
    data = build_delegation(records, trace_root=tmp_path)
    assert rows(data)["child"]["resumed"] is expected
    assert len(data["delegation"]) == 2


def test_three_levels_use_native_ids_and_exact_structured_lineage(
    tmp_path: Path,
) -> None:
    records = base()
    records[3] = tools("child", "extractor")
    records += [
        snapshot("grandchild", "extractor", "child"),
        tools("grandchild"),
        use("child", "extract", "swarm_extractor", assignment()),
        result("child", "extract", "Worker session: grandchild"),
        use(
            "grandchild",
            "blocked",
            "submit_report",
            report(
                status="blocked",
                coordination={
                    "event": "blocked",
                    "task_id": "repair",
                    "blocker_id": "parse",
                    "source_session_id": None,
                },
            ),
        ),
        result("grandchild", "blocked"),
        use(
            "root",
            "help",
            "swarm_builder",
            assignment(
                resume_session="child",
                blocker_id="parse",
                source_session_id="grandchild",
            ),
            10,
        ),
        result("root", "help", "Worker session: child"),
        use(
            "child",
            "assist",
            "submit_report",
            report(
                coordination={
                    "event": "assistance",
                    "task_id": "repair",
                    "blocker_id": "parse",
                    "source_session_id": "grandchild",
                }
            ),
            30,
        ),
        result("child", "assist"),
    ]
    data = build_delegation(records, trace_root=tmp_path)
    assert rows(data)["grandchild"]["parent"] == "child"
    assert rows(data)["child"]["parent"] == "root"
    assert rows(data)["child"]["resumed"] is True
    assert not data["evidence_gaps"]
    assert "tool:grandchild:blocked" in data["reports"][-1]["native_refs"]
    for field, wrong in [
        ("task_id", "repair-more"),
        ("blocker_id", "parse-more"),
        ("source_session_id", "grandchild-more"),
    ]:
        changed = deepcopy(records)
        changed[-4]["data"]["content"][0]["input"]["assignment"][field] = wrong
        broken = build_delegation(changed, trace_root=tmp_path)
        assert any(
            "exact native assignment/source lineage" in g.get("reason", "")
            for g in broken["evidence_gaps"]
        )


def test_legacy_runner_adapter_has_no_health_gate(tmp_path: Path) -> None:
    data, supplemental = analyze_coordination(
        base(), trace_root=tmp_path, evidence_gaps=[]
    )
    assert data["status"] == "observed"
    assert data["missing_evidence"]
    assert not supplemental


@pytest.mark.parametrize("wrapped", [None, "invalid", {"task_id": "other"}])
def test_flat_fields_cannot_substitute_for_nested_assignment_lineage(
    tmp_path: Path, wrapped: object
) -> None:
    records = base()
    records[-2]["data"]["content"][0]["input"] = {
        **assignment()["assignment"],
        "assignment": wrapped,
    }
    data = build_delegation(
        [
            *records,
            use("child", "finish", "submit_report", report()),
            result("child", "finish"),
        ],
        trace_root=tmp_path,
    )
    assert any(
        "exact native assignment/source lineage" in gap.get("reason", "")
        for gap in data["evidence_gaps"]
    )
