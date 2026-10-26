"""Deterministic tool, request-schema, policy, lifecycle, and hook facts."""

from __future__ import annotations

import base64
import hashlib
import json
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

_JSON = dict[str, Any]


def _blocks(message: _JSON) -> list[_JSON]:
    content = message.get("content", [])
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _outer_frames(record: _JSON) -> list[_JSON]:
    data = record.get("data", {})
    events = data.get("events") if isinstance(data, dict) else None
    if isinstance(events, list):
        return [frame for frame in events if isinstance(frame, dict)]
    return [data] if isinstance(data, dict) and data else []


def _call_id(block: _JSON) -> str | None:
    for key in ("id", "call_id", "tool_call_id", "tool_use_id"):
        value = block.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _definitions(record: _JSON) -> list[_JSON]:
    data = record.get("data", {})
    candidates = data.get("tools", data.get("tool_definitions", []))
    if isinstance(candidates, dict):
        candidates = list(candidates.values())
    if not isinstance(candidates, list):
        return []
    return [item for item in candidates if isinstance(item, dict)]


def _tool_name(definition: _JSON) -> str | None:
    for key in ("name", "id"):
        value = definition.get(key)
        if isinstance(value, str):
            return value
    function = definition.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        return str(function["name"])
    return None


def _schema_value(definition: _JSON) -> Any:
    return definition.get("input_schema", definition.get("parameters", definition))


def _schema_for(
    message: _JSON,
    session_id: str,
    tool_name: str,
    request_snapshots: Sequence[_JSON],
    list_snapshots: Sequence[_JSON],
) -> tuple[_JSON | None, str | None, str]:
    """Resolve native request_snapshot.tools before using a debug fallback."""

    reference = message.get("request_snapshot")
    if reference is not None and not isinstance(reference, dict):
        return None, "request_snapshot is not an object", "unresolved"
    tool_index = reference.get("tools") if isinstance(reference, dict) else None
    if tool_index is not None:
        if (
            isinstance(tool_index, bool)
            or not isinstance(tool_index, int)
            or tool_index < 0
        ):
            return (
                None,
                "request_snapshot.tools is not a nonnegative index",
                "unresolved",
            )
        matching = [
            record
            for record in request_snapshots
            if record.get("session_id") == session_id
            and record.get("snapshot_index") == tool_index
        ]
        if len(matching) != 1:
            return (
                None,
                f"request tool snapshot {tool_index} was not retained",
                "unresolved",
            )
        definitions = [
            definition
            for definition in _definitions(matching[0])
            if _tool_name(definition) == tool_name
        ]
        citation = str(matching[0]["id"])
        if len(definitions) == 1:
            return _schema_value(definitions[0]), None, citation
        return None, "tool is absent or duplicated in its request snapshot", citation
    fallbacks = [
        record for record in list_snapshots if record.get("session_id") == session_id
    ]
    if len(fallbacks) == 1:
        definitions = [
            definition
            for definition in _definitions(fallbacks[0])
            if _tool_name(definition) == tool_name
        ]
        if len(definitions) == 1:
            return (
                _schema_value(definitions[0]),
                "no request-specific schema reference; using point-in-time ListTools",
                str(fallbacks[0]["id"]),
            )
    return (
        None,
        "no unambiguous request-specific tool definition/schema was retained",
        "unresolved",
    )


def _policy_for(session_id: str, snapshots: Sequence[_JSON]) -> tuple[Any, str | None]:
    for record in snapshots:
        data = record.get("data", {})
        if record.get("session_id") == session_id:
            return data.get("worker_blueprint_policy"), data.get("tools")
    return None, "session policy snapshot is missing"


def _hook_artifact(path: Path) -> _JSON:
    if path.is_symlink() or not path.is_file():
        raise ValueError("retained hook payload is missing or unsafe")
    raw = path.read_bytes()
    try:
        return {"encoding": "utf-8", "text": raw.decode("utf-8")}
    except UnicodeDecodeError:
        return {"encoding": "base64", "data": base64.b64encode(raw).decode("ascii")}


def _hook_records(trace_root: Path) -> list[_JSON]:
    records: list[_JSON] = []
    if not trace_root.is_dir():
        return records
    for path in sorted(trace_root.rglob("trace.json")):
        try:
            if path.is_symlink():
                raise ValueError("symlinked hook trace")
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("hook trace is not an object")
            artifacts: dict[str, _JSON] = {}
            artifact_errors: list[str] = []
            candidates = {
                name: path.parent / name
                for name in ("stdin.bin", "stdout.bin", "stderr.bin")
            }
            for referenced in data.get("referenced_payloads", []):
                if isinstance(referenced, dict) and isinstance(
                    referenced.get("path"), str
                ):
                    candidates[referenced["path"]] = path.parent / referenced["path"]
            for name, artifact_path in candidates.items():
                if not artifact_path.exists():
                    if data.get("status", "completed") == "completed":
                        artifact_errors.append(f"{name}: payload was not retained")
                    continue
                try:
                    if not artifact_path.resolve().is_relative_to(
                        path.parent.resolve()
                    ):
                        raise ValueError("hook artifact escapes its invocation")
                    artifacts[name] = _hook_artifact(artifact_path)
                except (OSError, ValueError) as error:
                    artifact_errors.append(f"{name}: {error}")
            data = {
                **data,
                "artifacts": artifacts,
                "artifact_errors": artifact_errors,
            }
            sequence = data.get("sequence", path.parent.name)
            records.append(
                {
                    "id": f"hook:{data.get('plugin', 'unknown')}:{sequence}",
                    "kind": "hook-trace",
                    "path": path.relative_to(trace_root).as_posix(),
                    "data": data,
                }
            )
        except (OSError, ValueError, UnicodeError) as error:
            records.append(
                {
                    "id": f"hook:evidence-gap:{path.parent.name}",
                    "kind": "evidence-gap",
                    "path": path.relative_to(trace_root).as_posix(),
                    "data": {"error": str(error)},
                }
            )
    return records


def analyze_tool_health(
    records: Sequence[_JSON],
    *,
    trace_root: Path,
    evidence_gaps: Sequence[str] = (),
) -> tuple[_JSON, list[_JSON]]:
    """Return observed inventory plus supplemental hook/gap citations."""

    messages = [record for record in records if record.get("kind") == "message"]
    request_snapshots = [
        record for record in records if record.get("kind") == "request-tools"
    ]
    list_snapshots = [
        record for record in records if record.get("kind") == "effective-tools"
    ]
    session_snapshots = [
        record for record in records if record.get("kind") == "session-snapshot"
    ]
    outer = [record for record in records if record.get("kind") == "outer-event"]
    hooks = _hook_records(trace_root)
    calls: list[_JSON] = []
    by_call: dict[tuple[str, str], _JSON] = {}
    retries: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    missing: list[str] = list(evidence_gaps)
    issues: list[_JSON] = []

    for message_record in messages:
        message = message_record.get("data", {})
        session_id = str(message_record.get("session_id", "unknown"))
        for block in _blocks(message):
            if block.get("type") != "tool_use":
                continue
            native_id = _call_id(block)
            tool_name = block.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                tool_name = "unknown"
                missing.append(f"tool call in {message_record['id']} has no name")
            if native_id is None:
                native_id = (
                    "unidentified-"
                    + hashlib.sha256(
                        json.dumps(block, sort_keys=True, default=str).encode()
                    ).hexdigest()[:16]
                )
                missing.append(f"tool call in {message_record['id']} has no native ID")
            citation = f"tool:{session_id}:{native_id}"
            schema, schema_gap, schema_source = _schema_for(
                message,
                session_id,
                tool_name,
                request_snapshots,
                list_snapshots,
            )
            policy, policy_gap = _policy_for(session_id, session_snapshots)
            if schema_gap:
                missing.append(f"{citation}: {schema_gap}")
            if policy_gap:
                missing.append(f"{citation}: {policy_gap}")
            canonical_input = json.dumps(
                block.get("input"), sort_keys=True, default=str, ensure_ascii=False
            )
            retry_group = (session_id, tool_name, canonical_input)
            previous = retries[retry_group]
            call = {
                "id": citation,
                "call_id": native_id,
                "tool": tool_name,
                "session_id": session_id,
                "worker": message_record.get("worker", "unknown"),
                "input": block.get("input"),
                "message_citation": message_record["id"],
                "request_snapshot": message.get("request_snapshot"),
                "effective_schema": schema,
                "schema_source": schema_source,
                "schema_evidence_gap": schema_gap,
                "effective_policy": policy,
                "policy_evidence_gap": policy_gap,
                "retry_of": previous[-1] if previous else None,
                "attempt": len(previous) + 1,
                "result": None,
                "native_outcome": "unknown",
                "native_errors": [],
                "outer_events": [],
                "hook_traces": [],
            }
            retries[retry_group].append(citation)
            calls.append(call)
            if (session_id, native_id) in by_call:
                missing.append(f"duplicate tool call identity: {citation}")
            by_call[(session_id, native_id)] = call

    for message_record in messages:
        session_id = str(message_record.get("session_id", "unknown"))
        for block in _blocks(message_record.get("data", {})):
            if block.get("type") != "tool_result":
                continue
            call_id = _call_id(block)
            matched_call = by_call.get((session_id, call_id or ""))
            if matched_call is None:
                missing.append(
                    f"tool result {session_id}/{call_id or 'without ID'} "
                    "has no matching call"
                )
                continue
            matched_call["result"] = {
                "content": block.get("content"),
                "is_error": block.get("is_error"),
                "citation": message_record["id"],
            }
            matched_call["native_outcome"] = (
                "error" if block.get("is_error") is True else "completed"
            )
            if block.get("is_error") is True:
                matched_call["native_errors"].append(block.get("content"))

    for event_record in outer:
        for frame in _outer_frames(event_record):
            call_id = frame.get("call_id") or frame.get("tool_call_id")
            session_id = str(frame.get("session_id", "unknown"))
            matched_call = (
                by_call.get((session_id, str(call_id))) if call_id is not None else None
            )
            if matched_call is None:
                continue
            matched_call["outer_events"].append(event_record["id"])
            if frame.get("ev") == "tool_done":
                if frame.get("is_error") is True or frame.get("error"):
                    matched_call["native_outcome"] = "error"
                    matched_call["native_errors"].append(
                        frame.get("error") or frame.get("output")
                    )
                elif matched_call["native_outcome"] == "unknown":
                    matched_call["native_outcome"] = "completed"
            if frame.get("ev") == "error":
                matched_call["native_outcome"] = "error"
                matched_call["native_errors"].append(frame.get("message"))

    hook_inventory: list[_JSON] = []
    for hook in hooks:
        data = hook.get("data", {})
        if hook.get("kind") == "evidence-gap":
            missing.append(f"{hook['id']}: retained hook trace is unreadable")
        if data.get("status", "completed") != "completed":
            missing.append(f"{hook['id']}: hook invocation did not reach a final trace")
        for artifact_error in data.get("artifact_errors", []):
            missing.append(f"{hook['id']}: {artifact_error}")
        for referenced in data.get("referenced_payloads", []):
            if isinstance(referenced, dict) and referenced.get("error"):
                missing.append(
                    f"{hook['id']}: {referenced.get('field')}: {referenced['error']}"
                )
        context = data.get("context", {})
        call_id = context.get("tool_call_id") or context.get("call_id")
        session_id = context.get("session_id")
        tool_name = context.get("tool_name") or context.get("name")
        correlation = "unmatched"
        hook_call: _JSON | None = None
        if call_id is not None and session_id is not None:
            hook_call = by_call.get((str(session_id), str(call_id)))
            correlation = "native-call-id" if hook_call else "call-id-not-found"
        else:
            candidates = [
                candidate
                for candidate in calls
                if (session_id is None or candidate["session_id"] == session_id)
                and (tool_name is None or candidate["tool"] == tool_name)
            ]
            if len(candidates) == 1:
                hook_call = candidates[0]
                correlation = "unique-session-tool-match"
            elif len(candidates) > 1:
                correlation = "ambiguous-no-native-call-id"
                missing.append(
                    f"{hook['id']}: exact call correlation is unavailable; "
                    "native post-transform context has no tool_call_id"
                )
        if hook_call is not None:
            hook_call["hook_traces"].append(hook["id"])
        hook_inventory.append(
            {
                **hook,
                "data": {
                    key: value for key, value in data.items() if key != "artifacts"
                },
                "call_id": hook_call["id"] if hook_call else None,
                "call_correlation": correlation,
            }
        )
        if data.get("exit_status") not in {0, None}:
            issues.append(
                {
                    "kind": "hook-nonzero-exit",
                    "citation": hook["id"],
                    "plugin": data.get("plugin"),
                    "exit_status": data.get("exit_status"),
                }
            )

    for call in calls:
        if call["native_outcome"] == "unknown":
            missing.append(f"{call['id']}: no terminal native tool result was retained")
        if call["native_outcome"] == "error":
            issues.append(
                {
                    "kind": "native-tool-error",
                    "citation": call["message_citation"],
                    "tool_call": call["id"],
                    "errors": call["native_errors"],
                }
            )

    unique_missing = list(dict.fromkeys(missing))
    status = "issues found" if issues else "incomplete" if unique_missing else "healthy"
    report = {
        "status": status,
        "calls": calls,
        "hooks": hook_inventory,
        "issues": issues,
        "missing_evidence": unique_missing,
        "note": "Observed facts only; causal ownership requires diagnostic review.",
    }
    supplemental: list[_JSON] = list(hooks)
    for index, gap in enumerate(unique_missing):
        digest = hashlib.sha256(gap.encode()).hexdigest()[:12]
        supplemental.append(
            {
                "id": f"tool-health-gap:{index}:{digest}",
                "kind": "evidence-gap",
                "data": {"gap": gap},
            }
        )
    return report, supplemental
