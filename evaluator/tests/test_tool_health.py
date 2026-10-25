from __future__ import annotations

import json
from pathlib import Path

from jcode_harness_client.tool_health import analyze_tool_health


def message_record(
    session: str,
    message_id: str,
    worker: str,
    blocks: list[dict],
    **extra: object,
) -> dict:
    return {
        "id": f"session:{session}:message:{message_id}",
        "kind": "message",
        "session_id": session,
        "worker": worker,
        "data": {"id": message_id, "content": blocks, **extra},
    }


def base_records() -> list[dict]:
    return [
        {
            "id": "session:s:request-tools:0",
            "kind": "request-tools",
            "session_id": "s",
            "snapshot_index": 0,
            "data": {
                "session_id": "s",
                "snapshot_index": 0,
                "tools": [
                    {
                        "name": "newly-added-tool",
                        "input_schema": {"type": "object", "required": ["value"]},
                    }
                ],
            },
        },
        {
            "id": "session:s:snapshot",
            "kind": "session-snapshot",
            "session_id": "s",
            "data": {
                "session_id": "s",
                "worker_blueprint_policy": {
                    "name": "arbitrary-worker",
                    "enabled_tools": ["newly-added-tool"],
                },
            },
        },
    ]


def test_schema_policy_retry_and_hook_facts_stay_correlated(tmp_path: Path) -> None:
    calls = [
        {
            "type": "tool_use",
            "id": "call-1",
            "name": "newly-added-tool",
            "input": {"value": 1},
        },
        {
            "type": "tool_use",
            "id": "call-2",
            "name": "newly-added-tool",
            "input": {"value": 1},
        },
    ]
    records = [
        *base_records(),
        message_record(
            "s",
            "m1",
            "arbitrary-worker",
            calls,
            request_snapshot={"prompt": 0, "tools": 0},
        ),
        message_record(
            "s",
            "m2",
            "arbitrary-worker",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "call-1",
                    "content": "bad",
                    "is_error": True,
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "call-2",
                    "content": "ok",
                    "is_error": False,
                },
            ],
        ),
    ]
    trace = tmp_path / "plugin" / "000000000001-abc"
    trace.mkdir(parents=True)
    (trace / "trace.json").write_text(
        json.dumps(
            {
                "sequence": 1,
                "plugin": "custom-plugin",
                "context": {"session_id": "s", "tool_call_id": "call-1"},
                "exit_status": 9,
            }
        ),
        encoding="utf-8",
    )
    (trace / "stdin.bin").write_text("hook input", encoding="utf-8")
    (trace / "stdout.bin").write_text("hook output", encoding="utf-8")
    (trace / "stderr.bin").write_bytes(b"\xff")
    report, supplemental = analyze_tool_health(records, trace_root=tmp_path)
    assert len(report["calls"]) == 2
    first, second = report["calls"]
    assert first["effective_schema"]["required"] == ["value"]
    assert first["effective_policy"]["name"] == "arbitrary-worker"
    assert first["retry_of"] is None
    assert second["retry_of"] == first["id"]
    assert second["attempt"] == 2
    assert first["native_outcome"] == "error"
    assert second["native_outcome"] == "completed"
    assert first["hook_traces"] == ["hook:custom-plugin:1"]
    assert {issue["kind"] for issue in report["issues"]} == {
        "native-tool-error",
        "hook-nonzero-exit",
    }
    assert report["status"] == "issues found"
    hook_record = next(
        record for record in supplemental if record["kind"] == "hook-trace"
    )
    assert hook_record["data"]["artifacts"]["stdin.bin"]["text"] == "hook input"
    assert hook_record["data"]["artifacts"]["stderr.bin"] == {
        "encoding": "base64",
        "data": "/w==",
    }


def test_interrupted_hook_trace_is_missing_evidence(tmp_path: Path) -> None:
    trace = tmp_path / "plugin" / "000000000001-interrupted"
    trace.mkdir(parents=True)
    (trace / "trace.json").write_text(
        json.dumps(
            {
                "status": "running",
                "sequence": 1,
                "plugin": "custom-plugin",
                "context": {},
            }
        ),
        encoding="utf-8",
    )
    report, _ = analyze_tool_health([], trace_root=tmp_path)
    assert report["status"] == "incomplete"
    assert any(
        "did not reach a final trace" in gap for gap in report["missing_evidence"]
    )


def test_missing_schema_and_result_are_incomplete_not_a_guessed_cause(
    tmp_path: Path,
) -> None:
    records = [
        {
            "id": "session:s:snapshot",
            "kind": "session-snapshot",
            "session_id": "s",
            "data": {"session_id": "s", "worker_blueprint_policy": None},
        },
        message_record(
            "s",
            "m1",
            "unknown",
            [
                {
                    "type": "tool_use",
                    "id": "call-new",
                    "name": "brand-new-id",
                    "input": {},
                }
            ],
        ),
    ]
    report, supplemental = analyze_tool_health(records, trace_root=tmp_path)
    assert report["status"] == "incomplete"
    assert report["calls"][0]["tool"] == "brand-new-id"
    assert report["calls"][0]["effective_schema"] is None
    assert any("tool definition" in gap for gap in report["missing_evidence"])
    assert any(
        "terminal native tool result" in gap for gap in report["missing_evidence"]
    )
    assert not report["issues"]
    assert any(record["kind"] == "evidence-gap" for record in supplemental)
