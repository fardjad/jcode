"""Deterministic behavior-focused judge projection with raw retrieval index."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

_JSON = dict[str, Any]
_DIFF_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")
_EXCLUDED_BY_DEFAULT_KINDS = {
    "source-diff": "bulky source diff; file summary is projected",
    "request-prompt": (
        "static request prompt snapshot; blueprint and config snapshots are projected"
    ),
    "request-tools": (
        "raw request tool table; correlated tool schemas are projected in tool_health"
    ),
    "effective-tools": (
        "point-in-time tool table; correlated tool schemas are projected in tool_health"
    ),
    "native-log": "bulky native log; status and errors are projected",
    "journal-entry": (
        "raw journal entry; replayed session messages and metadata are projected"
    ),
    "outer-event": (
        "raw streaming frame bundle; correlated tool/turn outcomes are projected"
    ),
}


@dataclass(frozen=True, slots=True)
class JudgeProjection:
    records: list[_JSON]
    excluded: list[_JSON]
    raw_by_citation: dict[str, _JSON]


def _diff_summary(patch_text: str) -> _JSON:
    files: list[_JSON] = []
    current: _JSON | None = None
    for line in patch_text.splitlines():
        match = _DIFF_HEADER.match(line)
        if match:
            if current is not None:
                files.append(current)
            current = {
                "path": match.group(2),
                "status": "modified",
                "added_lines": 0,
                "removed_lines": 0,
                "binary": False,
            }
            continue
        if current is None:
            continue
        if line == "new file":
            current["status"] = "added"
        elif line == "deleted file":
            current["status"] = "deleted"
        elif line.startswith("Binary files differ"):
            current["binary"] = True
        elif line.startswith("+") and not line.startswith("+++"):
            current["added_lines"] += 1
        elif line.startswith("-") and not line.startswith("---"):
            current["removed_lines"] += 1
    if current is not None:
        files.append(current)
    return {
        "changed_files": [item["path"] for item in files],
        "file_stats": files,
        "byte_count": len(patch_text.encode("utf-8")),
        "sha256": hashlib.sha256(patch_text.encode("utf-8")).hexdigest(),
    }


def _project_message_block(
    block: _JSON, raw_citation: str
) -> tuple[_JSON | None, str | None]:
    block_type = block.get("type")
    if block_type == "reasoning":
        return None, "raw model reasoning blob"
    if block_type == "text":
        return {"type": "text", "text": block.get("text", "")}, None
    if block_type == "tool_use":
        return (
            {
                "type": "tool_use",
                "id": block.get("id")
                or block.get("call_id")
                or block.get("tool_call_id"),
                "name": block.get("name"),
                "input": block.get("input"),
            },
            None,
        )
    if block_type == "tool_result":
        content = block.get("content")
        is_error = bool(block.get("is_error"))
        content_text = (
            content
            if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False, default=str)
        )
        encoded = content_text.encode("utf-8")
        if is_error:
            return (
                {
                    "type": "tool_result",
                    "tool_use_id": (
                        block.get("tool_use_id")
                        or block.get("call_id")
                        or block.get("id")
                    ),
                    "is_error": True,
                    "error": content_text,
                    "byte_count": len(encoded),
                    "raw_citation": raw_citation,
                    "retrievable": True,
                },
                None,
            )
        return (
            {
                "type": "tool_result_ref",
                "tool_use_id": (
                    block.get("tool_use_id") or block.get("call_id") or block.get("id")
                ),
                "is_error": False,
                "byte_count": len(encoded),
                "line_count": len(content_text.splitlines()),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "raw_citation": raw_citation,
                "content_policy": "omitted-tool-output",
                "retrievable": True,
            },
            "bulky non-error tool result output",
        )
    return (
        {
            "type": str(block_type or "unknown"),
            "raw_citation": raw_citation,
            "retrievable": True,
        },
        None,
    )


def _project_message(record: _JSON) -> tuple[_JSON, list[str]]:
    raw_citation = str(record["id"])
    message = record.get("data", {})
    content = message.get("content", [])
    projected_blocks: list[Any] = []
    omitted_reasons: list[str] = []
    if isinstance(content, str):
        projected_blocks.append({"type": "text", "text": content})
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                projected_blocks.append(block)
                continue
            projected_block, omitted = _project_message_block(block, raw_citation)
            if projected_block is not None:
                projected_blocks.append(projected_block)
            if omitted is not None:
                omitted_reasons.append(omitted)
    policy = "curated-message" if omitted_reasons else "complete"
    return (
        {
            "id": raw_citation,
            "raw_citation": raw_citation,
            "kind": "message",
            "session_id": record.get("session_id"),
            "parent_session_id": record.get("parent_session_id"),
            "parent_id": record.get("parent_id"),
            "worker": record.get("worker"),
            "message_id": record.get("message_id"),
            "content_policy": policy,
            "retrievable": True,
            "data": {
                "id": message.get("id"),
                "role": message.get("role"),
                "display_role": message.get("display_role"),
                "request_snapshot": message.get("request_snapshot"),
                "token_usage": message.get("token_usage"),
                "content": projected_blocks,
            },
        },
        omitted_reasons,
    )


def _project_tool_call(call: _JSON) -> _JSON:
    result = call.get("result")
    result_summary: _JSON | None = None
    if isinstance(result, dict):
        raw_content = result.get("content")
        text = (
            raw_content
            if isinstance(raw_content, str)
            else json.dumps(raw_content, ensure_ascii=False, default=str)
        )
        encoded = text.encode("utf-8")
        result_summary = {
            "is_error": bool(result.get("is_error")),
            "citation": result.get("citation"),
            "byte_count": len(encoded),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "retrievable": True,
        }
        if result.get("is_error"):
            result_summary["error"] = text
    return {
        "id": call.get("id"),
        "raw_citation": call.get("message_citation") or call.get("id"),
        "call_id": call.get("call_id"),
        "tool": call.get("tool"),
        "session_id": call.get("session_id"),
        "worker": call.get("worker"),
        "input": call.get("input"),
        "message_citation": call.get("message_citation"),
        "request_snapshot": call.get("request_snapshot"),
        "schema_source": call.get("schema_source"),
        "schema_evidence_gap": call.get("schema_evidence_gap"),
        "effective_policy": call.get("effective_policy"),
        "policy_evidence_gap": call.get("policy_evidence_gap"),
        "retry_of": call.get("retry_of"),
        "attempt": call.get("attempt"),
        "native_outcome": call.get("native_outcome"),
        "native_errors": call.get("native_errors", []),
        "outer_events": call.get("outer_events", []),
        "hook_traces": call.get("hook_traces", []),
        "result_summary": result_summary,
        "content_policy": "curated-tool-call",
        "retrievable": True,
    }


def _project_metric(record: _JSON) -> tuple[_JSON, list[str]]:
    raw_citation = str(record["id"])
    data = record.get("data", {})
    if record.get("metric") != "tool_health" or not isinstance(data, dict):
        return (
            {
                **record,
                "raw_citation": raw_citation,
                "content_policy": "complete",
                "retrievable": True,
            },
            [],
        )
    calls = [
        _project_tool_call(call)
        for call in data.get("calls", [])
        if isinstance(call, dict)
    ]
    observed_schemas: dict[str, Any] = {}
    for call in data.get("calls", []):
        if isinstance(call, dict) and isinstance(call.get("tool"), str):
            tool_name = str(call["tool"])
            if (
                tool_name not in observed_schemas
                and call.get("effective_schema") is not None
            ):
                observed_schemas[tool_name] = call["effective_schema"]
    return (
        {
            "id": raw_citation,
            "raw_citation": raw_citation,
            "kind": "metric",
            "metric": "tool_health",
            "content_policy": "curated-metric",
            "retrievable": True,
            "data": {
                "status": data.get("status"),
                "observed_schemas": observed_schemas,
                "calls": calls,
                "hooks": data.get("hooks", []),
                "issues": data.get("issues", []),
                "missing_evidence": data.get("missing_evidence", []),
                "note": data.get("note"),
            },
        },
        ["bulky non-error tool result bodies in tool_health.calls"],
    )


def build_judge_projection(records: Sequence[_JSON]) -> JudgeProjection:
    """Create a deterministic behavior-focused projection and raw citation map."""

    projected: list[_JSON] = []
    excluded: list[_JSON] = []
    raw_by_citation: dict[str, _JSON] = {}

    for record in records:
        citation = str(record["id"])
        raw_by_citation[citation] = record
        kind = str(record.get("kind") or "unknown")
        data = record.get("data")

        if kind == "message" and isinstance(data, dict):
            for block in (
                data.get("content", []) if isinstance(data.get("content"), list) else []
            ):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    call_id = (
                        block.get("id")
                        or block.get("call_id")
                        or block.get("tool_call_id")
                    )
                    session_id = record.get("session_id")
                    if (
                        isinstance(call_id, str)
                        and call_id
                        and isinstance(session_id, str)
                    ):
                        tool_citation = f"tool:{session_id}:{call_id}"
                        raw_by_citation.setdefault(tool_citation, record)

        if kind in _EXCLUDED_BY_DEFAULT_KINDS:
            reason = _EXCLUDED_BY_DEFAULT_KINDS[kind]
            excluded.append(
                {
                    "raw_citation": citation,
                    "kind": kind,
                    "reason": reason,
                    "retrievable": True,
                }
            )
            if kind == "source-diff" and isinstance(data, dict):
                patch_text = str(data.get("patch") or "")
                projected.append(
                    {
                        "id": citation,
                        "raw_citation": citation,
                        "kind": "source-diff-summary",
                        "content_policy": "summary-only",
                        "retrievable": True,
                        "data": _diff_summary(patch_text),
                    }
                )
            continue

        if kind == "acceptance" and isinstance(data, dict):
            has_logs = bool(data.get("stdout") or data.get("stderr"))
            if has_logs:
                excluded.append(
                    {
                        "raw_citation": citation,
                        "kind": "acceptance-logs",
                        "reason": "bulky acceptance stdout/stderr logs",
                        "retrievable": True,
                    }
                )
            projected.append(
                {
                    "id": citation,
                    "raw_citation": citation,
                    "kind": "acceptance",
                    "content_policy": "status-only" if has_logs else "complete",
                    "retrievable": True,
                    "data": {
                        key: value
                        for key, value in data.items()
                        if key not in {"stdout", "stderr"}
                    },
                }
            )
            continue

        if kind == "session-snapshot" and isinstance(data, dict):
            excluded.append(
                {
                    "raw_citation": citation,
                    "kind": "session-snapshot-vectors",
                    "reason": "raw session vectors and duplicate message arrays",
                    "retrievable": True,
                }
            )
            projected.append(
                {
                    "id": citation,
                    "raw_citation": citation,
                    "kind": "session-policy",
                    "session_id": record.get("session_id"),
                    "worker": record.get("worker"),
                    "content_policy": "policy-and-metadata",
                    "retrievable": True,
                    "data": {
                        "session_id": data.get("session_id"),
                        "parent_id": data.get("parent_id"),
                        "model": data.get("model"),
                        "status": data.get("status"),
                        "worker_blueprint_policy": data.get("worker_blueprint_policy"),
                        "compaction": data.get("compaction"),
                    },
                }
            )
            continue

        if kind == "hook-trace" and isinstance(data, dict):
            if data.get("artifacts"):
                excluded.append(
                    {
                        "raw_citation": citation,
                        "kind": "hook-trace-artifacts",
                        "reason": "raw hook stdin/stdout/stderr and payload binaries",
                        "retrievable": True,
                    }
                )
            projected.append(
                {
                    "id": citation,
                    "raw_citation": citation,
                    "kind": "hook-trace",
                    "path": record.get("path"),
                    "content_policy": "metadata-only",
                    "retrievable": True,
                    "data": {
                        key: value for key, value in data.items() if key != "artifacts"
                    },
                }
            )
            continue

        if kind == "message" and isinstance(data, dict):
            projected_msg, omitted = _project_message(record)
            projected.append(projected_msg)
            for reason in dict.fromkeys(omitted):
                excluded.append(
                    {
                        "raw_citation": citation,
                        "kind": "message-content",
                        "reason": reason,
                        "retrievable": True,
                    }
                )
            continue

        if kind == "metric":
            projected_metric, omitted = _project_metric(record)
            projected.append(projected_metric)
            for reason in dict.fromkeys(omitted):
                excluded.append(
                    {
                        "raw_citation": citation,
                        "kind": "metric-detail",
                        "reason": reason,
                        "retrievable": True,
                    }
                )
            continue

        projected.append(
            {
                **record,
                "raw_citation": citation,
                "content_policy": "complete",
                "retrievable": True,
            }
        )

    return JudgeProjection(
        records=projected,
        excluded=excluded,
        raw_by_citation=raw_by_citation,
    )


def resolve_raw_citations(
    requested: Sequence[str],
    raw_by_citation: dict[str, _JSON],
) -> list[_JSON]:
    """Validate and fetch exact raw records for requested citations."""

    if not isinstance(requested, Sequence) or isinstance(requested, str | bytes):
        raise RuntimeError("retrieval_requests must be a citation list")
    resolved: list[_JSON] = []
    seen: set[str] = set()
    for citation in requested:
        if not isinstance(citation, str) or not citation.strip():
            raise RuntimeError("retrieval citation must be a nonempty string")
        if citation not in raw_by_citation:
            raise RuntimeError(
                f"judge requested unknown raw evidence citation: {citation}"
            )
        if citation not in seen:
            seen.add(citation)
            resolved.append(raw_by_citation[citation])
    return resolved
