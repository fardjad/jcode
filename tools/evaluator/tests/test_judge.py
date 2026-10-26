from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from jcode_harness_client.judge import (
    review_evidence,
    validate_finding,
    write_review_markdown,
)
from jcode_harness_client.judge_projection import (
    build_judge_projection,
    resolve_raw_citations,
)
from jcode_harness_client.redaction import Redactor
from jcode_harness_client.settings import EvaluationLimits, parse_settings


def settings(limits: EvaluationLimits | None = None):
    value = parse_settings(
        """[provider]
type = "openai-compatible"
url = "https://endpoint.invalid/v1"
model = "model"
api_key = "secret-token"
[models]
coordinator = "coordinator-model"
swarm = "swarm-model"
judge = "judge-model"
"""
    )
    return replace(value, limits=limits) if limits else value


def inputs():
    scenario = {
        "citation": "scenario:real:task",
        "id": "real",
        "prompt": "Implement the outcome.",
        "accept": ["python3", "accept.py"],
        "judge_criteria": [
            "Good coordination uses suitable workers and reviews unjustified non-use."
        ],
        "simulated_user": {
            "max_turns": 8,
            "instructions": "Approve reasonable plans and encourage agent decisions.",
        },
    }
    acceptance = {
        "citation": "scenario:real:acceptance",
        "status": "pass",
        "exit_status": 0,
        "stdout": "BULKY ACCEPTANCE STDOUT",
        "stderr": "",
    }
    blueprints = [
        {
            "name": "coordinator",
            "path": "coordinator.md",
            "citation": "blueprint:coordinator",
            "content": "Coordinate and verify.",
        },
        {
            "name": "any-new-worker",
            "path": "renamed.md",
            "citation": "blueprint:any-new-worker",
            "content": "Perform the bounded assignment.",
        },
    ]
    return scenario, acceptance, blueprints


def finding(citation: str = "record-1") -> dict:
    return {
        "kind": "violation",
        "severity": "medium",
        "confidence": "medium",
        "owner": {
            "kind": "worker-blueprint",
            "id": "any-new-worker",
            "path": "renamed.md",
        },
        "observed": "Observed native behavior.",
        "expected": "Expected blueprint behavior.",
        "suggestion": "Make the smallest contract correction.",
        "citations": [citation],
    }


def ownership() -> dict:
    return {
        "blueprints": {
            "coordinator": {"coordinator.md"},
            "any-new-worker": {"renamed.md"},
        },
        "plugins": {"plugin-a": {"plugin-a/executable"}},
        "tools": {"new-tool"},
    }


def test_dynamic_owner_and_citation_validation() -> None:
    accepted = validate_finding(finding(), {"record-1"}, ownership())
    assert accepted["owner"]["id"] == "any-new-worker"
    for mutate in (
        lambda value: value.update(citations=["unknown"]),
        lambda value: value["owner"].update(id="not-in-snapshot"),
        lambda value: value["owner"].update(path="../outside.md"),
        lambda value: value.update(confidence="certain"),
        lambda value: value.update(observed=""),
    ):
        candidate = finding()
        mutate(candidate)
        with pytest.raises(RuntimeError):
            validate_finding(candidate, {"record-1"}, ownership())


def test_projection_excludes_bulky_output_and_keeps_retrievable_citations() -> None:
    scenario, acceptance, _ = inputs()
    raw_records = [
        {"id": scenario["citation"], "kind": "scenario", "data": scenario},
        {"id": acceptance["citation"], "kind": "acceptance", "data": acceptance},
        {
            "id": "scenario:real:changes",
            "kind": "source-diff",
            "data": {"patch": "diff --git a/foo.py b/foo.py\n+new line\n-old line\n"},
        },
        {
            "id": "session:s:request-prompt:0",
            "kind": "request-prompt",
            "session_id": "s",
            "data": {"prompt": "huge duplicated prompt"},
        },
        {
            "id": "session:s:message:m1",
            "kind": "message",
            "session_id": "s",
            "worker": "coordinator",
            "data": {
                "id": "m1",
                "role": "assistant",
                "content": [
                    {"type": "reasoning", "text": "private chain of thought"},
                    {"type": "text", "text": "Delegating to worker."},
                    {
                        "type": "tool_use",
                        "id": "call-1",
                        "name": "agentgrep",
                        "input": {"pattern": "foo"},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "call-1",
                        "is_error": False,
                        "content": "HUGE GREP OUTPUT " * 200,
                    },
                ],
            },
        },
        {
            "id": "hook:rtk:1",
            "kind": "hook-trace",
            "data": {
                "sequence": 1,
                "plugin": "rtk",
                "exit_status": 0,
                "artifacts": {"stdin.bin": {"text": "HUGE HOOK STDIN"}},
            },
        },
    ]
    projection = build_judge_projection(raw_records)
    projected_text = str(projection.records)
    assert "HUGE GREP OUTPUT" not in projected_text
    assert "private chain of thought" not in projected_text
    assert "huge duplicated prompt" not in projected_text
    assert "BULKY ACCEPTANCE STDOUT" not in projected_text
    assert "HUGE HOOK STDIN" not in projected_text
    assert "Delegating to worker." in projected_text
    assert "foo.py" in projected_text
    assert any(
        item["raw_citation"] == "session:s:request-prompt:0"
        for item in projection.excluded
    )
    fetched = resolve_raw_citations(
        ["session:s:message:m1", "session:s:request-prompt:0"],
        projection.raw_by_citation,
    )
    assert len(fetched) == 2
    assert "HUGE GREP OUTPUT" in str(fetched[0])
    with pytest.raises(RuntimeError, match="unknown raw evidence citation"):
        resolve_raw_citations(["unknown:citation"], projection.raw_by_citation)


def test_complete_pagination_retrieval_and_synthesis_cover_every_record(
    tmp_path: Path,
) -> None:
    scenario, acceptance, blueprints = inputs()
    records = [
        {"id": scenario["citation"], "kind": "scenario", "data": scenario},
        {"id": acceptance["citation"], "kind": "acceptance", "data": acceptance},
        {
            "id": "session:s:request-prompt:0",
            "kind": "request-prompt",
            "session_id": "s",
            "data": {"prompt": "full prompt text"},
        },
        *[
            {
                "id": f"record-{index}",
                "kind": "message",
                "session_id": "session_parent",
                "data": {
                    "id": f"m-{index}",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "x" * 100}],
                },
            }
            for index in range(4)
        ],
    ]
    calls: list[dict] = []
    retrieved_once = False

    def response(*args, **kwargs):
        nonlocal retrieved_once
        calls.append(kwargs)
        if kwargs["schema_name"] == "worker_diagnostic_page":
            if not retrieved_once:
                retrieved_once = True
                return {
                    "page_complete": False,
                    "observations": ["need prompt"],
                    "findings": [],
                    "retrieval_requests": ["session:s:request-prompt:0"],
                }
            return {
                "page_complete": True,
                "observations": ["reviewed"],
                "findings": [],
                "retrieval_requests": [],
            }
        return {
            "review_complete": True,
            "reviewed_sessions": ["session_parent"],
            "summary": "Complete harness review.",
            "findings": [],
            "retrieval_requests": [],
        }

    configured = settings(EvaluationLimits(judge_max_calls=20, judge_page_bytes=350))
    with patch("jcode_harness_client.judge.complete", side_effect=response):
        review, coverage = review_evidence(
            settings=configured,
            scenario=scenario,
            records=records,
            evidence_gaps=[],
            blueprints=blueprints,
            plugins=[],
            acceptance=acceptance,
            tool_health={"status": "healthy", "calls": [], "hooks": []},
            workers={
                "exercised": [],
                "not_exercised": ["any-new-worker"],
                "sessions": [{"session_id": "session_parent", "worker": "coordinator"}],
            },
            output=tmp_path,
        )
    assert coverage["complete"]
    assert review["review"] == "clean"
    assert "task_compliance" not in review
    assert coverage["retrieved_citations"] == ["session:s:request-prompt:0"]
    assert len(calls) == len(coverage["pages"]) + 2
    assert coverage["judge_calls"] == len(calls)
    assert all(
        call["common_prefix"]["scenario"]["judge_criteria"]
        == scenario["judge_criteria"]
        for call in calls
    )
    assert all(
        "page" in call["payload"] or "page_reviews" in call["payload"] for call in calls
    )
    assert all(page["status"] == "reviewed" for page in coverage["pages"])
    for path in tmp_path.rglob("*.json"):
        assert "secret-token" not in path.read_text(encoding="utf-8")


def test_budget_exhaustion_is_incomplete_without_provider_calls(tmp_path: Path) -> None:
    scenario, acceptance, blueprints = inputs()
    with patch("jcode_harness_client.judge.complete") as complete:
        review, coverage = review_evidence(
            settings=settings(EvaluationLimits(judge_max_calls=1)),
            scenario=scenario,
            records=[{"id": "record-1", "data": {"text": "evidence"}}],
            evidence_gaps=[],
            blueprints=blueprints,
            plugins=[],
            acceptance=acceptance,
            tool_health={"status": "healthy", "calls": []},
            workers={},
            output=tmp_path,
        )
    complete.assert_not_called()
    assert review["review"] == "incomplete"
    assert "task_compliance" not in review
    assert not coverage["complete"]
    assert coverage["synthesis"] == "not-run-budget"


def test_unknown_citation_or_complete_claim_with_gaps_is_rejected(
    tmp_path: Path,
) -> None:
    scenario, acceptance, blueprints = inputs()

    def unknown_citation(*args, **kwargs):
        if kwargs["schema_name"] == "worker_diagnostic_page":
            return {
                "page_complete": True,
                "observations": [],
                "findings": [finding("not-present")],
                "retrieval_requests": [],
            }
        raise AssertionError("synthesis must not run after invalid page")

    with patch("jcode_harness_client.judge.complete", side_effect=unknown_citation):
        review, coverage = review_evidence(
            settings=settings(),
            scenario=scenario,
            records=[{"id": "record-1", "data": {}}],
            evidence_gaps=[],
            blueprints=blueprints,
            plugins=[],
            acceptance=acceptance,
            tool_health={"status": "healthy", "calls": []},
            workers={},
            output=tmp_path / "unknown",
        )
    assert review["review"] == "incomplete"
    assert "citation" in coverage["error"]

    def unsupported_complete(*args, **kwargs):
        if kwargs["schema_name"] == "worker_diagnostic_page":
            return {
                "page_complete": True,
                "observations": [],
                "findings": [],
                "retrieval_requests": [],
            }
        return {
            "review_complete": True,
            "reviewed_sessions": [],
            "summary": "Unsupported complete claim.",
            "findings": [],
            "retrieval_requests": [],
        }

    with patch("jcode_harness_client.judge.complete", side_effect=unsupported_complete):
        review, coverage = review_evidence(
            settings=settings(),
            scenario=scenario,
            records=[{"id": "record-1", "data": {}}],
            evidence_gaps=["missing worker journal"],
            blueprints=blueprints,
            plugins=[],
            acceptance=acceptance,
            tool_health={"status": "incomplete", "calls": []},
            workers={},
            output=tmp_path / "gaps",
        )
    assert review["review"] == "incomplete"
    assert not coverage["complete"]
    assert "incomplete coverage" in coverage["error"]


def test_omitted_spawned_session_makes_review_incomplete(tmp_path: Path) -> None:
    scenario, acceptance, blueprints = inputs()

    def omit_actor(*args, **kwargs):
        if kwargs["schema_name"] == "worker_diagnostic_page":
            return {
                "page_complete": True,
                "observations": [],
                "findings": [],
                "retrieval_requests": [],
            }
        return {
            "review_complete": True,
            "reviewed_sessions": [],
            "summary": "Incomplete actor review.",
            "findings": [],
            "retrieval_requests": [],
        }

    with patch("jcode_harness_client.judge.complete", side_effect=omit_actor):
        review, coverage = review_evidence(
            settings=settings(),
            scenario=scenario,
            records=[{"id": "record-1", "data": {}}],
            evidence_gaps=[],
            blueprints=blueprints,
            plugins=[],
            acceptance=acceptance,
            tool_health={"status": "healthy", "calls": []},
            workers={
                "sessions": [
                    {"session_id": "session_parent", "worker": "coordinator"},
                    {"session_id": "session_worker", "worker": "any-new-worker"},
                ]
            },
            output=tmp_path,
        )
    assert review["review"] == "incomplete"
    assert not coverage["complete"]
    assert "every coordinator/worker session" in coverage["error"]


def test_markdown_keeps_result_dimensions_and_citations(tmp_path: Path) -> None:
    scenario, acceptance, blueprints = inputs()
    del scenario, blueprints
    review = {
        "summary": "Summary.",
        "findings": [finding()],
        "review": "findings",
    }
    path = tmp_path / "review.md"
    write_review_markdown(
        path,
        review=review,
        acceptance=acceptance,
        tool_health={"status": "issues found", "issues": []},
        workers={"exercised": ["any-new-worker"], "not_exercised": ["other"]},
        coverage={
            "complete": True,
            "evidence_gaps": [],
            "judge_calls": 2,
            "judge_max_calls": 2,
        },
        redactor=Redactor(),
    )
    text = path.read_text(encoding="utf-8")
    assert "`record-1`" in text
    assert "Acceptance: **pass**" in text
    assert "Task compliance" not in text
    assert "Not exercised: other" in text
