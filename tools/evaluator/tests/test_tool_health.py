from __future__ import annotations

import json
from pathlib import Path

from jcode_harness_client.tool_health import analyze_tool_health, select_tool_errors


def records() -> list[dict]:
    return [
        {
            "id": "session:s:request-tools:0",
            "kind": "request-tools",
            "session_id": "s",
            "snapshot_index": 0,
            "data": {
                "tools": [{"name": "new-tool", "input_schema": {"type": "object"}}]
            },
        },
        {
            "id": "session:s:snapshot",
            "kind": "session-snapshot",
            "session_id": "s",
            "data": {"worker_blueprint_policy": {"name": "arbitrary-worker"}},
        },
        {
            "id": "session:s:message:call",
            "kind": "message",
            "session_id": "s",
            "data": {
                "request_snapshot": {"tools": 0},
                "content": [
                    {
                        "type": "tool_use",
                        "id": cid,
                        "name": "new-tool",
                        "input": {"value": 1},
                    }
                    for cid in ["failed", "success"]
                ],
            },
        },
        {
            "id": "session:s:message:result",
            "kind": "message",
            "session_id": "s",
            "data": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": cid,
                        "content": text,
                        "is_error": error,
                    }
                    for cid, text, error in [
                        ("failed", "bad", True),
                        ("success", "ok", False),
                    ]
                ]
            },
        },
    ]


def hook(tmp_path: Path, **extra: object) -> Path:
    trace = tmp_path / "plugin" / "invocation"
    trace.mkdir(parents=True)
    (trace / "trace.json").write_text(
        json.dumps(
            {
                "sequence": 1,
                "plugin": "custom-plugin",
                "context": {"session_id": "s", "tool_call_id": "failed"},
                "exit_status": 9,
                **extra,
            }
        )
    )
    (trace / "stdin.bin").write_text("hook input")
    (trace / "stdout.bin").write_text("hook output")
    (trace / "stderr.bin").write_bytes(b"\xff")
    return trace


def test_only_errors_selected_with_exact_schema_policy_input_and_hook(
    tmp_path: Path,
) -> None:
    hook(tmp_path)
    errors, supplemental, gaps = select_tool_errors(records(), trace_root=tmp_path)
    assert len(errors) == 1
    error = errors[0]
    assert error["session_id"] == "s" and error["call_id"] == "failed"
    assert error["input"] == {"value": 1}
    assert error["effective_schema"] == {"type": "object"}
    assert error["effective_policy"] == {"name": "arbitrary-worker"}
    assert error["error"] == ["bad"]
    assert "tool:s:failed" in error["native_refs"]
    assert error["related_hook_error"][0]["id"] == "hook:custom-plugin:1"
    assert supplemental[0]["data"]["artifacts"]["stdin.bin"]["text"] == "hook input"
    assert supplemental[0]["data"]["artifacts"]["stderr.bin"] == {
        "encoding": "base64",
        "data": "/w==",
    }
    assert not gaps


def test_missing_request_schema_never_uses_unrelated_list_snapshot(
    tmp_path: Path,
) -> None:
    data = records()[1:]
    data.append(
        {
            "id": "list",
            "kind": "effective-tools",
            "session_id": "s",
            "data": {
                "tools": [{"name": "new-tool", "input_schema": {"type": "string"}}]
            },
        }
    )
    errors, _, gaps = select_tool_errors(data, trace_root=tmp_path)
    assert errors[0]["effective_schema"] is None
    assert any("request-specific" in gap["reason"] for gap in gaps)


def test_native_ids_do_not_correlate_other_session_results_or_hooks(
    tmp_path: Path,
) -> None:
    data = records()
    data[-1]["session_id"] = "stranger"
    hook(tmp_path, context={"session_id": "stranger", "tool_call_id": "failed"})
    errors, _, gaps = select_tool_errors(data, trace_root=tmp_path)
    assert len(errors) == 2 and errors[0]["call_id"] is None
    assert any("exact native call context" in g["reason"] for g in gaps)


def test_interrupted_or_unsafe_hook_is_explicit_gap(tmp_path: Path) -> None:
    trace = hook(tmp_path, status="running", exit_status=None)
    report, _ = analyze_tool_health([], trace_root=tmp_path)
    assert report["status"] == "observed"
    assert any("final trace" in g["reason"] for g in report["missing_evidence"])
    (trace / "stdin.bin").unlink()
    (trace / "stdin.bin").symlink_to(tmp_path / "outside")
    errors, supplemental, gaps = select_tool_errors([], trace_root=tmp_path)
    assert supplemental[0]["kind"] == "evidence-gap"
    assert "unsafe" in errors[0]["error"]["error"]
    assert gaps


def test_outer_native_error_retained_without_success_inventory(tmp_path: Path) -> None:
    data = records()[:-1]
    data.append(
        {
            "id": "outer:1",
            "kind": "outer-event",
            "data": {
                "events": [
                    {
                        "ev": "tool_done",
                        "session_id": "s",
                        "call_id": "failed",
                        "is_error": True,
                        "error": "transport failure",
                    }
                ]
            },
        }
    )
    errors, _, _ = select_tool_errors(data, trace_root=tmp_path)
    assert len(errors) == 1 and errors[0]["error"] == ["transport failure"]
    assert "outer:1" in errors[0]["native_refs"]


def test_success_without_context_does_not_create_health_finding(tmp_path: Path) -> None:
    data = records()[2:]
    data[-1]["data"]["content"][0]["is_error"] = False
    report, _ = analyze_tool_health(data, trace_root=tmp_path)
    assert not report["tool_errors"] and not report["missing_evidence"]
