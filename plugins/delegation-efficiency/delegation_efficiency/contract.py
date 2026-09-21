"""Strict validation for jcode.delegation-efficiency.v1."""
from __future__ import annotations
import json
import re
from typing import Any

ENVELOPE = "jcode.delegation-efficiency.v1"
SCHEMA_VERSION = "1.0"
SEMANTICS_VERSION = "part1-runtime-1"
MAX_ENVELOPE_BYTES = 16 * 1024
MAX_STRING_BYTES = 256
MAX_ID_BYTES = 128
MAX_TIMESTAMP_UNIX_MS = 4_102_444_800_000
EVENT_KINDS = {"tool_attempt", "tool_result", "provider_usage", "delegation_spawn", "delegation_follow_up", "delegation_summary", "child_session", "observer_health", "communication_observation"}
ID_FIELDS = {"event_id", "session_id", "tool_invocation_id", "provider_tool_call_id", "guard_event_id", "delegation_id", "parent_session_id", "child_session_id", "request_id", "provider_response_id", "generation_id", "blueprint_id"}
ALLOWED_FIELDS = {"envelope", "schema_version", "semantics_version", "event", "event_id", "occurred_at_unix_ms", "session_id", "tool_invocation_id", "provider_tool_call_id", "guard_event_id", "delegation_id", "parent_session_id", "child_session_id", "request_id", "provider_response_id", "served_model", "generation_id", "deletion_generation", "attempt", "process_id", "blueprint_id", "blueprint_name", "spawn_mode", "tool_name", "provider", "route", "model", "process_role", "outcome", "guard_outcome", "failure_reason", "retryable", "retry_count", "cache_read_input_tokens", "cache_creation_input_tokens", "cost_micros", "upstream_inference_cost_micros", "upstream_inference_prompt_cost_micros", "upstream_inference_completions_cost_micros", "discount_micros", "cost_currency", "cost_source", "cost_status", "cost_version", "cost_timestamp_unix_ms", "original_bytes", "original_lines", "original_tokens", "final_visible_bytes", "final_visible_lines", "final_visible_tokens", "provider_input_tokens", "provider_output_tokens", "intermediate", "nudge", "final", "context_outcome", "refusal_outcome", "transform_outcome", "threshold_classification", "threshold_count", "transformer_classification", "transformer_count", "lifecycle", "status", "direction", "communication_kind", "bytes", "tokens", "evidence_level", "denominator_name", "denominator_numerator", "denominator_value", "tokenizer_identity", "tokenizer_status", "evidence", "attribution_status", "tool_latency_ms"}
SOURCE_FIELDS = frozenset(ALLOWED_FIELDS)
REQUIRED_FIELDS = {"envelope", "schema_version", "semantics_version", "event", "event_id", "occurred_at_unix_ms", "session_id", "tool_invocation_id", "tool_name", "evidence_level", "tokenizer_identity", "tokenizer_status", "evidence", "attribution_status", "tool_latency_ms"}
FORBIDDEN_NAMES = {"prompt", "tool_input", "tool_result", "content", "summary", "report", "metadata", "path", "file_path", "command", "query", "task"}
INTEGER_FIELDS = {"occurred_at_unix_ms", "deletion_generation", "attempt", "process_id", "retry_count", "cache_read_input_tokens", "cache_creation_input_tokens", "cost_micros", "upstream_inference_cost_micros", "upstream_inference_prompt_cost_micros", "upstream_inference_completions_cost_micros", "discount_micros", "cost_timestamp_unix_ms", "original_bytes", "original_lines", "original_tokens", "final_visible_bytes", "final_visible_lines", "final_visible_tokens", "provider_input_tokens", "provider_output_tokens", "threshold_count", "transformer_count", "denominator_numerator", "denominator_value", "bytes", "tokens", "tool_latency_ms"}
BOOLEAN_FIELDS = {"retryable", "intermediate", "nudge", "final"}
MAX_GENERATION = 2**63 - 1

# Every string in the envelope is either an identifier, a closed vocabulary
# value, or a structural name.  There are deliberately no free-form strings.
# In particular, failure reasons and labels must not become a covert channel
# for prompts, paths, hashes, or provider response text.
ENUM_FIELDS = {
    "envelope": {ENVELOPE},
    "schema_version": {SCHEMA_VERSION},
    "semantics_version": {SEMANTICS_VERSION},
    "event": EVENT_KINDS,
    "spawn_mode": {"auto", "headless", "inline", "visible"},
    "outcome": {"aborted", "cancelled", "completed", "error", "failed", "success", "unknown"},
    "guard_outcome": {"applied", "below_threshold", "context_guard_refused", "intercepted", "unknown"},
    "failure_reason": {"aborted", "cancelled", "invalid_request", "network_error", "not_found", "permission_denied", "provider_error", "rate_limited", "timeout", "tool_error", "tool_failed", "unknown"},
    "cost_currency": {"AUD", "CAD", "CHF", "CNY", "EUR", "GBP", "JPY", "USD"},
    "cost_source": {"provider_response", "provider_reported", "openrouter_response", "unknown"},
    "cost_status": {"provider_reported", "unknown"},
    "context_outcome": {"allowed", "blocked", "below_threshold", "context_guard_refused", "intercepted", "applied", "unknown"},
    "refusal_outcome": {"refused", "not_refused", "unknown"},
    "transform_outcome": {"applied", "not_applied", "unknown"},
    "threshold_classification": {"below_threshold", "context_guard_refused", "intercepted"},
    "transformer_classification": {"applied", "not_applied"},
    "lifecycle": EVENT_KINDS | {"completed", "provider_usage"},
    "status": {"aborted", "cancelled", "completed", "error", "failed", "success", "unknown"} | EVENT_KINDS,
    "evidence_level": {"estimated", "estimated_or_null", "measured", "provider_reported", "unknown"},
    "tokenizer_status": {"approximate", "provider_reported", "unavailable"},
    "evidence": {"estimated", "estimated_or_null", "measured", "provider_reported", "unknown"},
    "attribution_status": {"eligible", "not_applicable", "unknown_linkage"},
    "direction": {"inbound", "outbound"},
    "communication_kind": {"clarification", "context_read", "escalation", "follow_up", "retry", "summary"},
}
NAME_FIELDS = {
    "served_model", "blueprint_name", "tool_name", "provider", "route", "model",
    "process_role", "cost_version", "denominator_name", "tokenizer_identity",
}
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}\Z")
HASH_VALUE = re.compile(r"(?:[0-9a-fA-F]{32,}|(?:sha|md5|blake)[0-9a-zA-Z:_-]+)\Z", re.IGNORECASE)
CONTENT_TOKENS = {"command", "content", "file", "hash", "input", "metadata", "output", "path", "prompt", "query", "report", "result", "secret", "summary", "task"}


def _structural_name(value: str, field: str) -> None:
    if not SAFE_NAME.fullmatch(value) or value.startswith("/") or ".." in value or "//" in value or HASH_VALUE.fullmatch(value):
        raise EnvelopeError(f"invalid structural {field}")
    # Tool names may legitimately be `read_file` or `write_output`; their
    # grammar, rather than a substring blacklist, prevents content injection.
    # Other names are metadata dimensions and use a closed content-token check.
    tokens = re.split(r"[._:/@+\-]+", value.lower())
    if any(token in CONTENT_TOKENS for token in tokens) and (field != "tool_name" or len(tokens) == 1):
        raise EnvelopeError(f"content-like {field}")


def _validate_string_value(value: str, field: str) -> None:
    _string(value, field, MAX_ID_BYTES if field in ID_FIELDS else MAX_STRING_BYTES)
    if field in ID_FIELDS:
        if (any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for ch in value)
                or ".." in value or HASH_VALUE.fullmatch(value)
                or any(token in CONTENT_TOKENS for token in re.split(r"[._:/@+\-]+", value.lower()))):
            raise EnvelopeError(f"invalid {field}")
    elif field in ENUM_FIELDS:
        if value not in ENUM_FIELDS[field]:
            raise EnvelopeError(f"invalid {field}")
    elif field in NAME_FIELDS:
        _structural_name(value, field)
    else:
        raise EnvelopeError(f"unclassified string field {field}")

class EnvelopeError(ValueError): pass

def _string(value: Any, field: str, limit: int) -> None:
    if not isinstance(value, str) or not value or len(value.encode()) > limit or any(ord(c) < 32 for c in value):
        raise EnvelopeError(f"invalid {field}")

def validate_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict): raise EnvelopeError("event must be an object")
    if len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode()) > MAX_ENVELOPE_BYTES: raise EnvelopeError("event exceeds size limit")
    if not REQUIRED_FIELDS.issubset(value): raise EnvelopeError("missing required envelope field")
    if (value.get("envelope"), value.get("schema_version"), value.get("semantics_version")) != (ENVELOPE, SCHEMA_VERSION, SEMANTICS_VERSION): raise EnvelopeError("unsupported envelope version")
    _string(value["event"], "event", MAX_STRING_BYTES)
    if value["event"] not in EVENT_KINDS: raise EnvelopeError("unknown event kind")
    for key, item in value.items():
        if key not in ALLOWED_FIELDS: raise EnvelopeError("unknown or non-allowlisted envelope field")
        if key in FORBIDDEN_NAMES: raise EnvelopeError("content-bearing field")
        if isinstance(item, (dict, list)): raise EnvelopeError("nested values are not allowed")
        if item is not None and key in INTEGER_FIELDS and (not isinstance(item, int) or isinstance(item, bool) or item < 0): raise EnvelopeError(f"invalid {key}")
        if item is not None and key in BOOLEAN_FIELDS and not isinstance(item, bool): raise EnvelopeError(f"invalid {key}")
        if item is not None and key not in INTEGER_FIELDS and key not in BOOLEAN_FIELDS:
            if not isinstance(item, str):
                raise EnvelopeError(f"invalid {key}")
            _validate_string_value(item, key)
    for key in ID_FIELDS:
        if value.get(key) is not None:
            _string(value[key], key, MAX_ID_BYTES)
            if any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-" for ch in value[key]): raise EnvelopeError(f"invalid {key}")
    timestamp = value["occurred_at_unix_ms"]
    if not isinstance(timestamp, int) or isinstance(timestamp, bool) or not 0 <= timestamp <= MAX_TIMESTAMP_UNIX_MS: raise EnvelopeError("invalid timestamp")
    generation = value.get("deletion_generation")
    if generation is not None and generation > MAX_GENERATION: raise EnvelopeError("invalid deletion_generation")
    latency = value["tool_latency_ms"]
    if not isinstance(latency, int) or isinstance(latency, bool) or latency < 0: raise EnvelopeError("invalid latency")
    for key, item in value.items():
        if key in {"attempt", "retry_count", "threshold_count", "transformer_count"} and item is not None and item > 10000: raise EnvelopeError(f"invalid {key}")
    if value["event"] == "provider_usage" and any(value.get(k) is None for k in ("provider", "model", "attribution_status")): raise EnvelopeError("provider usage requires provider, model, and attribution_status")
    if value["event"] == "communication_observation":
        if any(value.get(k) is None for k in ("direction", "communication_kind", "bytes", "tokens")):
            raise EnvelopeError("communication observation requires typed measurements")
    return dict(value)

def parse_event(raw: str | bytes) -> dict[str, Any]:
    try: return validate_event(json.loads(raw))
    except (TypeError, json.JSONDecodeError) as exc: raise EnvelopeError("invalid JSON") from exc

def compatibility(value: Any) -> str:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION: return "unsupported_schema"
    if value.get("semantics_version") != SEMANTICS_VERSION: return "unsupported_semantics"
    if value.get("event") not in EVENT_KINDS: return "unknown_event_kind"
    if any(key not in ALLOWED_FIELDS for key in value): return "unknown_field"
    return "current"
