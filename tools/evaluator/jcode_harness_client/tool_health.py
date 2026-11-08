"""Select native tool failures with their actual request schema and policy."""

from __future__ import annotations

import base64
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_JSON = dict[str, Any]


def _blocks(message: _JSON) -> list[_JSON]:
    content = message.get("content", [])
    return (
        [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
    )


def _call_id(block: _JSON) -> str | None:
    for key in ("tool_use_id", "tool_call_id", "call_id", "id"):
        value = block.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _definitions(record: _JSON) -> list[_JSON]:
    data = record.get("data", {})
    candidates = data.get("tools", data.get("tool_definitions", []))
    if isinstance(candidates, dict):
        candidates = list(candidates.values())
    return (
        [d for d in candidates if isinstance(d, dict)]
        if isinstance(candidates, list)
        else []
    )


def _schema_for(
    message: _JSON,
    session_id: str,
    tool_name: str,
    request_snapshots: Sequence[_JSON],
    list_snapshots: Sequence[_JSON],
) -> tuple[Any, str | None, str | None]:
    """A missing referenced request never falls back to a different schema."""
    reference = message.get("request_snapshot")
    if reference is not None and not isinstance(reference, dict):
        return None, "request_snapshot is not an object", None
    index = reference.get("tools") if isinstance(reference, dict) else None
    if index is not None:
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            return None, "request_snapshot.tools is not a nonnegative index", None
        matches = [
            r
            for r in request_snapshots
            if r.get("session_id") == session_id and r.get("snapshot_index") == index
        ]
        gap = None
    else:
        matches = [r for r in list_snapshots if r.get("session_id") == session_id]
        gap = "no request-specific schema reference; using point-in-time ListTools"
    if len(matches) != 1:
        return (
            None,
            "no unambiguous request-specific tool definition/schema was retained",
            None,
        )
    definitions = []
    for definition in _definitions(matches[0]):
        function = definition.get("function", definition)
        if (
            isinstance(function, dict)
            and function.get("name", function.get("id")) == tool_name
        ):
            definitions.append(function)
    ref = matches[0].get("id")
    if len(definitions) != 1:
        return None, "tool is absent or duplicated in its request snapshot", ref
    definition = definitions[0]
    schema = definition.get("input_schema", definition.get("parameters"))
    if not isinstance(schema, dict):
        return None, "effective tool schema is missing", ref
    return schema, gap, ref


def native_calls(records: Sequence[_JSON]) -> list[_JSON]:
    """Join by exact (session, call ID); keep order for resume observations."""
    requests = [r for r in records if r.get("kind") == "request-tools"]
    lists = [r for r in records if r.get("kind") == "effective-tools"]
    policies = {
        r.get("session_id"): r for r in records if r.get("kind") == "session-snapshot"
    }
    calls: list[_JSON] = []
    results: dict[tuple[Any, Any], list[_JSON]] = {}
    for order, record in enumerate(records):
        if record.get("kind") != "message":
            continue
        sid, message = record.get("session_id"), record.get("data", {})
        if not isinstance(sid, str) or not isinstance(message, dict):
            continue
        for block in _blocks(message):
            cid = _call_id(block)
            if block.get("type") == "tool_result":
                results.setdefault((sid, cid), []).append(
                    {
                        **block,
                        "native_ref": record.get("id"),
                        "order": order,
                    }
                )
            if block.get("type") != "tool_use":
                continue
            schema, gap, schema_ref = _schema_for(
                message, sid, block.get("name"), requests, lists
            )
            policy_record = policies.get(sid, {})
            policy = policy_record.get("data", {}).get("worker_blueprint_policy")
            refs = [record.get("id"), schema_ref, policy_record.get("id")]
            if cid:
                refs.append(f"tool:{sid}:{cid}")
            calls.append(
                {
                    "session_id": sid,
                    "call_id": cid,
                    "tool": block.get("name"),
                    "input": block.get("input"),
                    "effective_schema": schema,
                    "effective_policy": policy,
                    "schema_evidence_gap": gap,
                    "native_refs": [r for r in refs if r],
                    "order": order,
                    "result": None,
                    "native_outcome": "unknown",
                    "error": [],
                }
            )
    identities = [(c["session_id"], c["call_id"]) for c in calls]
    for call in calls:
        key = (call["session_id"], call["call_id"])
        matches = results.get(key, [])
        if call["call_id"] and identities.count(key) == 1 and len(matches) == 1:
            result = matches[0]
            if result["order"] >= call["order"]:
                call["result"] = result
                call["native_refs"].append(result["native_ref"])
                call["native_outcome"] = (
                    "error" if result.get("is_error") is True else "completed"
                )
                if result.get("is_error") is True:
                    call["error"].append(result.get("content"))
        elif not call["call_id"] or identities.count(key) != 1 or len(matches) > 1:
            call["schema_evidence_gap"] = (
                "missing or ambiguous native call/result identity"
            )
        for record in records:
            if record.get("kind") != "outer-event":
                continue
            data = record.get("data", {})
            if not isinstance(data, dict):
                continue
            frames = data.get("events", [data])
            if not isinstance(frames, list):
                continue
            for frame in frames:
                if (
                    not isinstance(frame, dict)
                    or not call["call_id"]
                    or identities.count(key) != 1
                    or frame.get("session_id") != call["session_id"]
                    or _call_id(frame) != call["call_id"]
                ):
                    continue
                if frame.get("ev") in {"error", "tool_done"} and (
                    frame.get("is_error") is True
                    or frame.get("error")
                    or frame.get("ev") == "error"
                ):
                    call["native_outcome"] = "error"
                    call["error"].append(
                        frame.get("error")
                        or frame.get("message")
                        or frame.get("output")
                    )
                    call["native_refs"].append(record["id"])
    return calls


def _hook_records(trace_root: Path) -> list[_JSON]:
    """Read only failed/interrupted hooks, retaining safe invocation payloads."""
    hooks = []
    for path in sorted(trace_root.rglob("trace.json")):
        try:
            if path.is_symlink():
                raise ValueError("symlinked hook trace")
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("hook trace is not an object")
            if (
                data.get("exit_status") in {0, None}
                and data.get("status", "completed") == "completed"
            ):
                continue
            artifacts = {}
            artifact_errors = []
            names = ["stdin.bin", "stdout.bin", "stderr.bin"]
            names.extend(
                r["path"]
                for r in data.get("referenced_payloads", [])
                if isinstance(r, dict) and isinstance(r.get("path"), str)
            )
            for name in names:
                artifact = path.parent / name
                if artifact.is_symlink() or not artifact.resolve().is_relative_to(
                    path.parent.resolve()
                ):
                    raise ValueError("unsafe hook payload")
                if not artifact.is_file():
                    artifact_errors.append(f"{name}: hook payload was not retained")
                    continue
                raw = artifact.read_bytes()
                try:
                    artifacts[name] = {"encoding": "utf-8", "text": raw.decode("utf-8")}
                except UnicodeDecodeError:
                    artifacts[name] = {
                        "encoding": "base64",
                        "data": base64.b64encode(raw).decode("ascii"),
                    }
            hooks.append(
                {
                    "id": (
                        f"hook:{data.get('plugin', 'unknown')}:"
                        f"{data.get('sequence', path.parent.name)}"
                    ),
                    "kind": "hook-trace",
                    "data": {
                        **data,
                        "artifacts": artifacts,
                        "artifact_errors": artifact_errors,
                    },
                }
            )
        except (OSError, ValueError, UnicodeError) as error:
            hooks.append(
                {
                    "id": f"hook:evidence-gap:{path.parent.name}",
                    "kind": "evidence-gap",
                    "data": {"error": str(error)},
                }
            )
    return hooks


def select_tool_errors(
    records: Sequence[_JSON],
    *,
    trace_root: Path,
    calls: list[_JSON] | None = None,
) -> tuple[list[_JSON], list[_JSON], list[_JSON]]:
    calls = native_calls(records) if calls is None else calls
    identities = [(c["session_id"], c["call_id"]) for c in calls]
    hooks = _hook_records(trace_root)
    errors, gaps = [], []
    for call in calls:
        related = [
            h
            for h in hooks
            if h["data"].get("context", {}).get("session_id") == call["session_id"]
            and _call_id(h["data"].get("context", {})) == call["call_id"]
            and call["call_id"]
            and identities.count((call["session_id"], call["call_id"])) == 1
        ]
        if call["native_outcome"] != "error" and not related:
            continue
        refs = call["native_refs"] + [h["id"] for h in related]
        errors.append(
            {
                key: call[key]
                for key in (
                    "session_id",
                    "call_id",
                    "tool",
                    "input",
                    "effective_schema",
                    "effective_policy",
                    "error",
                )
            }
            | {
                "related_hook_error": related,
                "native_refs": refs,
            }
        )
        reasons = [call["schema_evidence_gap"]]
        if not isinstance(call["effective_policy"], dict):
            reasons.append("session policy snapshot is missing")
        for reason in filter(None, reasons):
            gaps.append({"reason": reason, "native_refs": refs})
    correlated = {h["id"] for error in errors for h in error["related_hook_error"]}
    for hook in hooks:
        if hook["id"] not in correlated:
            errors.append(
                {
                    "session_id": hook["data"].get("context", {}).get("session_id"),
                    "call_id": None,
                    "tool": None,
                    "input": None,
                    "effective_schema": None,
                    "effective_policy": None,
                    "error": hook["data"],
                    "related_hook_error": [hook],
                    "native_refs": [hook["id"]],
                }
            )
            gaps.append(
                {
                    "reason": "hook failure lacks exact native call context",
                    "native_refs": [hook["id"]],
                }
            )
        if hook["data"].get("status", "completed") != "completed":
            gaps.append(
                {
                    "reason": "hook invocation did not reach a final trace",
                    "native_refs": [hook["id"]],
                }
            )
        for reason in hook["data"].get("artifact_errors", []):
            gaps.append({"reason": reason, "native_refs": [hook["id"]]})
    for record in records:
        if record.get("kind") != "message" or not isinstance(record.get("data"), dict):
            continue
        for block in _blocks(record["data"]):
            cid, sid = _call_id(block), record.get("session_id")
            if (
                block.get("type") != "tool_result"
                or block.get("is_error") is not True
                or (cid and identities.count((sid, cid)) == 1)
            ):
                continue
            refs = [record["id"]]
            errors.append(
                {
                    "session_id": sid,
                    "call_id": cid,
                    "tool": None,
                    "input": None,
                    "effective_schema": None,
                    "effective_policy": None,
                    "error": [block.get("content")],
                    "related_hook_error": [],
                    "native_refs": refs,
                }
            )
            gaps.append(
                {
                    "reason": (
                        "native error result lacks unambiguous original call context"
                    ),
                    "native_refs": refs,
                }
            )
    return errors, hooks, gaps


def analyze_tool_health(
    records: Sequence[_JSON],
    *,
    trace_root: Path,
    evidence_gaps: Sequence[Any] = (),
) -> tuple[_JSON, list[_JSON]]:
    """Legacy runner adapter; 'observed' is not an independent health verdict."""
    errors, hooks, gaps = select_tool_errors(records, trace_root=trace_root)
    legacy_calls = [
        {
            **error,
            "id": next(
                (r for r in error["native_refs"] if r.startswith("tool:")), None
            ),
            "message_citation": error["native_refs"][0],
            "native_errors": error["error"],
            "native_outcome": "error",
        }
        for error in errors
    ]
    return {
        "status": "observed",
        "tool_errors": errors,
        "calls": legacy_calls,
        "hooks": hooks,
        "issues": [],
        "missing_evidence": [*evidence_gaps, *gaps],
    }, hooks
