"""Pydantic AI backend with caching, raw wire retention, and usage accounting."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
from anthropic import AsyncAnthropic
from genai_prices import calc_price
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
from pydantic_ai.usage import RequestUsage, UsageLimits

from .redaction import Redactor, write_json
from .settings import ProviderSettings

_JSON = dict[str, Any]
SIMULATOR_MAX_TOKENS = 512
JUDGE_MAX_TOKENS = 8192


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
    """Normalized usage and best-effort USD cost for one model call."""

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


@dataclass(slots=True)
class UsageTracker:
    """Collect per-call usage across simulator, judge, and native sessions."""

    records: list[CallUsageRecord] = field(default_factory=list)

    def add(self, record: CallUsageRecord) -> None:
        self.records.append(record)

    def summary(self) -> _JSON:
        input_tokens = sum(record.input_tokens for record in self.records)
        output_tokens = sum(record.output_tokens for record in self.records)
        cache_read_tokens = sum(record.cache_read_tokens for record in self.records)
        cache_write_tokens = sum(record.cache_write_tokens for record in self.records)
        priced_records = [
            record
            for record in self.records
            if record.priced and record.cost_usd is not None
        ]
        unpriced_calls = sum(
            1 for record in self.records if not record.priced or record.cost_usd is None
        )
        cost_usd: float | None
        if not self.records:
            cost_usd = 0.0
        elif priced_records:
            cost_usd = round(
                sum(float(record.cost_usd or 0.0) for record in priced_records), 8
            )
        else:
            cost_usd = None
        result: _JSON = {
            "cost_usd": cost_usd,
            "calls": len(self.records),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
        }
        if unpriced_calls:
            result["unpriced_calls"] = unpriced_calls
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


def _estimate_usage_cost(
    usage: RequestUsage,
    *,
    model_name: str,
    provider_type: str,
    provider_url: str,
) -> float | None:
    if not usage.has_values():
        return None
    candidates: list[str] = [model_name]
    if "/" in model_name:
        candidates.append(model_name.split("/", 1)[1])
    if ":" in model_name:
        candidates.append(model_name.split(":", 1)[0])
    provider_ids: list[str | None]
    if provider_type == "anthropic-compatible":
        provider_ids = ["anthropic", "openrouter", None]
    elif provider_type in {"openrouter", "open-router"}:
        provider_ids = ["openrouter", "openai", "anthropic", None]
    else:
        provider_ids = ["openai", "openrouter", "anthropic", None]
    for candidate in candidates:
        if provider_url:
            try:
                price = calc_price(
                    usage,
                    candidate,
                    provider_api_url=provider_url,
                )
                return float(price.total_price)
            except Exception:
                pass
        for provider_id in provider_ids:
            try:
                price = calc_price(
                    usage,
                    candidate,
                    provider_id=provider_id,
                )
                return float(price.total_price)
            except Exception:
                continue
    return None


def _extract_wire_usage(
    provider_type: str,
    model_name: str,
    provider_url: str,
    body: _JSON | None,
) -> tuple[int, int, int, int, float | None, bool]:
    if not isinstance(body, dict):
        return 0, 0, 0, 0, None, False
    raw_usage = body.get("usage")
    if not isinstance(raw_usage, dict):
        return 0, 0, 0, 0, None, False

    reported_cost: float | None = None
    cost_val = raw_usage.get("cost")
    if isinstance(cost_val, int | float) and not isinstance(cost_val, bool):
        reported_cost = float(cost_val)
    elif isinstance(raw_usage.get("cost_details"), dict):
        upstream = raw_usage["cost_details"].get("upstream_inference_cost")
        if isinstance(upstream, int | float) and not isinstance(upstream, bool):
            reported_cost = float(upstream)

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
        return in_tok, out_tok, cache_read, cache_write, reported_cost, True
    if not has_tokens:
        return 0, 0, 0, 0, None, False
    req_usage = RequestUsage(
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
    )
    estimated = _estimate_usage_cost(
        req_usage,
        model_name=model_name,
        provider_type=provider_type,
        provider_url=provider_url,
    )
    return (
        in_tok,
        out_tok,
        cache_read,
        cache_write,
        estimated,
        estimated is not None,
    )


def record_native_session_usage(
    *,
    raw_dir: Path,
    settings: ProviderSettings,
    coordinator_session_id: str | None,
    tracker: UsageTracker,
) -> None:
    """Aggregate native coordinator/worker token_usage without double counting."""

    session_root = raw_dir / "sessions"
    if not session_root.is_dir():
        return
    seen_messages: set[tuple[str, str]] = set()
    for path in sorted(session_root.glob("session_*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except OSError, ValueError, UnicodeError:
            continue
        if not isinstance(snapshot, dict):
            continue
        session_id = str(snapshot.get("session_id") or snapshot.get("id") or path.stem)
        role = "coordinator" if session_id == coordinator_session_id else "swarm"
        raw_model = snapshot.get("model")
        model_name = (
            str(raw_model).split(":", 1)[-1]
            if isinstance(raw_model, str) and raw_model.strip()
            else settings.model_for(role)
        )
        messages: dict[str, _JSON] = {}
        raw_messages = snapshot.get("messages")
        if isinstance(raw_messages, list):
            for msg in raw_messages:
                if isinstance(msg, dict) and isinstance(msg.get("id"), str):
                    messages[msg["id"]] = msg
        journal = path.with_name(
            (path.name[:-5] if path.name.endswith(".json") else path.stem)
            + ".journal.jsonl"
        )
        if journal.is_file() and not journal.is_symlink():
            try:
                for line in journal.read_text(encoding="utf-8").splitlines():
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        continue
                    if isinstance(entry.get("replace_messages"), list):
                        messages = {
                            str(msg["id"]): msg
                            for msg in entry["replace_messages"]
                            if isinstance(msg, dict) and isinstance(msg.get("id"), str)
                        }
                    if isinstance(entry.get("append_messages"), list):
                        for msg in entry["append_messages"]:
                            if isinstance(msg, dict) and isinstance(msg.get("id"), str):
                                msg_id = str(msg["id"])
                                if msg_id not in messages:
                                    messages[msg_id] = msg
            except OSError, ValueError, UnicodeError:
                pass

        for msg_id, msg in messages.items():
            key = (session_id, msg_id)
            if key in seen_messages:
                continue
            seen_messages.add(key)
            token_usage = msg.get("token_usage")
            if not isinstance(token_usage, dict):
                continue
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
            out_tok = int(token_usage.get("output_tokens") or 0)
            req_usage = RequestUsage(
                input_tokens=in_tok,
                output_tokens=out_tok,
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
            )
            cost_usd = _estimate_usage_cost(
                req_usage,
                model_name=model_name,
                provider_type=settings.provider,
                provider_url=settings.url,
            )
            tracker.add(
                CallUsageRecord(
                    role=role,
                    model=model_name,
                    provider=settings.provider,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cache_read_tokens=cache_read,
                    cache_write_tokens=cache_write,
                    cost_usd=cost_usd,
                    priced=cost_usd is not None,
                    completed=True,
                )
            )


class _WireRecordingTransport(httpx.AsyncBaseTransport):
    """Intercept raw HTTP wire bodies and persist sanitized evidence."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        output: Path,
        redactor: Redactor,
    ) -> None:
        self._inner = inner
        self._output = output
        self._redactor = redactor
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
        write_json(
            self._output.with_suffix(".input.json"),
            parsed_req,
            self._redactor,
        )
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
        write_json(
            self._output.with_suffix(".response.json"),
            parsed_resp,
            self._redactor,
        )
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
        model_settings=cast(ModelSettings, {"max_tokens": resolved_max_tokens}),
    )

    try:
        result = agent.run_sync(
            sanitized_prompt,
            usage_limits=UsageLimits(request_limit=1),
        )
    except Exception as error:
        if usage_tracker is not None and recorder.last_response_body is not None:
            in_t, out_t, cr_t, cw_t, cost, priced = _extract_wire_usage(
                settings.provider,
                model_name,
                settings.url,
                recorder.last_response_body,
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
                    priced=priced,
                    completed=False,
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
        in_t, out_t, cr_t, cw_t, wire_cost, wire_priced = _extract_wire_usage(
            settings.provider,
            model_name,
            settings.url,
            recorder.last_response_body,
        )
        input_tokens = run_usage.input_tokens or in_t
        output_tokens = run_usage.output_tokens or out_t
        cache_read_tokens = run_usage.cache_read_tokens or cr_t
        cache_write_tokens = run_usage.cache_write_tokens or cw_t

        cost_usd: float | None = None
        provider_details = result.response.provider_details or {}
        if isinstance(provider_details.get("cost"), int | float) and not isinstance(
            provider_details.get("cost"), bool
        ):
            cost_usd = float(provider_details["cost"])
        elif wire_cost is not None and wire_priced:
            cost_usd = wire_cost
        else:
            try:
                cost_usd = float(result.response.cost().total_price)
            except Exception:
                req_usage = RequestUsage(
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cache_read_tokens=cache_read_tokens,
                    cache_write_tokens=cache_write_tokens,
                )
                cost_usd = _estimate_usage_cost(
                    req_usage,
                    model_name=model_name,
                    provider_type=settings.provider,
                    provider_url=settings.url,
                )
        usage_tracker.add(
            CallUsageRecord(
                role=role,
                model=model_name,
                provider=settings.provider,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                cost_usd=cost_usd,
                priced=cost_usd is not None,
                completed=True,
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
