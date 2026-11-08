"""Pydantic AI backend with caching and provider-reported usage accounting."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
from anthropic import AsyncAnthropic
from openai import AsyncOpenAI
from pydantic import BaseModel
from pydantic_ai import Agent, CachePoint
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import UserContent
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.openrouter import OpenRouterModel
from pydantic_ai.output import NativeOutput, PromptedOutput
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openrouter import OpenRouterProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from .redaction import Redactor
from .settings import ProviderSettings

_JSON = dict[str, Any]
SIMULATOR_MAX_TOKENS = 512
JUDGE_MAX_TOKENS = 8192
COST_SOURCE_REPORTED_NATIVE = "reported-native"
COST_SOURCE_REPORTED_WIRE = "reported-wire"
COST_UNKNOWN_WIRE = "no provider-reported cost in wire response"
COST_UNKNOWN_NATIVE = "no provider-reported cost in native session usage"
_NATIVE_REPORTED_COST_KEYS = ("cost_usd", "total_cost_usd", "total_cost", "cost")


def _finite_cost_value(value: Any) -> float | None:
    """Return a finite nonnegative real cost, or None for unusable values.

    Zero is a valid reported cost. Booleans, negatives, NaN, and infinities
    are rejected so malformed provider data never poisons cost totals.
    """

    if isinstance(value, int | float) and not isinstance(value, bool):
        try:
            cost = float(value)
        except OverflowError:
            return None
        if math.isfinite(cost) and cost >= 0.0:
            return cost
    return None


def cache_identity(*parts: str) -> str:
    """Derive a stable <=64-character cache-routing identity."""

    if any(not isinstance(part, str) or not part.strip() for part in parts):
        raise ValueError("cache identity parts must be nonempty strings")
    joined = ":".join(part.strip() for part in parts)
    digest = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:24]
    prefix = ":".join(part.strip()[:16] for part in parts[:2])
    return f"jcode-eval:{prefix}:{digest}"[:64]


@dataclass(slots=True)
class CallUsageRecord:
    """Normalized usage and provider-reported USD cost for one model call."""

    role: str
    model: str
    provider: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    priced: bool = False
    completed: bool = False
    category: str = "agent"
    cost_source: str | None = None
    cost_unknown_reason: str | None = None
    session_id: str | None = None
    message_id: str | None = None


@dataclass(slots=True)
class UsageTracker:
    """Collect per-call usage across simulator, judge, and native sessions."""

    records: list[CallUsageRecord] = field(default_factory=list)
    native_messages: set[tuple[str, str]] = field(default_factory=set)

    def add(self, record: CallUsageRecord) -> None:
        self.records.append(record)

    def summary(self) -> _JSON:
        def aggregate(records: list[CallUsageRecord]) -> _JSON:
            input_tokens = sum(record.input_tokens for record in records)
            output_tokens = sum(record.output_tokens for record in records)
            cache_read_tokens = sum(record.cache_read_tokens for record in records)
            cache_write_tokens = sum(record.cache_write_tokens for record in records)
            priced_records = [
                record
                for record in records
                if record.priced and record.cost_usd is not None
            ]
            unpriced_calls = sum(
                1 for record in records if not record.priced or record.cost_usd is None
            )
            cost_sources: dict[str, int] = {}
            unpriced_reasons: dict[str, int] = {}
            for record in records:
                if record.cost_usd is not None and record.cost_source:
                    cost_sources[record.cost_source] = (
                        cost_sources.get(record.cost_source, 0) + 1
                    )
                if not record.priced or record.cost_usd is None:
                    reason = record.cost_unknown_reason or "cost unknown"
                    unpriced_reasons[reason] = unpriced_reasons.get(reason, 0) + 1
            if not records:
                cost_usd: float | None = 0.0
            elif priced_records:
                cost_usd = math.fsum(
                    float(record.cost_usd) for record in priced_records
                )
            else:
                cost_usd = None
            result: _JSON = {
                "cost_usd": cost_usd,
                "calls": len(records),
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_tokens": cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
            }
            if unpriced_calls:
                result["unpriced_calls"] = unpriced_calls
            if cost_sources:
                result["cost_sources"] = dict(sorted(cost_sources.items()))
            if unpriced_reasons:
                result["unpriced_reasons"] = dict(sorted(unpriced_reasons.items()))
            return result

        result = aggregate(self.records)
        result["unpriced"] = [
            {
                "role": record.role,
                "model": record.model,
                "session_id": record.session_id,
                "message_id": record.message_id,
                "reason": record.cost_unknown_reason or "cost unknown",
            }
            for record in self.records
            if not record.priced or record.cost_usd is None
        ]
        return result


@dataclass(init=False)
class Caching(AbstractCapability[Any]):
    """Provider-aware prompt-caching capability for Pydantic AI agents."""

    provider_type: str
    cache_key: str
    retention: str | None
    ttl: bool | str

    def __init__(
        self,
        *,
        provider_type: str,
        cache_key: str,
        retention: str | None = None,
        ttl: bool | str = True,
    ) -> None:
        super().__init__()
        if not isinstance(cache_key, str) or not cache_key.strip():
            raise ValueError("Camping/Caching requires a nonempty stable cache_key")
        if retention not in {None, "in_memory", "24h"}:
            raise ValueError("cache retention must be None, 'in_memory', or '24h'")
        if ttl not in {True, "5m", "1h"}:
            raise ValueError("cache ttl must be True, '5m', or '1h'")
        self.provider_type = provider_type
        self.cache_key = cache_key
        self.retention = retention
        self.ttl = ttl

    def get_model_settings(self) -> ModelSettings:
        if self.provider_type == "anthropic-compatible":
            return cast(
                ModelSettings,
                {
                    "anthropic_cache_instructions": self.ttl,
                    "anthropic_cache_tool_definitions": self.ttl,
                },
            )
        if self.provider_type in {"openrouter", "open-router"}:
            settings: dict[str, Any] = {
                "openai_prompt_cache_key": self.cache_key,
                "openrouter_cache_instructions": self.ttl,
                "openrouter_cache_tool_definitions": self.ttl,
                "openrouter_provider": {"require_parameters": True},
                "openrouter_usage": {"include": True},
            }
            if self.retention is not None:
                settings["openai_prompt_cache_retention"] = self.retention
            return cast(ModelSettings, settings)
        settings = {"openai_prompt_cache_key": self.cache_key}
        if self.retention is not None:
            settings["openai_prompt_cache_retention"] = self.retention
        return cast(ModelSettings, settings)


def _native_reported_cost(token_usage: _JSON) -> float | None:
    """Extract a numeric cost retained by native usage, never invented.

    Only explicit finite nonnegative numeric cost fields count. A missing,
    non-numeric, bool, negative, NaN, or infinite value means no retained
    cost; zero is a valid reported cost.
    """

    for key in _NATIVE_REPORTED_COST_KEYS:
        cost = _finite_cost_value(token_usage.get(key))
        if cost is not None:
            return cost
    return None


def _provider_details_cost(provider_details: _JSON | None) -> float | None:
    """Finite nonnegative reported cost from response provider details.

    Returns None for missing, non-numeric, bool, negative, NaN, or infinite
    values so malformed provider-reported costs stay unpriced with a safe
    reason instead of poisoning totals. Zero is a valid reported cost.
    """

    if not isinstance(provider_details, dict):
        return None
    return _finite_cost_value(provider_details.get("cost"))


def _extract_wire_usage(
    provider_type: str,
    body: _JSON | None,
) -> tuple[int, int, int, int, float | None, str | None, str | None]:
    """Extract token usage and provider-reported cost from a wire body.

    Cost comes only from provider-reported fields. A wire response without a
    usable reported cost stays unpriced with a safe reason; token counts are
    still preserved when present.
    """

    if not isinstance(body, dict):
        return 0, 0, 0, 0, None, None, "no wire response body retained"
    raw_usage = body.get("usage")
    if not isinstance(raw_usage, dict):
        return 0, 0, 0, 0, None, None, "no usage object in wire response"

    reported_cost: float | None = _finite_cost_value(raw_usage.get("cost"))
    if reported_cost is None and isinstance(raw_usage.get("cost_details"), dict):
        reported_cost = _finite_cost_value(
            raw_usage["cost_details"].get("upstream_inference_cost")
        )

    if provider_type == "anthropic-compatible" or (
        "input_tokens" in raw_usage and "prompt_tokens" not in raw_usage
    ):
        raw_in = int(raw_usage.get("input_tokens") or 0)
        out_tok = int(raw_usage.get("output_tokens") or 0)
        cache_read = int(raw_usage.get("cache_read_input_tokens") or 0)
        cache_write = int(raw_usage.get("cache_creation_input_tokens") or 0)
        in_tok = raw_in + cache_read + cache_write
    else:
        prompt_details = raw_usage.get("prompt_tokens_details")
        details = prompt_details if isinstance(prompt_details, dict) else {}
        cache_read = int(
            details.get(
                "cached_tokens",
                raw_usage.get("cache_read_input_tokens") or 0,
            )
            or 0
        )
        cache_write = int(
            details.get(
                "cache_write_tokens",
                raw_usage.get("cache_creation_input_tokens") or 0,
            )
            or 0
        )
        raw_prompt = raw_usage.get("prompt_tokens", raw_usage.get("input_tokens"))
        in_tok = int(raw_prompt or 0)
        out_tok = int(
            raw_usage.get("completion_tokens", raw_usage.get("output_tokens")) or 0
        )

    has_tokens = bool(in_tok or out_tok or cache_read or cache_write)
    if reported_cost is not None:
        return (
            in_tok,
            out_tok,
            cache_read,
            cache_write,
            reported_cost,
            COST_SOURCE_REPORTED_WIRE,
            None,
        )
    if not has_tokens:
        return 0, 0, 0, 0, None, None, "no token usage in wire response"
    return in_tok, out_tok, cache_read, cache_write, None, None, COST_UNKNOWN_WIRE


def record_native_session_usage(
    *,
    raw_dir: Path | None = None,
    settings: ProviderSettings,
    coordinator_session_id: str | None,
    tracker: UsageTracker,
    bundle: Any | None = None,
) -> None:
    """Account for the same reconstructed messages used by evidence selection."""
    if bundle is None:
        if raw_dir is None:
            raise ValueError("raw_dir or replayed bundle is required")
        from .evidence import collect_evidence

        bundle = collect_evidence(
            raw_dir=raw_dir, coordinator_session_id=coordinator_session_id,
            outer_events=(), session_listing=(),
        )
    snapshots = {
        r["session_id"]: r["data"] for r in bundle.records
        if r.get("kind") == "session-snapshot"
    }
    for record in bundle.usage_messages:
        session_id = record["session_id"]
        msg_id = record["message_id"]
        key = (session_id, msg_id)
        msg = record["data"]
        token_usage = msg.get("token_usage")
        if key in tracker.native_messages or not isinstance(token_usage, dict):
            continue
        tracker.native_messages.add(key)
        snapshot = snapshots.get(session_id, {})
        role = (
            "coordinator"
            if session_id == coordinator_session_id
            else record.get("worker", "unknown")
        )
        if role == "unknown":
            role = "swarm"
        raw_model = snapshot.get("model")
        model_name = (
            raw_model.split(":", 1)[-1]
            if isinstance(raw_model, str) and raw_model.strip()
            else settings.model_for("coordinator") if role == "coordinator"
            else settings.model_for("swarm") if role == "swarm"
            else settings.model_for_worker(role)
        )
        cache_read = int(token_usage.get("cache_read_input_tokens") or 0)
        cache_write = int(token_usage.get("cache_creation_input_tokens") or 0)
        raw_in = int(token_usage.get("input_tokens") or 0)
        prompt_tok = token_usage.get("prompt_tokens")
        if isinstance(prompt_tok, int) and not isinstance(prompt_tok, bool):
            in_tok = prompt_tok
        elif settings.provider == "anthropic-compatible":
            in_tok = raw_in + cache_read + cache_write
        else:
            in_tok = max(raw_in, cache_read + cache_write)
        cost = _native_reported_cost(token_usage)
        tracker.add(CallUsageRecord(
            role=role, model=model_name, provider=settings.provider,
            input_tokens=in_tok,
            output_tokens=int(token_usage.get("output_tokens") or 0),
            cache_read_tokens=cache_read, cache_write_tokens=cache_write,
            cost_usd=cost, priced=cost is not None, completed=True,
            cost_source=COST_SOURCE_REPORTED_NATIVE if cost is not None else None,
            cost_unknown_reason=COST_UNKNOWN_NATIVE if cost is None else None,
            session_id=session_id, message_id=msg_id,
        ))


class _WireRecordingTransport(httpx.AsyncBaseTransport):
    """Capture response usage in memory without persisting provider traffic."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        output: Path,
        redactor: Redactor,
    ) -> None:
        self._inner = inner
        del output, redactor
        self.last_request_body: _JSON | None = None
        self.last_response_body: _JSON | None = None
        self.status_code: int | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw_req = await request.aread()
        try:
            parsed_req = json.loads(raw_req.decode("utf-8"))
        except ValueError, UnicodeError:
            parsed_req = {"raw": raw_req.decode("utf-8", errors="replace")}
        if isinstance(parsed_req, dict):
            self.last_request_body = parsed_req
        response = await self._inner.handle_async_request(request)
        raw_resp = await response.aread()
        self.status_code = response.status_code
        try:
            parsed_resp = json.loads(raw_resp.decode("utf-8"))
        except ValueError, UnicodeError:
            parsed_resp = {
                "status_code": response.status_code,
                "raw": raw_resp.decode("utf-8", errors="replace"),
            }
        if isinstance(parsed_resp, dict):
            self.last_response_body = parsed_resp
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def _auth_headers(settings: ProviderSettings) -> dict[str, str]:
    headers = {"User-Agent": "jcode-evaluator/2"}
    if settings.auth == "header":
        headers[settings.auth_header or "x-api-key"] = settings.api_key
    else:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    return headers


def _anthropic_base_url(url: str) -> str:
    stripped = url.rstrip("/")
    if stripped.endswith("/v1"):
        return stripped[:-3]
    return stripped


def _build_model(
    settings: ProviderSettings,
    *,
    role: str,
    http_client: httpx.AsyncClient,
) -> Model:
    model_name = settings.model_for(role)
    headers = _auth_headers(settings)
    if settings.provider == "anthropic-compatible":
        anthropic_client = AsyncAnthropic(
            api_key=settings.api_key if settings.auth == "header" else None,
            auth_token=settings.api_key if settings.auth == "bearer" else None,
            base_url=_anthropic_base_url(settings.url),
            default_headers=headers,
            http_client=http_client,
            max_retries=0,
            timeout=float(settings.limits.turn_timeout_seconds),
        )
        anthropic_provider = AnthropicProvider(anthropic_client=anthropic_client)
        return AnthropicModel(model_name, provider=anthropic_provider)

    openai_client = AsyncOpenAI(
        api_key=settings.api_key,
        base_url=settings.url.rstrip("/"),
        default_headers=headers,
        http_client=cast(Any, http_client),
        max_retries=0,
        timeout=float(settings.limits.turn_timeout_seconds),
    )
    if settings.provider in {"openrouter", "open-router"} and "/" in model_name:
        openrouter_provider = OpenRouterProvider(openai_client=openai_client)
        base_profile = OpenRouterProvider.model_profile(model_name)
        from dataclasses import replace

        from pydantic_ai.providers.openrouter import OpenRouterModelProfile

        resolved_profile = replace(
            OpenRouterModelProfile.from_profile(base_profile),
            openrouter_supports_cache_control=True,
        )
        return OpenRouterModel(
            model_name,
            provider=openrouter_provider,
            profile=resolved_profile,
        )
    openai_provider = OpenAIProvider(openai_client=openai_client)
    return OpenAIChatModel(model_name, provider=openai_provider)


def effort_model_settings(provider_type: str, effort: str | None) -> dict[str, Any]:
    """Map a plain effort level onto the provider's real request fields.

    Inspected SDK support only: ``openai_reasoning_effort`` for
    OpenAI-compatible chat models, ``anthropic_effort`` for Anthropic
    (output-config effort), and OpenRouter's generic top-level
    ``reasoning`` object via ``extra_body``. No invented fields.
    """

    if effort is None:
        return {}
    if provider_type == "anthropic-compatible":
        return {"anthropic_effort": effort}
    if provider_type in {"openrouter", "open-router"}:
        return {"extra_body": {"reasoning": {"effort": effort}}}
    return {"openai_reasoning_effort": effort}


def run_structured(
    settings: ProviderSettings,
    *,
    role: str,
    system: str,
    user_prompt: str | Sequence[UserContent],
    output_path: Path,
    output_model: type[BaseModel],
    schema_name: str,
    cache_key: str,
    max_tokens: int | None = None,
    usage_tracker: UsageTracker | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> _JSON:
    """Run one single-request Pydantic AI structured call with zero retries."""

    redactor = Redactor((settings.api_key,))
    sanitized_system = redactor.text(system)
    sanitized_prompt: str | list[UserContent]
    if isinstance(user_prompt, str):
        sanitized_prompt = redactor.text(user_prompt)
    else:
        sanitized_prompt = [
            redactor.text(item) if isinstance(item, str) else item
            for item in user_prompt
        ]

    base_transport = transport or httpx.AsyncHTTPTransport()
    recorder = _WireRecordingTransport(
        base_transport,
        output=output_path,
        redactor=redactor,
    )
    http_client = httpx.AsyncClient(
        transport=recorder,
        timeout=float(settings.limits.turn_timeout_seconds),
    )
    model_name = settings.model_for(role)
    resolved_max_tokens = max_tokens or (
        SIMULATOR_MAX_TOKENS if role == "simulator" else JUDGE_MAX_TOKENS
    )
    caching = Caching(provider_type=settings.provider, cache_key=cache_key)
    output_spec: Any = (
        PromptedOutput(output_model, name=schema_name)
        if settings.provider == "anthropic-compatible"
        else NativeOutput(output_model, name=schema_name, strict=True)
    )
    model = _build_model(settings, role=role, http_client=http_client)
    agent: Agent[None, BaseModel] = Agent(
        model,
        instructions=sanitized_system,
        output_type=output_spec,
        retries=0,
        capabilities=[caching],
        model_settings=cast(
            ModelSettings,
            {
                "max_tokens": resolved_max_tokens,
                **effort_model_settings(settings.provider, settings.effort_for(role)),
            },
        ),
    )

    try:
        result = agent.run_sync(
            sanitized_prompt,
            usage_limits=UsageLimits(request_limit=1),
        )
    except Exception as error:
        if usage_tracker is not None and recorder.last_response_body is not None:
            in_t, out_t, cr_t, cw_t, cost, cost_source, cost_reason = (
                _extract_wire_usage(
                    settings.provider,
                    recorder.last_response_body,
                )
            )
            usage_tracker.add(
                CallUsageRecord(
                    role=role,
                    model=model_name,
                    provider=settings.provider,
                    input_tokens=in_t,
                    output_tokens=out_t,
                    cache_read_tokens=cr_t,
                    cache_write_tokens=cw_t,
                    cost_usd=cost,
                    priced=cost is not None,
                    completed=False,
                    category="evaluation-overhead",
                    cost_source=cost_source,
                    cost_unknown_reason=cost_reason,
                )
            )
        elif usage_tracker is not None and recorder.last_request_body is not None:
            usage_tracker.add(
                CallUsageRecord(
                    role=role,
                    model=model_name,
                    provider=settings.provider,
                    completed=False,
                    priced=False,
                    category="evaluation-overhead",
                    cost_unknown_reason=("provider error before usage was reported"),
                )
            )
        safe_message = redactor.text(str(error))
        if recorder.status_code is not None and recorder.status_code >= 400:
            raise RuntimeError(
                f"{role} provider HTTP {recorder.status_code}: {safe_message}"
            ) from None
        raise RuntimeError(f"{role} call failed: {safe_message}") from None

    if usage_tracker is not None:
        run_usage = result.usage
        in_t, out_t, cr_t, cw_t, wire_cost, wire_source, wire_reason = (
            _extract_wire_usage(
                settings.provider,
                recorder.last_response_body,
            )
        )
        input_tokens = run_usage.input_tokens or in_t
        output_tokens = run_usage.output_tokens or out_t
        cache_read_tokens = run_usage.cache_read_tokens or cr_t
        cache_write_tokens = run_usage.cache_write_tokens or cw_t

        resolved_cost: float | None = None
        resolved_source: str | None = None
        resolved_reason: str | None = wire_reason
        provider_details = result.response.provider_details or {}
        details_cost = _provider_details_cost(provider_details)
        if details_cost is not None:
            resolved_cost = details_cost
            resolved_source = COST_SOURCE_REPORTED_WIRE
        elif wire_cost is not None and wire_source is not None:
            resolved_cost = wire_cost
            resolved_source = wire_source
        usage_tracker.add(
            CallUsageRecord(
                role=role,
                model=model_name,
                provider=settings.provider,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                cost_usd=resolved_cost,
                priced=resolved_cost is not None,
                completed=True,
                category="evaluation-overhead",
                cost_source=resolved_source,
                cost_unknown_reason=(
                    None if resolved_cost is not None else resolved_reason
                ),
            )
        )

    output_obj = result.output
    if isinstance(output_obj, BaseModel):
        return output_obj.model_dump()
    if isinstance(output_obj, dict):
        return dict(output_obj)
    raise RuntimeError(f"{role} structured output must be an object")


__all__ = [
    "CachePoint",
    "Caching",
    "CallUsageRecord",
    "JUDGE_MAX_TOKENS",
    "SIMULATOR_MAX_TOKENS",
    "UsageTracker",
    "cache_identity",
    "record_native_session_usage",
    "run_structured",
]
