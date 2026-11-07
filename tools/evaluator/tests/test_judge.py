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


def multi_citation_finding(citations: list[str]) -> dict:
    value = finding()
    value["citations"] = list(citations)
    return value


def finding_variant(
    *,
    kind: str = "violation",
    severity: str = "medium",
    confidence: str = "medium",
    owner: dict | None = None,
    observed: str = "Observed native behavior.",
    expected: str = "Expected blueprint behavior.",
    suggestion: str = "Make the smallest contract correction.",
    citations: list[str] | None = None,
) -> dict:
    value = finding()
    value["kind"] = kind
    value["severity"] = severity
    value["confidence"] = confidence
    value["observed"] = observed
    value["expected"] = expected
    value["suggestion"] = suggestion
    if owner is not None:
        value["owner"] = owner
    if citations is not None:
        value["citations"] = list(citations)
    return value


def coordinator_blueprint_owner() -> dict:
    return {
        "kind": "coordinator-blueprint",
        "id": "coordinator",
        "path": "coordinator.md",
    }


def message_record(citation: str) -> dict:
    return {
        "id": citation,
        "kind": "message",
        "session_id": "session_parent",
        "worker": "coordinator",
        "data": {
            "id": citation.rsplit(":", 1)[-1],
            "role": "assistant",
            "content": [{"type": "text", "text": "Delegating to worker. " + "e" * 200}],
        },
    }


def coordination_metric_record(
    *,
    delegations: list[str] | None = None,
    reported_events: list[str] | None = None,
    links: list[str] | None = None,
    source_citations: list[str] | None = None,
    repeated_blockers: list[str] | None = None,
    missing_evidence: list[str] | None = None,
) -> dict:
    data: dict = {
        "status": "healthy",
        "worker_sessions": [
            {"session_id": "session_parent", "worker": "coordinator"},
            {
                "session_id": "session_worker",
                "worker": "any-new-worker",
                "parent_session_id": "session_parent",
            },
        ],
        "note": "Correlated facts are not proof of useful escalation.",
    }
    if delegations is not None:
        data["delegations"] = [
            {
                "session_id": "session_parent",
                "worker": "any-new-worker",
                "call_id": f"call-{index}",
                "citation": citation,
                "assignment": "Perform the bounded assignment.",
            }
            for index, citation in enumerate(delegations)
        ]
    if reported_events is not None:
        data["reported_events"] = [
            {
                "packet": {"event": "done", "task_id": f"task-{index}"},
                "session_id": "session_worker",
                "citation": citation,
            }
            for index, citation in enumerate(reported_events)
        ]
    if links is not None or source_citations is not None:
        data["links"] = [
            {
                "event": "handback",
                "task_id": "task-0",
                "blocker_id": "blocker-0",
                "source_session_id": "session_worker",
                "session_id": "session_parent",
                "status": "correlated",
                "citation": (links or [None])[0],
                "source_citations": list(source_citations or []),
            }
        ]
    if repeated_blockers is not None:
        data["repeated_blockers"] = [
            {
                "task_id": "task-0",
                "blocker_id": "blocker-0",
                "citations": list(repeated_blockers),
            }
        ]
    if missing_evidence is not None:
        data["missing_evidence"] = [
            {"citation": citation, "reason": "lineage not correlated"}
            for citation in missing_evidence
        ]
    return {
        "id": "scenario:real:metric:coordination",
        "kind": "metric",
        "metric": "coordination",
        "data": data,
    }


def _coordination_page(records: list[dict]) -> bool:
    return any(record.get("metric") == "coordination" for record in records)


def _coordination_page_ids(coverage: dict) -> list[str]:
    coordination_id = "scenario:real:metric:coordination"
    pages = [
        page["record_ids"]
        for page in coverage["pages"]
        if page["record_ids"] == [coordination_id]
    ]
    assert pages, "coordination record must form its own evidence page"
    return pages[0]


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


def test_page_visible_coordination_delegation_citation_regression(
    tmp_path: Path,
) -> None:
    """Replay the historical page-3 rejection.

    The coordination metric record formed its own evidence page while the
    delegated message records it cites by ID sat on earlier pages; a finding
    citing those displayed delegation citations must be accepted.
    """
    scenario, acceptance, blueprints = inputs()
    coordination_id = "scenario:real:metric:coordination"
    delegation_citation = "session:session_parent:message:m-delegation"
    records = [
        message_record(delegation_citation),
        coordination_metric_record(delegations=[delegation_citation]),
    ]

    def response(*args, **kwargs):
        if kwargs["schema_name"] == "worker_diagnostic_page":
            if _coordination_page(kwargs["payload"]["page"]["records"]):
                return {
                    "page_complete": True,
                    "observations": ["Reviewed the coordination metric."],
                    "findings": [
                        multi_citation_finding([coordination_id, delegation_citation])
                    ],
                    "retrieval_requests": [],
                }
            return {
                "page_complete": True,
                "observations": [],
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

    with patch("jcode_harness_client.judge.complete", side_effect=response):
        review, coverage = review_evidence(
            settings=settings(EvaluationLimits(judge_page_bytes=200)),
            scenario=scenario,
            records=records,
            evidence_gaps=[],
            blueprints=blueprints,
            plugins=[],
            acceptance=acceptance,
            tool_health={"status": "healthy", "calls": []},
            workers={
                "sessions": [{"session_id": "session_parent", "worker": "coordinator"}]
            },
            output=tmp_path,
        )
    coordination_ids = _coordination_page_ids(coverage)
    assert delegation_citation not in coordination_ids
    assert coverage["complete"]
    assert review["review"] == "findings"
    assert review["findings"][0]["citations"] == [
        coordination_id,
        delegation_citation,
    ]


def test_coordination_citations_from_each_defined_field_are_accepted(
    tmp_path: Path,
) -> None:
    scenario, acceptance, blueprints = inputs()
    coordination_id = "scenario:real:metric:coordination"
    field_citations = {
        "delegation": "session:session_parent:message:m-delegation",
        "event": "session:session_parent:message:m-event",
        "link": "session:session_parent:message:m-link",
        "source": "session:session_parent:message:m-source",
        "repeated": "session:session_parent:message:m-repeated",
        "missing": "session:session_parent:message:m-missing",
    }
    records = [message_record(citation) for citation in field_citations.values()] + [
        coordination_metric_record(
            delegations=[field_citations["delegation"]],
            reported_events=[field_citations["event"]],
            links=[field_citations["link"]],
            source_citations=[field_citations["source"]],
            repeated_blockers=[field_citations["repeated"]],
            missing_evidence=[field_citations["missing"]],
        )
    ]

    def response(*args, **kwargs):
        if kwargs["schema_name"] == "worker_diagnostic_page":
            if _coordination_page(kwargs["payload"]["page"]["records"]):
                return {
                    "page_complete": True,
                    "observations": ["Reviewed the coordination metric."],
                    "findings": [
                        multi_citation_finding([coordination_id, citation])
                        for citation in field_citations.values()
                    ],
                    "retrieval_requests": [],
                }
            return {
                "page_complete": True,
                "observations": [],
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

    with patch("jcode_harness_client.judge.complete", side_effect=response):
        review, coverage = review_evidence(
            settings=settings(EvaluationLimits(judge_page_bytes=200)),
            scenario=scenario,
            records=records,
            evidence_gaps=[],
            blueprints=blueprints,
            plugins=[],
            acceptance=acceptance,
            tool_health={"status": "healthy", "calls": []},
            workers={
                "sessions": [{"session_id": "session_parent", "worker": "coordinator"}]
            },
            output=tmp_path,
        )
    coordination_ids = _coordination_page_ids(coverage)
    assert not set(field_citations.values()) & set(coordination_ids)
    assert coverage["complete"]
    assert review["review"] == "findings"
    # The six findings state one identical claim through different citation
    # sources, so the claim-based dedup collapses them; validation ran before
    # dedup, so every coordination citation must survive into the stable union.
    assert len(review["findings"]) == 1
    merged = review["findings"][0]
    assert merged["citations"] == [coordination_id, *field_citations.values()]
    assert {
        citation for finding in review["findings"] for citation in finding["citations"]
    } == {coordination_id, *field_citations.values()}


def test_unregistered_or_off_page_coordination_citations_are_rejected(
    tmp_path: Path,
) -> None:
    scenario, acceptance, blueprints = inputs()
    coordination_id = "scenario:real:metric:coordination"
    retained = "session:session_parent:message:m-retained"
    off_page = "session:session_parent:message:m-off-page"
    fabricated = "session:session_parent:message:m-fabricated"

    def rejection_response(rejected_citation):
        def response(*args, **kwargs):
            if kwargs["schema_name"] == "worker_diagnostic_page":
                if _coordination_page(kwargs["payload"]["page"]["records"]):
                    return {
                        "page_complete": True,
                        "observations": [],
                        "findings": [
                            multi_citation_finding([coordination_id, rejected_citation])
                        ],
                        "retrieval_requests": [],
                    }
                return {
                    "page_complete": True,
                    "observations": [],
                    "findings": [],
                    "retrieval_requests": [],
                }
            raise AssertionError("synthesis must not run after an invalid page")

        return response

    for label, rejected, records in (
        (
            "fabricated delegation citation",
            fabricated,
            [
                message_record(retained),
                coordination_metric_record(delegations=[fabricated]),
            ],
        ),
        (
            "registry-backed but off-page record",
            off_page,
            [
                message_record(retained),
                message_record(off_page),
                coordination_metric_record(delegations=[retained]),
            ],
        ),
        (
            "worker session identity",
            "session_worker",
            [
                message_record(retained),
                coordination_metric_record(delegations=[retained]),
            ],
        ),
    ):
        target = tmp_path / label.replace(" ", "-")
        with patch(
            "jcode_harness_client.judge.complete",
            side_effect=rejection_response(rejected),
        ):
            review, coverage = review_evidence(
                settings=settings(EvaluationLimits(judge_page_bytes=200)),
                scenario=scenario,
                records=records,
                evidence_gaps=[],
                blueprints=blueprints,
                plugins=[],
                acceptance=acceptance,
                tool_health={"status": "healthy", "calls": []},
                workers={
                    "sessions": [
                        {"session_id": "session_parent", "worker": "coordinator"}
                    ]
                },
                output=target,
            )
        assert review["review"] == "incomplete", label
        assert "citation" in coverage["error"], label


def escalation_resumption_metric_record(displayed: list[str]) -> dict:
    citations = list(displayed) + [None] * 16
    data: dict = {
        "status": "healthy",
        "scenario": "escalation-resumption",
        "observed_steps": [
            {
                "step": 0,
                "name": "R1-pass",
                "observed": True,
                "event": {
                    "citation": citations[0],
                    "session_id": "session_worker",
                },
            }
        ],
        "failed_attempts": [{"citation": citations[1]}],
        "out_of_script": [{"citation": citations[2]}],
        "paging_calls": [{"citation": citations[3]}],
        "native_resume_calls": [{"citation": citations[4]}],
        "orchestration": {
            "status": "healthy",
            "violations": [{"citation": citations[5]}],
            "missing_evidence": [],
            "native_resume_grounding": [
                {
                    "resume_call": {"citation": citations[6]},
                    "helper_completion_citation": citations[7],
                    "new_turn_citations": [citations[8]],
                }
            ],
        },
        "context_shielding": {
            "status": "healthy",
            "artifact_events": [
                {
                    "citation": citations[9],
                    "guard": {"citation": citations[10]},
                }
            ],
            "violations": [{"citations": [citations[11]]}],
            "missing_evidence": [],
        },
        "evidence_unknown": {
            "status": "healthy",
            "extractions": [
                {
                    "spawn_calls": [{"citation": citations[12]}],
                    "report_citations": [citations[13]],
                    "citation": citations[15],
                }
            ],
            "role_grounding": [{"citation": citations[14]}],
            "violations": [],
            "unknown": [],
        },
        "note": "Observed orchestration facts only; not a correctness gate.",
    }
    return {
        "id": "scenario:real:metric:escalation_resumption",
        "kind": "metric",
        "metric": "escalation_resumption",
        "data": data,
    }


def _escalation_page(records: list[dict]) -> bool:
    return any(record.get("metric") == "escalation_resumption" for record in records)


def test_escalation_resumption_displayed_citations_are_accepted(
    tmp_path: Path,
) -> None:
    scenario, acceptance, blueprints = inputs()
    metric_id = "scenario:real:metric:escalation_resumption"
    event_refs = [f"scenario:real:controller:event:{index}" for index in range(1, 16)]
    guard_run_ref = "scenario:real:controller:guard-run:7"
    displayed = [*event_refs, guard_run_ref]
    records = [
        *(
            {
                "id": citation,
                "kind": "controller-event",
                "session_id": "session_worker",
                "worker": "any-new-worker",
                "data": {"seq": index, "kind": "record"},
            }
            for index, citation in enumerate(event_refs, start=1)
        ),
        {
            "id": guard_run_ref,
            "kind": "controller-guard-run",
            "session_id": "session_worker",
            "worker": "any-new-worker",
            "data": {"seq": 7, "kind": "guard-run"},
        },
        escalation_resumption_metric_record(displayed),
    ]

    def response(*args, **kwargs):
        if kwargs["schema_name"] == "worker_diagnostic_page":
            if _escalation_page(kwargs["payload"]["page"]["records"]):
                return {
                    "page_complete": True,
                    "observations": ["Reviewed the escalation_resumption metric."],
                    "findings": [
                        multi_citation_finding([metric_id, citation])
                        for citation in displayed
                    ],
                    "retrieval_requests": [],
                }
            return {
                "page_complete": True,
                "observations": [],
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

    with patch("jcode_harness_client.judge.complete", side_effect=response):
        review, coverage = review_evidence(
            settings=settings(EvaluationLimits(judge_page_bytes=200)),
            scenario=scenario,
            records=records,
            evidence_gaps=[],
            blueprints=blueprints,
            plugins=[],
            acceptance=acceptance,
            tool_health={"status": "healthy", "calls": []},
            workers={
                "sessions": [{"session_id": "session_parent", "worker": "coordinator"}]
            },
            output=tmp_path,
        )
    metric_pages = [
        page for page in coverage["pages"] if page["record_ids"] == [metric_id]
    ]
    assert metric_pages, "escalation_resumption record must form its own page"
    # The controller-event records live on different pages; the metric page
    # alone displays their references, and that display admits them.
    assert not set(displayed) & set(metric_pages[0]["record_ids"])
    assert coverage["complete"]
    assert review["review"] == "findings"
    assert len(review["findings"]) == 1
    assert review["findings"][0]["citations"] == [metric_id, *displayed]


def test_historical_page_eleven_guard_run_extraction_citations_are_accepted(
    tmp_path: Path,
) -> None:
    """The historical page-11 guard-run refs must be admissible.

    The retained blind run's judge cited the five guard-run references
    displayed by the escalation_resumption metric page (seqs 101, 102,
    106, 115, and 116) and the review ended incomplete only because the
    extraction entries' own citation field was never harvested.
    Registry-backed refs shown on the page are accepted.
    """

    scenario, acceptance, blueprints = inputs()
    metric_id = "scenario:real:metric:escalation_resumption"
    seqs = (101, 102, 106, 115, 116)
    refs = [f"scenario:real:controller:guard-run:{seq}" for seq in seqs]
    data: dict = {
        "status": "healthy",
        "scenario": "escalation-resumption",
        "evidence_unknown": {
            "status": "healthy",
            "extractions": [
                {
                    "guard_seq": seq,
                    "citation": ref,
                    "spawn_calls": [],
                    "report_citations": [],
                }
                for seq, ref in zip(seqs, refs, strict=True)
            ],
            "role_grounding": [],
            "violations": [],
            "unknown": [],
        },
        "note": "Observed orchestration facts only; not a correctness gate.",
    }
    records = [
        *(
            {
                "id": ref,
                "kind": "controller-guard-run",
                "session_id": "session_parent",
                "worker": "coordinator",
                "data": {
                    "seq": seq,
                    "kind": "guard-run",
                    "guard": {"replacement": True},
                },
            }
            for seq, ref in zip(seqs, refs, strict=True)
        ),
        {
            "id": metric_id,
            "kind": "metric",
            "metric": "escalation_resumption",
            "data": data,
        },
    ]

    def response(*args, **kwargs):
        if kwargs["schema_name"] == "worker_diagnostic_page":
            if _escalation_page(kwargs["payload"]["page"]["records"]):
                return {
                    "page_complete": True,
                    "observations": ["Reviewed the escalation_resumption metric."],
                    "findings": [multi_citation_finding([metric_id, *refs])],
                    "retrieval_requests": [],
                }
            return {
                "page_complete": True,
                "observations": [],
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

    with patch("jcode_harness_client.judge.complete", side_effect=response):
        review, coverage = review_evidence(
            settings=settings(EvaluationLimits(judge_page_bytes=200)),
            scenario=scenario,
            records=records,
            evidence_gaps=[],
            blueprints=blueprints,
            plugins=[],
            acceptance=acceptance,
            tool_health={"status": "healthy", "calls": []},
            workers={
                "sessions": [{"session_id": "session_parent", "worker": "coordinator"}]
            },
            output=tmp_path,
        )
    assert coverage["complete"]
    assert review["review"] == "findings"
    assert review["findings"][0]["citations"] == [metric_id, *refs]


def test_fabricated_guard_run_extraction_citation_is_rejected(
    tmp_path: Path,
) -> None:
    """An unregistered guard-run ref in an extraction entry stays unknown.

    Harvesting the extraction entry's citation field admits only the
    registered reference it displays; a fabricated off-page guard-run id
    is rejected exactly like any other unknown citation.
    """

    scenario, acceptance, blueprints = inputs()
    metric_id = "scenario:real:metric:escalation_resumption"
    retained = "session:session_parent:message:m-retained"
    fabricated = "scenario:real:controller:guard-run:999"

    def rejection_response(rejected_citation):
        def response(*args, **kwargs):
            if kwargs["schema_name"] == "worker_diagnostic_page":
                if _escalation_page(kwargs["payload"]["page"]["records"]):
                    return {
                        "page_complete": True,
                        "observations": [],
                        "findings": [
                            multi_citation_finding([metric_id, rejected_citation])
                        ],
                        "retrieval_requests": [],
                    }
                return {
                    "page_complete": True,
                    "observations": [],
                    "findings": [],
                    "retrieval_requests": [],
                }
            raise AssertionError("synthesis must not run after an invalid page")

        return response

    with patch(
        "jcode_harness_client.judge.complete",
        side_effect=rejection_response(fabricated),
    ):
        review, coverage = review_evidence(
            settings=settings(EvaluationLimits(judge_page_bytes=200)),
            scenario=scenario,
            records=[
                message_record(retained),
                # Only the extraction entry's own citation field carries the
                # fabricated ref; every other displayed slot stays empty.
                escalation_resumption_metric_record([None] * 15 + [fabricated]),
            ],
            evidence_gaps=[],
            blueprints=blueprints,
            plugins=[],
            acceptance=acceptance,
            tool_health={"status": "healthy", "calls": []},
            workers={
                "sessions": [{"session_id": "session_parent", "worker": "coordinator"}]
            },
            output=tmp_path,
        )
    assert review["review"] == "incomplete"
    assert "citation" in coverage["error"]


def test_unregistered_or_off_page_escalation_citations_are_rejected(
    tmp_path: Path,
) -> None:
    scenario, acceptance, blueprints = inputs()
    metric_id = "scenario:real:metric:escalation_resumption"
    retained = "session:session_parent:message:m-retained"
    off_page = "session:session_parent:message:m-off-page"
    fabricated = "scenario:real:controller:event:fabricated"

    def rejection_response(rejected_citation):
        def response(*args, **kwargs):
            if kwargs["schema_name"] == "worker_diagnostic_page":
                if _escalation_page(kwargs["payload"]["page"]["records"]):
                    return {
                        "page_complete": True,
                        "observations": [],
                        "findings": [
                            multi_citation_finding([metric_id, rejected_citation])
                        ],
                        "retrieval_requests": [],
                    }
                return {
                    "page_complete": True,
                    "observations": [],
                    "findings": [],
                    "retrieval_requests": [],
                }
            raise AssertionError("synthesis must not run after an invalid page")

        return response

    for label, rejected, records in (
        (
            "fabricated displayed controller ref",
            fabricated,
            [
                message_record(retained),
                escalation_resumption_metric_record([fabricated]),
            ],
        ),
        (
            "registry-backed but off-page record",
            off_page,
            [
                message_record(retained),
                message_record(off_page),
                escalation_resumption_metric_record([retained]),
            ],
        ),
        (
            "unregistered session identity",
            "session_worker",
            [
                message_record(retained),
                escalation_resumption_metric_record([retained]),
            ],
        ),
    ):
        target = tmp_path / label.replace(" ", "-")
        with patch(
            "jcode_harness_client.judge.complete",
            side_effect=rejection_response(rejected),
        ):
            review, coverage = review_evidence(
                settings=settings(EvaluationLimits(judge_page_bytes=200)),
                scenario=scenario,
                records=records,
                evidence_gaps=[],
                blueprints=blueprints,
                plugins=[],
                acceptance=acceptance,
                tool_health={"status": "healthy", "calls": []},
                workers={
                    "sessions": [
                        {"session_id": "session_parent", "worker": "coordinator"}
                    ]
                },
                output=target,
            )
        assert review["review"] == "incomplete", label
        assert "citation" in coverage["error"], label


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


_HISTORICAL_FINDINGS: list[dict] = [
    {
        "kind": "violation",
        "severity": "low",
        "confidence": "high",
        "owner": {
            "kind": "model-behavior",
            "id": "coordinator",
            "path": "coordinator.md",
        },
        "observed": (
            "The implementation assignment permits 'max 3 implementation/check "
            "attempts' without preserving the Routine blueprint's maximum of two "
            "meaningful failed attempts. This creates an assignment-level allowance "
            "for a third failed attempt, although the worker succeeded after one "
            "visible implementation/check cycle."
        ),
        "expected": (
            "Assignments must respect the worker's actual blueprint: at most two "
            "meaningful failed attempts, with earlier escalation for authority, "
            "design, safety, or deterministic environment blockers."
        ),
        "suggestion": (
            "Specify at most two meaningful failed attempts and distinguish successful "
            "checks or infrastructure failures from reasoning attempts. Carry the same "
            "failed-attempt budget into any assistance lineage."
        ),
        "citations": [
            "blueprint:routine",
            "blueprint:coordinator",
            "session:session_turtle_1794014158165_f078d60f08f71d3c:message"
            ":message_1794014201433_17923015478817895801",
            "session:session_parrot_1794014201489_e0c828d771e94628:message"
            ":message_1794014233517_17899378045403342106",
            "session:session_parrot_1794014201489_e0c828d771e94628:message"
            ":message_1794014239474_10655618016012666228",
        ],
    },
    {
        "kind": "improvement-opportunity",
        "severity": "low",
        "confidence": "high",
        "owner": {
            "kind": "model-behavior",
            "id": "coordinator",
            "path": "coordinator.md",
        },
        "observed": (
            "Assignments request coordination JSON using task_id/status/native-session "
            "terminology, while the blueprint defines "
            "event/task_id/blocker_id/source_session_id. Returned reports consequently "
            "use differing shapes; the implementation report also states that tests "
            "passed without explicitly stating the requested exit status."
        ),
        "expected": (
            "A consistent return template would make report review and native-trace "
            "correlation easier while retaining unknown identities as unknown."
        ),
        "suggestion": (
            "Use the blueprint's field names consistently in assignments, explicitly "
            "allow null for unavailable identities, and request a separate numeric "
            "exit-status field. Continue treating native traces\u2014not worker "
            "JSON\u2014as identity evidence."
        ),
        "citations": [
            "blueprint:coordinator",
            "blueprint:routine",
            "session:session_rabbit_1794014167405_6a6dce0b4dc2d980:message"
            ":message_1794014182563_6782951592711115675",
            "session:session_parrot_1794014201489_e0c828d771e94628:message"
            ":message_1794014258904_8923155324407574034",
            "session:session_jaguar_1794014266619_b8505646ed333b29:message"
            ":message_1794014293216_3863731626759395465",
        ],
    },
    {
        "kind": "violation",
        "severity": "medium",
        "confidence": "high",
        "owner": {
            "kind": "model-behavior",
            "id": "coordinator",
            "path": None,
        },
        "observed": (
            "The scheduler-repair assignment authorizes up to 3 implementation/check "
            "attempts without retaining the worker blueprint's stopping rule after two "
            "meaningful failed attempts. This permits a third attempt after two "
            "failures. The worker actually completed one observed edit/check cycle, so "
            "no worker overrun is demonstrated."
        ),
        "expected": (
            "Assignments must preserve the supplied blueprint's maximum of two "
            "meaningful failed attempts and require earlier escalation for "
            "deterministic environment failures, missing authority, or repeated "
            "uncertainty."
        ),
        "suggestion": (
            "Specify at most two meaningful failed attempts across the task lineage, "
            "with escalation immediately after the second failure and sooner for "
            "non-reasoning blockers."
        ),
        "citations": [
            "session:session_turtle_1794014158165_f078d60f08f71d3c:message"
            ":message_1794014201433_17923015478817895801",
            "blueprint:coordinator",
            "blueprint:routine",
        ],
    },
    {
        "kind": "violation",
        "severity": "low",
        "confidence": "high",
        "owner": {
            "kind": "model-behavior",
            "id": "coordinator",
            "path": None,
        },
        "observed": (
            "All three delegation assignments request coordination JSON using "
            "task_id/status/native-session fields rather than the blueprint's event, "
            "task_id, blocker_id, and source_session_id contract. They also omit "
            "blocker_id from the assignments. This is a coordinator return-contract "
            "mismatch; the workers' actual final JSON is not visible here."
        ),
        "expected": (
            "With coordination analysis enabled, assignments and requested reports "
            "should use the prescribed identities and event vocabulary, preserving "
            "unknown identity values rather than inventing them."
        ),
        "suggestion": (
            "Request a coordination object with event, task_id, blocker_id, and "
            "source_session_id; use the appropriate done event and explicitly mark "
            "unavailable identities unknown or null."
        ),
        "citations": [
            "session:session_turtle_1794014158165_f078d60f08f71d3c:message"
            ":message_1794014167348_5489091732883993623",
            "session:session_turtle_1794014158165_f078d60f08f71d3c:message"
            ":message_1794014201433_17923015478817895801",
            "session:session_turtle_1794014158165_f078d60f08f71d3c:message"
            ":message_1794014266562_369968418199450342",
            "blueprint:coordinator",
            "blueprint:routine",
        ],
    },
    {
        "kind": "violation",
        "severity": "medium",
        "confidence": "high",
        "owner": {
            "kind": "model-behavior",
            "id": "coordinator",
            "path": None,
        },
        "observed": (
            "The implementation assignment authorizes 'max 3 implementation/check "
            "attempts' without requiring a stop after two meaningful failed attempts. "
            "This permits a third attempt following two failures. The page does not "
            "establish that the worker actually exhausted that allowance."
        ),
        "expected": (
            "The Routine shared-work contract makes two meaningful failed attempts the "
            "maximum and requires escalation or reassessment rather than another "
            "attempt after that limit."
        ),
        "suggestion": (
            "Specify a maximum of two meaningful failed attempts, with earlier "
            "escalation for authority, design, or environment blockers; distinguish "
            "successful checks from failed reasoning attempts."
        ),
        "citations": [
            "session:session_turtle_1794014158165_f078d60f08f71d3c:message"
            ":message_1794014201433_17923015478817895801",
            "blueprint:coordinator",
            "blueprint:routine",
        ],
    },
    {
        "kind": "violation",
        "severity": "low",
        "confidence": "high",
        "owner": {
            "kind": "model-behavior",
            "id": "coordinator",
            "path": None,
        },
        "observed": (
            "All three assignments provide task_id but omit blocker_id, and request "
            "status/native-session fields rather than consistently requesting the "
            "blueprint's event, task_id, blocker_id, and source_session_id "
            "coordination schema."
        ),
        "expected": (
            "When coordination analysis is enabled, coordinator assignments include "
            "task_id and blocker_id and request fenced coordination reports carrying "
            "the prescribed identities. Unknown identities must not be fabricated."
        ),
        "suggestion": (
            "Use a consistent coordination return contract with event, task_id, "
            "blocker_id, and source_session_id; explicitly mark absent blocker or "
            "unknown native identity without inventing values."
        ),
        "citations": [
            "scenario:stable-task-order:metric:coordination",
            "blueprint:coordinator",
        ],
    },
    {
        "kind": "violation",
        "severity": "low",
        "confidence": "high",
        "owner": {
            "kind": "model-behavior",
            "id": "routine",
            "path": None,
        },
        "observed": (
            "The discovery and implementation completion packets omit blocker_id and "
            "source_session_id. Discovery substitutes native_session_id: 'unknown'; "
            "implementation supplies only event, task_id, and status. The verification "
            "packet uses the required fields with null identity values."
        ),
        "expected": (
            "Routine's blueprint requires enabled coordination reports to contain "
            "event, known task_id, blocker_id, and source_session_id, while preserving "
            "uncertainty about native identities."
        ),
        "suggestion": (
            "Retain the useful completion reports but emit the prescribed field names "
            "consistently, using null for absent or unknown values and relying on "
            "native traces for session correlation."
        ),
        "citations": [
            "session:session_rabbit_1794014167405_6a6dce0b4dc2d980:message"
            ":message_1794014182563_6782951592711115675",
            "session:session_parrot_1794014201489_e0c828d771e94628:message"
            ":message_1794014258904_8923155324407574034",
            "session:session_jaguar_1794014266619_b8505646ed333b29:message"
            ":message_1794014293216_3863731626759395465",
            "blueprint:routine",
        ],
    },
]


def _historical_records() -> list[dict]:
    citations = {
        citation
        for historical in _HISTORICAL_FINDINGS
        for citation in historical["citations"]
    }
    records = [
        {
            "id": "blueprint:routine",
            "kind": "blueprint",
            "data": {"content": "Routine blueprint."},
        },
        {
            "id": "blueprint:coordinator",
            "kind": "blueprint",
            "data": {"content": "Coordinator blueprint."},
        },
        {
            "id": "scenario:stable-task-order:metric:coordination",
            "kind": "metric",
            "metric": "coordination",
            "data": {"status": "healthy"},
        },
    ]
    records.extend(
        message_record(citation)
        for citation in sorted(citations)
        if citation.startswith("session:")
    )
    return records


def test_duplicate_findings_collapse_to_strongest_supported_record(
    tmp_path: Path,
) -> None:
    scenario, acceptance, blueprints = inputs()
    canonical = finding_variant(
        severity="low",
        confidence="high",
        citations=["record-2"],
        observed="Canonical attempt-cap wording.",
    )
    duplicate = finding_variant(
        severity="medium",
        confidence="low",
        citations=["record-1"],
        observed="Canonical attempt-cap wording.",
        suggestion="Also distinguish successful checks from failed attempts.",
    )

    def response(*args, **kwargs):
        if kwargs["schema_name"] == "worker_diagnostic_page":
            return {
                "page_complete": True,
                "observations": [],
                "findings": [canonical, duplicate],
                "retrieval_requests": [],
            }
        return {
            "review_complete": True,
            "reviewed_sessions": ["session_parent"],
            "summary": "Complete harness review.",
            "findings": [canonical],
            "retrieval_requests": [],
        }

    with patch("jcode_harness_client.judge.complete", side_effect=response):
        review, coverage = review_evidence(
            settings=settings(),
            scenario=scenario,
            records=[message_record("record-1"), message_record("record-2")],
            evidence_gaps=[],
            blueprints=blueprints,
            plugins=[],
            acceptance=acceptance,
            tool_health={"status": "healthy", "calls": []},
            workers={
                "sessions": [{"session_id": "session_parent", "worker": "coordinator"}]
            },
            output=tmp_path,
        )
    assert coverage["complete"]
    assert review["review"] == "findings"
    assert len(review["findings"]) == 1
    merged = review["findings"][0]
    assert merged["severity"] == "medium"
    assert merged["confidence"] == "low"
    assert merged["citations"] == ["record-2", "record-1"]
    assert merged["observed"] == "Canonical attempt-cap wording."
    assert merged["expected"] == canonical["expected"]
    assert merged["suggestion"] == (
        "Make the smallest contract correction."
        " | Also distinguish successful checks from failed attempts."
    )
    assert merged["owner"] == canonical["owner"]


def test_shared_owner_or_citations_do_not_merge_distinct_causes(
    tmp_path: Path,
) -> None:
    scenario, acceptance, blueprints = inputs()
    first_cause = finding_variant(
        citations=["record-1", "record-2"],
        observed="First distinct cause.",
    )
    overlapping_cause = finding_variant(
        citations=["record-1", "record-3"],
        observed="Second distinct cause sharing one citation.",
    )
    improvement_cause = finding_variant(
        kind="improvement-opportunity",
        citations=["record-1", "record-2"],
        observed="Same evidence framed as an optional improvement.",
    )
    other_owner_cause = finding_variant(
        owner=coordinator_blueprint_owner(),
        citations=["record-1", "record-2"],
        observed="Same evidence owned by the coordinator blueprint.",
    )
    superset_cause = finding_variant(
        citations=["record-1", "record-2", "record-3"],
        observed="Wider evidence set for a distinct cause.",
    )
    same_citations_other_claim = finding_variant(
        citations=["record-1", "record-2"],
        observed="Different claim citing exactly the same records.",
    )
    same_observed_other_expected = finding_variant(
        citations=["record-1", "record-2"],
        observed="First distinct cause.",
        expected="A different expected contract for the same observation.",
    )

    def response(*args, **kwargs):
        if kwargs["schema_name"] == "worker_diagnostic_page":
            return {
                "page_complete": True,
                "observations": [],
                "findings": [
                    first_cause,
                    overlapping_cause,
                    improvement_cause,
                    other_owner_cause,
                    superset_cause,
                    same_citations_other_claim,
                    same_observed_other_expected,
                ],
                "retrieval_requests": [],
            }
        return {
            "review_complete": True,
            "reviewed_sessions": ["session_parent"],
            "summary": "Complete harness review.",
            "findings": [],
            "retrieval_requests": [],
        }

    with patch("jcode_harness_client.judge.complete", side_effect=response):
        review, coverage = review_evidence(
            settings=settings(),
            scenario=scenario,
            records=[
                message_record("record-1"),
                message_record("record-2"),
                message_record("record-3"),
            ],
            evidence_gaps=[],
            blueprints=blueprints,
            plugins=[],
            acceptance=acceptance,
            tool_health={"status": "healthy", "calls": []},
            workers={
                "sessions": [{"session_id": "session_parent", "worker": "coordinator"}]
            },
            output=tmp_path,
        )
    assert coverage["complete"]
    deduped = review["findings"]
    assert [finding["observed"] for finding in deduped] == [
        "First distinct cause.",
        "Second distinct cause sharing one citation.",
        "Same evidence framed as an optional improvement.",
        "Same evidence owned by the coordinator blueprint.",
        "Wider evidence set for a distinct cause.",
        "Different claim citing exactly the same records.",
        "First distinct cause.",
    ]
    assert deduped[0]["citations"] == ["record-1", "record-2"]
    assert deduped[5]["citations"] == ["record-1", "record-2"]
    assert deduped[6]["citations"] == ["record-1", "record-2"]
    assert deduped[0]["expected"] != deduped[6]["expected"]
    assert deduped[2]["kind"] == "improvement-opportunity"
    assert deduped[3]["owner"]["kind"] == "coordinator-blueprint"


def test_single_finding_is_returned_unchanged(tmp_path: Path) -> None:
    scenario, acceptance, blueprints = inputs()
    expected = finding()

    def response(*args, **kwargs):
        if kwargs["schema_name"] == "worker_diagnostic_page":
            return {
                "page_complete": True,
                "observations": [],
                "findings": [expected],
                "retrieval_requests": [],
            }
        return {
            "review_complete": True,
            "reviewed_sessions": ["session_parent"],
            "summary": "Complete harness review.",
            "findings": [],
            "retrieval_requests": [],
        }

    with patch("jcode_harness_client.judge.complete", side_effect=response):
        review, coverage = review_evidence(
            settings=settings(),
            scenario=scenario,
            records=[message_record("record-1")],
            evidence_gaps=[],
            blueprints=blueprints,
            plugins=[],
            acceptance=acceptance,
            tool_health={"status": "healthy", "calls": []},
            workers={
                "sessions": [{"session_id": "session_parent", "worker": "coordinator"}]
            },
            output=tmp_path,
        )
    assert coverage["complete"]
    assert review["review"] == "findings"
    assert review["findings"] == [expected]


def test_invalid_citation_is_rejected_before_deduplication(tmp_path: Path) -> None:
    scenario, acceptance, blueprints = inputs()

    def response(*args, **kwargs):
        if kwargs["schema_name"] == "worker_diagnostic_page":
            return {
                "page_complete": True,
                "observations": [],
                "findings": [
                    finding(),
                    finding_variant(citations=["record-1", "not-present"]),
                ],
                "retrieval_requests": [],
            }
        raise AssertionError("synthesis must not run after an invalid page")

    with patch("jcode_harness_client.judge.complete", side_effect=response):
        review, coverage = review_evidence(
            settings=settings(),
            scenario=scenario,
            records=[message_record("record-1")],
            evidence_gaps=[],
            blueprints=blueprints,
            plugins=[],
            acceptance=acceptance,
            tool_health={"status": "healthy", "calls": []},
            workers={
                "sessions": [{"session_id": "session_parent", "worker": "coordinator"}]
            },
            output=tmp_path,
        )
    assert review["review"] == "incomplete"
    assert not coverage["complete"]
    assert "citation" in coverage["error"]
    assert review["findings"] == []


def test_historical_smoke_findings_replay_offline_dedup(tmp_path: Path) -> None:
    """Replay the seven findings from the last offline smoke review.

    Historical findings 3 and 5 share one owner, kind, and citation set, so
    the earlier citation-set-only identity collapsed them, but their observed
    and expected claims are worded differently and must stay separate. No two
    of the seven findings have exactly matching observed and expected strings
    for the same owner and kind, so deterministic dedup keeps all seven
    unchanged and in order.
    """

    scenario, acceptance, blueprints = inputs()

    def response(*args, **kwargs):
        if kwargs["schema_name"] == "worker_diagnostic_page":
            return {
                "page_complete": True,
                "observations": ["Reviewed all projected records."],
                "findings": [dict(item) for item in _HISTORICAL_FINDINGS[:5]],
                "retrieval_requests": [],
            }
        return {
            "review_complete": True,
            "reviewed_sessions": ["session_parent"],
            "summary": "Complete harness review.",
            "findings": [dict(item) for item in _HISTORICAL_FINDINGS[5:]],
            "retrieval_requests": [],
        }

    with patch("jcode_harness_client.judge.complete", side_effect=response):
        review, coverage = review_evidence(
            settings=settings(),
            scenario=scenario,
            records=_historical_records(),
            evidence_gaps=[],
            blueprints=blueprints,
            plugins=[],
            acceptance=acceptance,
            tool_health={"status": "healthy", "calls": []},
            workers={
                "sessions": [{"session_id": "session_parent", "worker": "coordinator"}]
            },
            output=tmp_path,
        )
    assert coverage["complete"]
    assert review["review"] == "findings"
    deduped = review["findings"]
    historical = _HISTORICAL_FINDINGS
    assert len(deduped) == 7
    assert [finding["observed"] for finding in deduped] == [
        item["observed"] for item in historical
    ]
    assert all(
        finding == original
        for finding, original in zip(deduped, historical, strict=True)
    )
    assert deduped[2] == historical[2]
    assert deduped[4] == historical[4]
    assert deduped[1]["kind"] == "improvement-opportunity"
    assert deduped[2]["kind"] == "violation"
    assert deduped[3]["kind"] == "violation"
    assert deduped[4]["kind"] == "violation"


def test_dedup_requires_exact_claim_text_but_unions_exact_duplicates(
    tmp_path: Path,
) -> None:
    scenario, acceptance, blueprints = inputs()
    exact_first = finding_variant(
        citations=["record-1"],
        observed="Observed code returns `OK`.",
        expected="Expected result: `OK`.",
    )
    exact_second = finding_variant(
        severity="high",
        confidence="low",
        citations=["record-2"],
        observed="Observed code returns `OK`.",
        expected="Expected result: `OK`.",
        suggestion="Retain the exact behavior check.",
    )
    case_sensitive_observed = finding_variant(
        citations=["record-1"],
        observed="Observed code returns `ok`.",
        expected="Expected result: `OK`.",
    )
    case_sensitive_expected = finding_variant(
        citations=["record-1"],
        observed="Observed code returns `OK`.",
        expected="Expected result: `ok`.",
    )
    whitespace_significant = finding_variant(
        citations=["record-1"],
        observed="Observed code returns `OK`.",
        expected="Expected snippet:\n    return value",
    )

    def response(*args, **kwargs):
        if kwargs["schema_name"] == "worker_diagnostic_page":
            return {
                "page_complete": True,
                "observations": [],
                "findings": [
                    exact_first,
                    exact_second,
                    case_sensitive_observed,
                    case_sensitive_expected,
                    whitespace_significant,
                ],
                "retrieval_requests": [],
            }
        return {
            "review_complete": True,
            "reviewed_sessions": ["session_parent"],
            "summary": "Complete harness review.",
            "findings": [],
            "retrieval_requests": [],
        }

    with patch("jcode_harness_client.judge.complete", side_effect=response):
        review, coverage = review_evidence(
            settings=settings(),
            scenario=scenario,
            records=[
                message_record("record-1"),
                message_record("record-2"),
            ],
            evidence_gaps=[],
            blueprints=blueprints,
            plugins=[],
            acceptance=acceptance,
            tool_health={"status": "healthy", "calls": []},
            workers={
                "sessions": [{"session_id": "session_parent", "worker": "coordinator"}]
            },
            output=tmp_path,
        )

    assert coverage["complete"]
    deduped = review["findings"]
    assert len(deduped) == 4
    merged, *distinct = deduped
    assert merged["observed"] == exact_first["observed"]
    assert merged["expected"] == exact_first["expected"]
    assert merged["severity"] == "high"
    assert merged["confidence"] == "low"
    assert merged["citations"] == ["record-1", "record-2"]
    assert merged["suggestion"] == (
        exact_first["suggestion"] + " | " + exact_second["suggestion"]
    )
    assert [item["observed"] for item in distinct] == [
        case_sensitive_observed["observed"],
        case_sensitive_expected["observed"],
        whitespace_significant["observed"],
    ]
    assert [item["expected"] for item in distinct] == [
        case_sensitive_observed["expected"],
        case_sensitive_expected["expected"],
        whitespace_significant["expected"],
    ]
    assert all(item["citations"] == ["record-1"] for item in distinct)


def test_projection_keeps_escalation_resumption_metric_complete() -> None:
    """Non-tool-health metric reports stay complete and citable."""

    record = {
        "id": "scenario:escalation-resumption:metric:escalation_resumption",
        "kind": "metric",
        "metric": "escalation_resumption",
        "data": {"status": "healthy"},
    }
    projection = build_judge_projection([record])
    assert len(projection.records) == 1
    projected = projection.records[0]
    assert projected["content_policy"] == "complete"
    assert projected["raw_citation"] == record["id"]
    assert projected["data"] == {"status": "healthy"}
