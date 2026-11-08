from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from jcode_harness_client.judge import complete
from jcode_harness_client.llm_backend import (
    Caching,
    CallUsageRecord,
    UsageTracker,
    _extract_wire_usage,
    _native_reported_cost,
    _provider_details_cost,
    cache_identity,
    record_native_session_usage,
    run_structured,
)
from jcode_harness_client.settings import parse_settings


class _SimpleSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    sensitive: str = ""


def settings(
    provider: str,
    model: str = "judge-model",
    url: str = "https://endpoint.invalid/v1",
):
    return parse_settings(
        f"""[provider]
type = "{provider}"
url = "{url}"
model = "{model}"
api_key = "secret-token"
"""
    )


def write_session(
    sessions: Path,
    session_id: str,
    *,
    model: str,
    messages: list[dict],
    **extra: object,
) -> None:
    payload: dict = {
        "session_id": session_id,
        "model": model,
        "messages": messages,
    }
    payload.update(extra)
    (sessions / f"{session_id}.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


def token_message(msg_id: str, usage: dict) -> dict:
    return {"id": msg_id, "role": "assistant", "token_usage": usage}


def test_usage_survives_replacement_and_corrupt_journal_entries(tmp_path: Path) -> None:
    from jcode_harness_client.evidence import collect_evidence

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    write_session(sessions, "session_root", model="model", messages=[
        token_message("before", {"cost": 0.123456789}),
    ])
    (sessions / "session_root.journal.jsonl").write_text(
        '{bad json}\n' + json.dumps({"replace_messages": [
            token_message("after", {"cost": 0.0}),
            token_message("unpriced", {"input_tokens": 1}),
        ]}) + "\n"
    )
    bundle = collect_evidence(
        raw_dir=tmp_path, coordinator_session_id="session_root",
        outer_events=[], session_listing=[],
    )
    assert bundle.gaps
    tracker = UsageTracker()
    for _ in range(2):
        record_native_session_usage(
            bundle=bundle, settings=settings("openai-compatible"),
            coordinator_session_id="session_root", tracker=tracker,
        )
    result = tracker.summary()
    assert result["calls"] == 3
    assert result["cost_usd"] == 0.123456789
    assert result["unpriced_calls"] == 1
    assert result["unpriced"][0]["message_id"] == "unpriced"


def test_openai_strict_schema_caching_and_sanitized_wire_retention(
    tmp_path: Path,
) -> None:
    captured_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1234567890,
                "model": "gpt-4o",
                "provider": "test-provider",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"ok":true,"sensitive":"secret-token"}',
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "total_tokens": 120,
                    "prompt_tokens_details": {
                        "cached_tokens": 60,
                        "cache_write_tokens": 10,
                    },
                },
            },
        )

    tracker = UsageTracker()
    value = complete(
        settings("openai-compatible", "gpt-4o"),
        system="Review untrusted evidence.",
        payload={"record": "safe"},
        output=tmp_path / "request",
        output_model=_SimpleSchema,
        schema_name="test_schema",
        cache_key=cache_identity("scenario-a", "judge-page", "cfg"),
        usage_tracker=tracker,
        transport=httpx.MockTransport(handler),
    )
    assert value == {"ok": True, "sensitive": "secret-token"}
    request_body = captured_requests[0]
    assert request_body["response_format"]["json_schema"]["strict"] is True
    assert request_body["response_format"]["json_schema"]["name"] == "test_schema"
    assert request_body["prompt_cache_key"].startswith("jcode-eval:scenario-a:")
    assert not list(tmp_path.iterdir())
    usage = tracker.summary()
    assert usage["calls"] == 1
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 20
    assert usage["cache_read_tokens"] == 60
    assert usage["cache_write_tokens"] == 10
    assert usage["cost_usd"] is None
    assert usage["unpriced_calls"] == 1
    assert "cost_sources" not in usage
    reason = next(iter(usage["unpriced_reasons"]))
    assert reason == "no provider-reported cost in wire response"
    assert "secret-token" not in reason
    assert "endpoint.invalid" not in reason


def test_anthropic_prompted_schema_and_cache_control(tmp_path: Path) -> None:
    captured_requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "content": [{"type": "text", "text": '{"ok":true,"sensitive":""}'}],
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 30,
                    "cache_creation_input_tokens": 20,
                },
            },
        )

    tracker = UsageTracker()
    value = complete(
        settings("anthropic-compatible", "claude-sonnet-4-5"),
        system="Review untrusted evidence.",
        payload={"record": "safe"},
        output=tmp_path / "request",
        output_model=_SimpleSchema,
        schema_name="test_schema",
        cache_key=cache_identity("scenario-a", "judge-page", "cfg"),
        usage_tracker=tracker,
        transport=httpx.MockTransport(handler),
    )
    assert value == {"ok": True, "sensitive": ""}
    request_body = captured_requests[0]
    assert "response_format" not in request_body
    system_blocks = request_body["system"]
    assert any("cache_control" in block for block in system_blocks)
    summary = tracker.summary()
    assert summary["input_tokens"] == 100
    assert summary["cache_read_tokens"] == 30
    assert summary["cache_write_tokens"] == 20


def test_no_sdk_or_agent_retries_on_http_error_or_malformed_output(
    tmp_path: Path,
) -> None:
    http_calls = 0

    def error_handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(
            400,
            json={
                "error": {"message": "unsupported parameter prompt_cache_key"},
                "usage": {"prompt_tokens": 12, "completion_tokens": 0},
            },
        )

    tracker = UsageTracker()
    with pytest.raises(RuntimeError, match="HTTP 400"):
        run_structured(
            settings("openai-compatible", "unknown-custom-model"),
            role="judge",
            system="sys",
            user_prompt="prompt",
            output_path=tmp_path / "fail-http",
            output_model=_SimpleSchema,
            schema_name="test_schema",
            cache_key="jcode-eval:test:1",
            usage_tracker=tracker,
            transport=httpx.MockTransport(error_handler),
        )
    assert http_calls == 1
    error_summary = tracker.summary()
    assert error_summary["calls"] == 1
    assert error_summary["unpriced_calls"] == 1
    error_reasons = error_summary["unpriced_reasons"]
    assert len(error_reasons) == 1
    error_reason = next(iter(error_reasons))
    assert error_reason == "no provider-reported cost in wire response"
    assert "secret-token" not in error_reason
    assert "endpoint.invalid" not in error_reason

    malformed_calls = 0

    def malformed_handler(request: httpx.Request) -> httpx.Response:
        nonlocal malformed_calls
        malformed_calls += 1
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-2",
                "object": "chat.completion",
                "created": 1234567890,
                "model": "gpt-4o",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"ok":"bad"}'},
                    }
                ],
                "usage": {
                    "prompt_tokens": 25,
                    "completion_tokens": 5,
                    "total_tokens": 30,
                    "cost": 0.0125,
                },
            },
        )

    malformed_tracker = UsageTracker()
    with pytest.raises(RuntimeError, match="judge call failed"):
        run_structured(
            settings("openai-compatible", "gpt-4o"),
            role="judge",
            system="sys",
            user_prompt="prompt",
            output_path=tmp_path / "fail-parse",
            output_model=_SimpleSchema,
            schema_name="test_schema",
            cache_key="jcode-eval:test:2",
            usage_tracker=malformed_tracker,
            transport=httpx.MockTransport(malformed_handler),
        )
    assert malformed_calls == 1
    malformed_summary = malformed_tracker.summary()
    assert malformed_summary["cost_usd"] == 0.0125
    assert malformed_summary["cost_sources"] == {"reported-wire": 1}


def test_native_session_usage_aggregation_and_deduplication(tmp_path: Path) -> None:
    sessions = tmp_path / "raw" / "sessions"
    sessions.mkdir(parents=True)
    coordinator_session_id = f"session_{uuid4().hex}"
    worker_session_id = f"session_{uuid4().hex}"
    worker_name = f"worker-{uuid4().hex}"
    parent = sessions / f"{coordinator_session_id}.json"
    parent.write_text(
        json.dumps(
            {
                "session_id": coordinator_session_id,
                "model": "gpt-4o",
                "messages": [
                    {
                        "id": "m1",
                        "role": "assistant",
                        "token_usage": {
                            "prompt_tokens": 200,
                            "input_tokens": 200,
                            "output_tokens": 50,
                            "cache_read_input_tokens": 100,
                            "cache_creation_input_tokens": 20,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    parent.with_name(f"{coordinator_session_id}.journal.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "append_messages": [
                            {
                                "id": "m1",
                                "role": "assistant",
                                "token_usage": {
                                    "prompt_tokens": 200,
                                    "input_tokens": 200,
                                    "output_tokens": 50,
                                },
                            },
                            {
                                "id": "m2",
                                "role": "assistant",
                                "token_usage": {
                                    "prompt_tokens": 300,
                                    "input_tokens": 300,
                                    "output_tokens": 40,
                                    "cache_read_input_tokens": 150,
                                },
                            },
                        ]
                    }
                )
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    worker = sessions / f"{worker_session_id}.json"
    worker.write_text(
        json.dumps(
            {
                "session_id": worker_session_id,
                "model": "worker_acceptance:observed-unpriced-model",
                "worker_blueprint_policy": {"name": worker_name},
                "messages": [
                    {
                        "id": "w1",
                        "role": "assistant",
                        "token_usage": {
                            "prompt_tokens": 80,
                            "input_tokens": 80,
                            "output_tokens": 10,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    tracker = UsageTracker()
    record_native_session_usage(
        raw_dir=tmp_path / "raw",
        settings=settings("openai-compatible", "gpt-4o"),
        coordinator_session_id=coordinator_session_id,
        tracker=tracker,
    )
    summary = tracker.summary()
    assert summary["calls"] == 3
    assert summary["input_tokens"] == 580
    assert summary["output_tokens"] == 100
    assert summary["cache_read_tokens"] == 250
    assert summary["unpriced_calls"] == 3
    assert summary["cost_usd"] is None
    assert "cost_sources" not in summary
    assert summary["unpriced_reasons"] == {
        "no provider-reported cost in native session usage": 3
    }
    assert {item["message_id"] for item in summary["unpriced"]} == {"m1", "m2", "w1"}
    assert any(
        item["role"] == worker_name and item["model"] == "observed-unpriced-model"
        for item in summary["unpriced"]
    )
    record_native_session_usage(
        raw_dir=tmp_path / "raw", settings=settings("openai-compatible", "gpt-4o"),
        coordinator_session_id=coordinator_session_id, tracker=tracker,
    )
    assert tracker.summary()["calls"] == 3
    for reason in summary["unpriced_reasons"]:
        assert "secret-token" not in reason
        assert "endpoint.invalid" not in reason


def test_three_level_native_usage_counts_each_session_once(tmp_path: Path) -> None:
    sessions = tmp_path / "raw" / "sessions"
    sessions.mkdir(parents=True)
    root_id = f"session_{uuid4().hex}"
    advanced_id = f"session_{uuid4().hex}"
    routine_id = f"session_{uuid4().hex}"
    advanced_name = f"advanced-{uuid4().hex}"
    routine_name = f"routine-{uuid4().hex}"
    write_session(
        sessions,
        root_id,
        model="gpt-4o",
        messages=[
            token_message(
                "m1", {"prompt_tokens": 100, "input_tokens": 100, "output_tokens": 20}
            )
        ],
    )
    advanced_path = sessions / f"{advanced_id}.json"
    write_session(
        sessions,
        advanced_id,
        model="gpt-6.1-sol",
        parent_id=root_id,
        worker_blueprint_policy={"name": advanced_name},
        messages=[
            token_message(
                "a1", {"prompt_tokens": 80, "input_tokens": 80, "output_tokens": 10}
            )
        ],
    )
    # The snapshot message reappears verbatim in the journal and must be
    # counted once; the journal adds one further advanced turn.
    advanced_path.with_name(f"{advanced_id}.journal.jsonl").write_text(
        json.dumps(
            {
                "meta": {"parent_id": root_id},
                "append_messages": [
                    token_message(
                        "a1",
                        {
                            "prompt_tokens": 80,
                            "input_tokens": 80,
                            "output_tokens": 10,
                        },
                    ),
                    token_message(
                        "a2",
                        {"prompt_tokens": 50, "input_tokens": 50, "output_tokens": 5},
                    ),
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    write_session(
        sessions,
        routine_id,
        model="gpt-6-luna",
        parent_id=advanced_id,
        worker_blueprint_policy={"name": routine_name},
        messages=[
            token_message(
                "r1", {"prompt_tokens": 30, "input_tokens": 30, "output_tokens": 5}
            )
        ],
    )
    tracker = UsageTracker()
    record_native_session_usage(
        raw_dir=tmp_path / "raw",
        settings=settings("openai-compatible", "gpt-4o"),
        coordinator_session_id=root_id,
        tracker=tracker,
    )
    summary = tracker.summary()
    assert summary["calls"] == 4
    assert summary["input_tokens"] == 260
    assert summary["output_tokens"] == 40
    assert summary["unpriced_calls"] == 4
    assert summary["unpriced_reasons"] == {
        "no provider-reported cost in native session usage": 4
    }
    assert {item["session_id"] for item in summary["unpriced"]} == {
        root_id, advanced_id, routine_id
    }


def test_usage_tracker_breakdowns_separate_unpriced_overhead_calls() -> None:
    tracker = UsageTracker()
    worker_role = f"worker-{uuid4().hex}"
    tracker.add(
        CallUsageRecord(
            role=worker_role,
            model="observed-model",
            provider="openai-compatible",
            input_tokens=11,
            priced=False,
        )
    )
    tracker.add(
        CallUsageRecord(
            role="judge",
            model="review-model",
            provider="openai-compatible",
            output_tokens=7,
            cost_usd=0.25,
            priced=True,
            category="evaluation-overhead",
        )
    )
    summary = tracker.summary()
    assert summary["calls"] == 2
    assert summary["unpriced_calls"] == 1
    assert summary["cost_usd"] == 0.25
    assert summary["unpriced"][0]["role"] == worker_role


def test_caching_capability_validation() -> None:
    with pytest.raises(ValueError, match="cache_key"):
        Caching(provider_type="openai-compatible", cache_key="")
    with pytest.raises(ValueError, match="retention"):
        Caching(
            provider_type="openai-compatible",
            cache_key="valid",
            retention="invalid",
        )


def test_native_reported_cost_precedence_and_zero(tmp_path: Path) -> None:
    sessions = tmp_path / "raw" / "sessions"
    sessions.mkdir(parents=True)
    session_id = f"session_{uuid4().hex}"
    write_session(
        sessions,
        session_id,
        model="gpt-4o",
        messages=[
            token_message(
                "m1",
                {
                    "prompt_tokens": 200,
                    "input_tokens": 200,
                    "output_tokens": 50,
                    "cost_usd": 0.0123,
                },
            ),
            token_message(
                "m2",
                {
                    "prompt_tokens": 100,
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cost": 0.0,
                },
            ),
        ],
    )
    tracker = UsageTracker()
    record_native_session_usage(
        raw_dir=tmp_path / "raw",
        settings=settings("openai-compatible", "gpt-4o"),
        coordinator_session_id=session_id,
        tracker=tracker,
    )
    summary = tracker.summary()
    assert summary["calls"] == 2
    assert summary["cost_usd"] == 0.0123
    assert "unpriced_calls" not in summary
    assert summary["cost_sources"] == {"reported-native": 2}
    assert tracker.records[0].cost_source == "reported-native"
    assert tracker.records[1].cost_usd == 0.0 and tracker.records[1].priced


def test_native_missing_reported_cost_stays_unknown_with_safe_reason(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "raw" / "sessions"
    sessions.mkdir(parents=True)
    session_id = f"session_{uuid4().hex}"
    write_session(
        sessions,
        session_id,
        model="gpt-6.1-sol",
        messages=[
            token_message(
                "m1",
                {"prompt_tokens": 100, "input_tokens": 100, "output_tokens": 20},
            )
        ],
    )
    worker_session_id = f"session_{uuid4().hex}"
    worker_name = f"worker-{uuid4().hex}"
    write_session(
        sessions,
        worker_session_id,
        model="gpt-6-luna",
        messages=[
            token_message(
                "w1",
                {"prompt_tokens": 80, "input_tokens": 80, "output_tokens": 10},
            )
        ],
        worker_blueprint_policy={"name": worker_name},
    )
    tracker = UsageTracker()
    record_native_session_usage(
        raw_dir=tmp_path / "raw",
        settings=settings("openai-compatible", "gpt-4o"),
        coordinator_session_id=session_id,
        tracker=tracker,
    )
    summary = tracker.summary()
    assert summary["calls"] == 2
    assert summary["cost_usd"] is None
    assert summary["unpriced_calls"] == 2
    reasons = summary["unpriced_reasons"]
    assert reasons == {"no provider-reported cost in native session usage": 2}
    for reason in reasons:
        assert "secret-token" not in reason
        assert "endpoint.invalid" not in reason
        assert "$" not in reason


def test_wire_missing_reported_cost_records_safe_unpriced_reason(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-3",
                "object": "chat.completion",
                "created": 1234567890,
                "model": "gpt-6.1-sol",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": '{"ok":true}'},
                    }
                ],
                "usage": {
                    "prompt_tokens": 40,
                    "completion_tokens": 8,
                    "total_tokens": 48,
                },
            },
        )

    tracker = UsageTracker()
    value = complete(
        settings("openai-compatible", "gpt-6.1-sol"),
        system="Review untrusted evidence.",
        payload={"record": "safe"},
        output=tmp_path / "request",
        output_model=_SimpleSchema,
        schema_name="test_schema",
        cache_key=cache_identity("scenario-a", "judge-page", "cfg"),
        usage_tracker=tracker,
        transport=httpx.MockTransport(handler),
    )
    assert value == {"ok": True, "sensitive": ""}
    summary = tracker.summary()
    assert summary["calls"] == 1
    assert summary["input_tokens"] == 40
    assert summary["output_tokens"] == 8
    assert summary["cost_usd"] is None
    assert summary["unpriced_calls"] == 1
    assert "cost_sources" not in summary
    reason = next(iter(summary["unpriced_reasons"]))
    assert reason == "no provider-reported cost in wire response"
    assert "secret-token" not in reason
    assert "endpoint.invalid" not in reason


def test_wire_reported_zero_cost_is_priced(tmp_path: Path) -> None:
    def handler(body_usage: dict) -> httpx.MockTransport:
        def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-4",
                    "object": "chat.completion",
                    "created": 1234567890,
                    "model": "gpt-4o",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": '{"ok":true}'},
                        }
                    ],
                    "usage": body_usage,
                },
            )

        return httpx.MockTransport(respond)

    zero_tracker = UsageTracker()
    complete(
        settings("openai-compatible", "gpt-4o"),
        system="Review untrusted evidence.",
        payload={"record": "safe"},
        output=tmp_path / "zero",
        output_model=_SimpleSchema,
        schema_name="test_schema",
        cache_key=cache_identity("scenario-a", "judge-page", "cfg"),
        usage_tracker=zero_tracker,
        transport=handler(
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "cost": 0.0,
            }
        ),
    )
    zero_summary = zero_tracker.summary()
    assert zero_summary["cost_usd"] == 0.0
    assert "unpriced_calls" not in zero_summary
    assert zero_summary["cost_sources"] == {"reported-wire": 1}
    assert zero_tracker.records[0].priced

    details_tracker = UsageTracker()
    complete(
        settings("openai-compatible", "gpt-4o"),
        system="Review untrusted evidence.",
        payload={"record": "safe"},
        output=tmp_path / "details",
        output_model=_SimpleSchema,
        schema_name="test_schema",
        cache_key=cache_identity("scenario-a", "judge-page", "cfg"),
        usage_tracker=details_tracker,
        transport=handler(
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "cost_details": {"upstream_inference_cost": 0.03},
            }
        ),
    )
    details_summary = details_tracker.summary()
    assert details_summary["cost_usd"] == 0.03
    assert details_summary["cost_sources"] == {"reported-wire": 1}


def test_native_reported_cost_rejects_nonfinite_negative_and_bool() -> None:
    for bad in (float("nan"), float("inf"), float("-inf"), -1.5, True):
        assert _native_reported_cost({"cost_usd": bad}) is None
        assert _native_reported_cost({"total_cost_usd": bad}) is None
        assert _native_reported_cost({"total_cost": bad}) is None
        assert _native_reported_cost({"cost": bad}) is None
    assert _native_reported_cost({"cost_usd": 0.0}) == 0.0
    assert _native_reported_cost({"cost": 3}) == 3.0
    assert _native_reported_cost({"cost": "0.5"}) is None
    assert _native_reported_cost({}) is None


def test_native_nonfinite_reported_cost_stays_unpriced(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "raw" / "sessions"
    sessions.mkdir(parents=True)
    session_id = f"session_{uuid4().hex}"
    write_session(
        sessions,
        session_id,
        model="gpt-4o",
        messages=[
            token_message(
                "m1",
                {
                    "prompt_tokens": 200,
                    "input_tokens": 200,
                    "output_tokens": 50,
                    "cost_usd": float("nan"),
                },
            ),
            token_message(
                "m2",
                {
                    "prompt_tokens": 100,
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cost": float("-inf"),
                },
            ),
        ],
    )
    tracker = UsageTracker()
    record_native_session_usage(
        raw_dir=tmp_path / "raw",
        settings=settings("openai-compatible", "gpt-4o"),
        coordinator_session_id=session_id,
        tracker=tracker,
    )
    summary = tracker.summary()
    assert summary["calls"] == 2
    assert summary["cost_usd"] is None
    assert summary["unpriced_calls"] == 2
    assert "cost_sources" not in summary
    assert summary["unpriced_reasons"] == {
        "no provider-reported cost in native session usage": 2
    }


def test_wire_malformed_reported_cost_stays_unpriced() -> None:
    bad_costs = (float("nan"), float("inf"), float("-inf"), -0.75, True)
    for bad in bad_costs:
        usage_bodies = (
            {"prompt_tokens": 10, "completion_tokens": 5, "cost": bad},
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "cost_details": {"upstream_inference_cost": bad},
            },
        )
        for usage_body in usage_bodies:
            in_tok, out_tok, cache_read, cache_write, cost, source, reason = (
                _extract_wire_usage("openai-compatible", {"usage": usage_body})
            )
            assert (in_tok, out_tok, cache_read, cache_write) == (10, 5, 0, 0)
            assert cost is None
            assert source is None
            assert reason == "no provider-reported cost in wire response"


def test_wire_nan_reported_cost_end_to_end_stays_unpriced(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = {
            "id": "chatcmpl-nan",
            "object": "chat.completion",
            "created": 1234567890,
            "model": "gpt-4o",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": '{"ok":true,"sensitive":""}',
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "cost": float("nan"),
            },
        }
        return httpx.Response(
            200,
            content=json.dumps(payload),
            headers={"content-type": "application/json"},
        )

    tracker = UsageTracker()
    complete(
        settings("openai-compatible", "gpt-4o"),
        system="Review untrusted evidence.",
        payload={"record": "safe"},
        output=tmp_path / "nan-cost",
        output_model=_SimpleSchema,
        schema_name="test_schema",
        cache_key=cache_identity("scenario-a", "judge-page", "cfg"),
        usage_tracker=tracker,
        transport=httpx.MockTransport(handler),
    )
    summary = tracker.summary()
    assert summary["cost_usd"] is None
    assert summary["unpriced_calls"] == 1
    assert "cost_sources" not in summary
    assert summary["unpriced_reasons"] == {
        "no provider-reported cost in wire response": 1
    }


def test_provider_details_cost_rejects_nonfinite_negative_and_bool() -> None:
    for bad in (float("nan"), float("inf"), float("-inf"), -0.5, True):
        assert _provider_details_cost({"cost": bad}) is None
    assert _provider_details_cost({"cost": 0.0}) == 0.0
    assert _provider_details_cost({"cost": 0.0125}) == 0.0125
    assert _provider_details_cost({}) is None
    assert _provider_details_cost(None) is None


def test_summary_unknown_cost_counts_are_accurate() -> None:
    tracker = UsageTracker()
    tracker.add(
        CallUsageRecord(
            role="coordinator",
            model="gpt-4o",
            provider="openai-compatible",
            input_tokens=10,
            cost_usd=0.5,
            priced=True,
            cost_source="reported-native",
        )
    )
    tracker.add(
        CallUsageRecord(
            role="judge",
            model="gpt-4o-mini",
            provider="openai-compatible",
            output_tokens=5,
            cost_usd=0.25,
            priced=True,
            category="evaluation-overhead",
            cost_source="reported-wire",
        )
    )
    tracker.add(
        CallUsageRecord(
            role="coordinator",
            model="gpt-4o",
            provider="openai-compatible",
            input_tokens=7,
            cost_usd=0.0,
            priced=True,
            cost_source="reported-native",
        )
    )
    tracker.add(
        CallUsageRecord(
            role="swarm",
            model="gpt-6.1-sol",
            provider="openai-compatible",
            input_tokens=3,
            priced=False,
            cost_unknown_reason="no provider-reported cost in native session usage",
        )
    )
    tracker.add(
        CallUsageRecord(
            role="swarm",
            model="gpt-6-luna",
            provider="openai-compatible",
            input_tokens=4,
            priced=False,
            cost_unknown_reason="no provider-reported cost in wire response",
        )
    )
    summary = tracker.summary()
    assert summary["calls"] == 5
    assert summary["cost_usd"] == 0.75
    assert summary["unpriced_calls"] == 2
    assert summary["cost_sources"] == {"reported-native": 2, "reported-wire": 1}
    assert summary["unpriced_reasons"] == {
        "no provider-reported cost in native session usage": 1,
        "no provider-reported cost in wire response": 1,
    }
    assert len(summary["unpriced"]) == 2


def _effort_settings(provider: str, model: str) -> object:
    return parse_settings(
        f"""[provider]
type = "{provider}"
url = "https://endpoint.invalid/v1"
model = "{model}"
api_key = "secret-token"
[effort]
judge = "high"
"""
    )


def test_reasoning_effort_reaches_each_provider_wire_body(tmp_path: Path) -> None:
    captured: list[dict] = []

    def openai_handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1234567890,
                "model": "gpt-4o",
                "provider": "test-provider",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"ok":true,"sensitive":""}',
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
            },
        )

    complete(
        _effort_settings("openai-compatible", "gpt-4o"),
        system="Review.",
        payload={"record": "safe"},
        output=tmp_path / "openai-request",
        output_model=_SimpleSchema,
        schema_name="test_schema",
        cache_key=cache_identity("scenario-a", "judge-page", "cfg"),
        transport=httpx.MockTransport(openai_handler),
    )
    assert captured[0]["reasoning_effort"] == "high"

    captured.clear()

    def openrouter_handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return openai_handler(request)

    complete(
        _effort_settings("openrouter", "openai/gpt-4o"),
        system="Review.",
        payload={"record": "safe"},
        output=tmp_path / "openrouter-request",
        output_model=_SimpleSchema,
        schema_name="test_schema",
        cache_key=cache_identity("scenario-b", "judge-page", "cfg"),
        transport=httpx.MockTransport(openrouter_handler),
    )
    assert captured[0]["reasoning"] == {"effort": "high"}

    captured.clear()

    def anthropic_handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-5",
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "content": [{"type": "text", "text": '{"ok":true,"sensitive":""}'}],
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        )

    complete(
        _effort_settings("anthropic-compatible", "claude-sonnet-4-5"),
        system="Review.",
        payload={"record": "safe"},
        output=tmp_path / "anthropic-request",
        output_model=_SimpleSchema,
        schema_name="test_schema",
        cache_key=cache_identity("scenario-c", "judge-page", "cfg"),
        transport=httpx.MockTransport(anthropic_handler),
    )
    assert captured[0]["output_config"]["effort"] == "high"
