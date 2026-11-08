from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from jcode_harness_client.judge import (
    build_judge_input,
    prepare_judge_input,
    review_compact,
    validate_finding,
)
from jcode_harness_client.coordination import build_delegation
from jcode_harness_client.llm_backend import UsageTracker
from jcode_harness_client.report import render_report
from jcode_harness_client.settings import parse_settings


def settings():
    return parse_settings("""[provider]
type = "openai-compatible"
url = "https://endpoint.invalid/v1"
model = "gpt-4o"
api_key = "secret-token"
""")


def finding():
    return {
        "severity": "medium",
        "category": "blueprint",
        "text": "Assignment omitted a boundary.",
        "citations": ["tool:parent:call"],
        "fix_target": "worker blueprint",
    }


def input_data():
    coordination = {
        "delegation": [
            {
                "session": "child",
                "tier": "new-worker",
                "native_refs": ["session:child:snapshot"],
            }
        ],
        "assignments": [
            {
                "session_id": "parent",
                "call_id": "call",
                "tool": "swarm_new-worker",
                "input_schema": {"type": "object"},
                "input": {"objective": "work"},
                "tool_result_validation": {"outcome": "completed"},
                "native_refs": ["tool:parent:call"],
            }
        ],
        "reports": [
            {
                "session_id": "child",
                "call_id": "report",
                "output_schema": {},
                "arguments": {"findings": "all material facts"},
                "accepted_or_rejected": "accepted",
                "native_refs": ["session:child:message:report"],
            }
        ],
        "tool_errors": [],
        "evidence_gaps": [],
    }
    return build_judge_input(
        scenario={
            "id": "demo",
            "prompt": "fix",
            "judge_criteria": ["honor policy"],
            "unrelated": "ignored",
        },
        acceptance={"status": "pass", "stdout": "bulky output"},
        coordination=coordination,
        coordinator_messages=[
            {
                "session_id": "parent",
                "message_id": "m1",
                "role": "assistant",
                "text": "Delegating.",
            }
        ],
        scenario_extras=[{"journal_event": "injected", "native_refs": ["journal:1"]}],
        blueprints={
            "new-worker": {
                "policy": "honor boundary",
                "input_schema": {},
                "output_schema": {},
            }
        },
        fix_targets={"blueprints": ["worker.md"]},
    )


def registry():
    return {
        key: {}
        for key in (
            "tool:parent:call",
            "session:child:snapshot",
            "session:child:message:report",
            "session:parent:message:m1",
            "journal:1",
            "off-page",
        )
    }


def transport_for(response, requests):
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 123,
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(response),
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 3,
                    "total_tokens": 13,
                    "cost": 0.0,
                },
            },
        )

    return httpx.MockTransport(handler)


def test_single_structured_review_selected_inputs_and_gap(tmp_path: Path):
    data = input_data()
    data["evidence_gaps"] = [{"reason": "worker report missing", "session_id": "child"}]
    requests = []
    tracker = UsageTracker()
    result = review_compact(
        settings=settings(),
        judge_input=data,
        citation_map=registry(),
        output=tmp_path / "judge",
        usage_tracker=tracker,
        transport=transport_for({"findings": [finding()]}, requests),
    )
    assert result["error"] is None
    assert result["findings"][0]["category"] == "evaluator"
    assert result["findings"][0]["citations"] == ["child"]
    assert result["findings"][1] == finding()
    assert len(requests) == 1
    wire = json.dumps(requests[0])
    assert (
        "honor policy" in wire
        and "all material facts" in wire
        and "honor boundary" in wire
    )
    assert "bulky output" not in wire and "unrelated" not in wire
    assert tracker.summary()["cost_usd"] == 0.0


def test_repeated_evidence_gaps_preserve_sessions_and_citations(tmp_path: Path):
    data = input_data()
    data["evidence_gaps"] = [
        {"reason": "context missing", "session_id": "child"},
        {"reason": "context missing", "session_id": "other"},
        {"reason": "different gap", "session_id": "child"},
    ]
    result = review_compact(
        settings=settings(), judge_input=data, citation_map={"child": {}, "other": {}},
        output=tmp_path / "judge", transport=transport_for({"findings": []}, []),
    )
    assert len(result["findings"]) == 2
    assert result["findings"][0]["text"] == "context missing; sessions: child, other"
    assert result["findings"][0]["citations"] == ["child", "other"]


@pytest.mark.parametrize("bad", ["off-page", "not-in-registry"])
def test_citations_must_be_selected_and_registered(tmp_path: Path, bad: str):
    item = finding()
    item["citations"] = [bad]
    requests = []
    result = review_compact(
        settings=settings(),
        judge_input=input_data(),
        citation_map=registry(),
        output=tmp_path / "judge",
        transport=transport_for({"findings": [item]}, requests),
    )
    assert len(requests) == 1
    assert "unknown evidence citation" in result["error"]
    assert result["findings"][-1]["category"] == "evaluator"


def test_invalid_schema_provider_error_and_secrets_are_run_errors(tmp_path: Path):
    requests = []
    item = finding()
    item["category"] = "unsupported"
    result = review_compact(
        settings=settings(),
        judge_input=input_data(),
        citation_map=registry(),
        output=tmp_path / "invalid",
        transport=transport_for({"findings": [item]}, requests),
    )
    assert result["error"] is not None and len(requests) == 1

    def fail(request):
        requests.append({})
        return httpx.Response(400, json={"error": {"message": "secret-token"}})

    result = review_compact(
        settings=settings(),
        judge_input=input_data(),
        citation_map=registry(),
        output=tmp_path / "error",
        transport=httpx.MockTransport(fail),
    )
    assert "HTTP 400" in result["error"]
    assert "secret-token" not in json.dumps(result)
    assert len(requests) == 2


def test_validate_rejects_duplicate_citations_and_blank_fields():
    item = finding()
    item["citations"] *= 2
    with pytest.raises(RuntimeError, match="citation"):
        validate_finding(item, {"tool:parent:call"})
    item = finding()
    item["fix_target"] = "  "
    with pytest.raises(RuntimeError, match="nonempty"):
        validate_finding(item, {"tool:parent:call"})


def test_selected_native_delegation_and_messages(tmp_path):
    records = [
        {
            "id": "session:root:snapshot",
            "kind": "session-snapshot",
            "session_id": "root",
            "worker": "coordinator",
            "data": {"model": "m", "reasoning_effort": "high"},
        },
        {
            "id": "session:root:request-tools:0",
            "kind": "request-tools",
            "session_id": "root",
            "snapshot_index": 0,
            "data": {
                "tools": [{"name": "swarm_builder", "input_schema": {"type": "object"}}]
            },
        },
        {
            "id": "session:child:snapshot",
            "kind": "session-snapshot",
            "session_id": "child",
            "worker": "builder",
            "data": {
                "parent_id": "root",
                "model": "w",
                "reasoning_effort": "low",
                "worker_blueprint_policy": {"name": "builder"},
            },
        },
        {
            "id": "session:root:message:m",
            "kind": "message",
            "session_id": "root",
            "worker": "coordinator",
            "message_id": "m",
            "data": {
                "role": "assistant",
                "request_snapshot": {"tools": 0},
                "content": [
                    {"type": "text", "text": "Delegate."},
                    {"type": "reasoning", "text": "private reasoning"},
                    {
                        "type": "tool_use",
                        "id": "c",
                        "name": "swarm_builder",
                        "input": {"objective": "repair"},
                    },
                ],
            },
        },
        {
            "id": "session:root:message:r",
            "kind": "message",
            "session_id": "root",
            "worker": "coordinator",
            "data": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "c",
                        "content": "Worker session: child",
                        "is_error": False,
                    }
                ],
            },
        },
    ]
    data = prepare_judge_input(
        scenario={"id": "demo"},
        records=records,
        coordination=build_delegation(records, trace_root=tmp_path, evidence_gaps=[]),
        scenario_extras=[],
        blueprints=[
            {
                "name": "builder",
                "path": "builder.md",
                "content": "---\nname: builder\ninput-schema: {type: object}\n"
                "output-schema: {type: object}\n---\nHonor limits.",
            }
        ],
        plugins=[],
        acceptance={"status": "pass"},
    )
    assert (
        data["assignments"][0]["tool_result_validation"]["target_session_id"] == "child"
    )
    assert data["blueprints"]["builder"]["policy"] == "Honor limits."
    assert data["blueprints"]["builder"]["native_policies"] == [{"name": "builder"}]
    assert data["coordinator_messages"][0]["text"] == "Delegate."
    assert "private reasoning" not in json.dumps(data)


def test_report_layout_and_unknown_cost():
    report = {
        "scenario": "demo",
        "acceptance": "pass",
        "cost_usd": None,
        "unpriced_calls": 2,
        "wall": "4s",
        "delegation": [
            {
                "session": "s1",
                "tier": "coordinator",
                "parent": None,
                "model": "m",
                "effort": "high",
                "status": "unknown",
                "resumed": False,
                "checks": "unknown",
            },
            {
                "session": "s2",
                "tier": "worker",
                "parent": "s1",
                "model": "w",
                "effort": "unknown",
                "status": "done",
                "resumed": True,
                "checks": [{"command": "x | y", "exit_code": 0}, {"exit_code": 1}],
            },
        ],
        "time": {
            "build_check": "1s",
            "coordinator_turns": "2s",
            "coordinator_turn_count": 3,
            "simulator": "0s",
            "acceptance": "1s",
            "judge": "1s",
            "report": "0s",
        },
        "effective_effort": {"workers": {"worker": "low"}},
        "scenario_extras": [
            {"journal_event": {"injected": True, "stage": 2}},
            {"journal_event": {"guard": {"replacement": True}}},
            {"journal_event": {"kind": "marker-mismatch"}},
        ],
        "findings": [finding()],
    }
    text = render_report(report)
    assert text.startswith(
        "# demo — acceptance PASS — unknown (2 calls unpriced) — 4s\n\n"
        "Models: coordinator m/high · worker w/low\n"
    )
    assert (
        "## Time\n\nbuild check 1s · coordinator turns 2s (3) · simulator 0s · "
        "acceptance 1s · judge 1s · report 0s" in text
    )
    assert "| s2 | worker | s1 | w | done | yes | 2 (1 failed) |" in text
    assert "x \\| y" not in text
    assert "Injections: 1 (stages 2); guard replacements: 1; marker mismatches: 1\n\n## Findings" in text
    assert (
        "1. [medium · blueprint] Assignment omitted a boundary.\n"
        "   cite: tool:parent:call · fix target: worker blueprint" in text
    )
    assert "incomplete" not in text
    second = {**report, "scenario": "other", "cost_usd": 0.0, "findings": []}
    combined = render_report({"scenarios": [report, second]})
    assert "\n\n# other — acceptance PASS — $0.0" in combined
    assert combined.endswith("## Findings\n\nNone reported.\n")
