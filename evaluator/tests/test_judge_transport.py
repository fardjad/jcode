from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from jcode_harness_client.judge import complete
from jcode_harness_client.llm_backend import (
    Caching,
    UsageTracker,
    cache_identity,
    record_native_session_usage,
    run_structured,
)
from jcode_harness_client.settings import parse_settings


class _SimpleSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    sensitive: str = ""


def settings(provider: str, model: str = "judge-model"):
    return parse_settings(
        f"""[provider]
type = "{provider}"
url = "https://endpoint.invalid/v1"
model = "{model}"
api_key = "secret-token"
"""
    )


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
    assert b"secret-token" not in (tmp_path / "request.input.json").read_bytes()
    saved_response = (tmp_path / "request.response.json").read_bytes()
    assert b"secret-token" not in saved_response
    assert b"[REDACTED]" in saved_response
    usage = tracker.summary()
    assert usage["calls"] == 1
    assert usage["input_tokens"] == 100
    assert usage["output_tokens"] == 20
    assert usage["cache_read_tokens"] == 60
    assert usage["cache_write_tokens"] == 10
    assert usage["cost_usd"] is not None and usage["cost_usd"] > 0


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
    assert tracker.summary()["calls"] == 1
    assert tracker.summary()["unpriced_calls"] == 1

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
    assert malformed_tracker.summary()["cost_usd"] == 0.0125


def test_native_session_usage_aggregation_and_deduplication(tmp_path: Path) -> None:
    sessions = tmp_path / "raw" / "sessions"
    sessions.mkdir(parents=True)
    parent = sessions / "session_parent.json"
    parent.write_text(
        json.dumps(
            {
                "session_id": "session_parent",
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
    parent.with_name("session_parent.journal.jsonl").write_text(
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
    worker = sessions / "session_worker.json"
    worker.write_text(
        json.dumps(
            {
                "session_id": "session_worker",
                "model": "worker_acceptance:custom-unpriced-model",
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
        coordinator_session_id="session_parent",
        tracker=tracker,
    )
    summary = tracker.summary()
    assert summary["calls"] == 3
    assert summary["input_tokens"] == 580
    assert summary["output_tokens"] == 100
    assert summary["cache_read_tokens"] == 250
    assert summary["unpriced_calls"] == 1
    assert summary["cost_usd"] is not None and summary["cost_usd"] > 0


def test_caching_capability_validation() -> None:
    with pytest.raises(ValueError, match="cache_key"):
        Caching(provider_type="openai-compatible", cache_key="")
    with pytest.raises(ValueError, match="retention"):
        Caching(
            provider_type="openai-compatible",
            cache_key="valid",
            retention="invalid",
        )
