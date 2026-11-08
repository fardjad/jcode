"""Replay native evidence and retain only cited slices and native journals."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .redaction import Redactor, write_bytes, write_json, write_text

_JSON = dict[str, Any]
_SKIPPED_PRIVATE_NAMES = {
    "config.toml",
    "credentials.json",
    "auth.json",
    "tokens.json",
    "token.json",
}
_VECTORS = {
    "request_prompt_snapshots": "append_request_prompt_snapshots",
    "request_tool_snapshots": "append_request_tool_snapshots",
}


def _redact_serialized(raw: bytes, redactor: Redactor) -> bytes:
    raw = redactor.data(raw)
    for secret in redactor.secrets:
        if secret:
            for ascii_only in (True, False):
                escaped = json.dumps(secret, ensure_ascii=ascii_only)[1:-1]
                for variant in (escaped, escaped.replace("/", r"\/")):
                    raw = raw.replace(variant.encode(), b"[REDACTED]")
    return raw


@dataclass(slots=True)
class EvidenceBundle:
    records: list[_JSON]
    sessions: list[_JSON]
    gaps: list[str]
    usage_messages: list[_JSON] = field(default_factory=list)

    @property
    def citations(self) -> set[str]:
        return {str(record["id"]) for record in self.records}

    @property
    def complete(self) -> bool:
        return not self.gaps

    @property
    def citation_map(self) -> dict[str, _JSON]:
        from .tool_health import native_calls

        result = {record["id"]: record for record in self.records}
        for record in self.records:
            if record.get("kind") == "session-snapshot":
                result[record["session_id"]] = record
        for call in native_calls(self.records):
            if call.get("call_id"):
                result[f"tool:{call['session_id']}:{call['call_id']}"] = call
        return result


def write_cited_evidence(
    output: Path,
    citation_map: Mapping[str, Any],
    findings: Sequence[_JSON],
    journal_root: Path,
    redactor: Redactor,
) -> list[str]:
    """Persist cited records and native journals from ephemeral collection.

    The caller owns the temporary source and its lifetime. Never walk or prune
    the destination: it may contain user data. Missing citations are reported.
    """
    refs = sorted(
        {
            ref
            for finding in findings
            for ref in finding.get("citations", [])
            if isinstance(ref, str) and ref
        }
    )
    missing = [ref for ref in refs if ref not in citation_map]
    selected = [
        {"id": ref, "source": citation_map[ref]}
        for ref in refs
        if ref in citation_map
    ]
    if selected:
        write_text(
            output / "raw" / "cited.jsonl",
            "".join(
                json.dumps(redactor.value(record), ensure_ascii=False) + "\n"
                for record in selected
            ),
        )
    if journal_root.is_symlink():
        return [*missing, "unsafe native journal directory"]
    if not journal_root.is_dir():
        return [*missing, "native journal directory is missing"]
    for path in sorted(journal_root.glob("session_*.journal.jsonl")):
        if path.is_symlink() or not path.is_file():
            missing.append(f"unsafe native journal: {path.name}")
            continue
        try:
            write_bytes(
                output / "raw" / "sessions" / path.name,
                _redact_serialized(path.read_bytes(), redactor),
            )
        except OSError:
            missing.append(f"unreadable native journal: {path.name}")
    return missing


@dataclass(slots=True)
class _Replay:
    messages: dict[str, _JSON] = field(default_factory=dict)
    vectors: dict[str, list[Any]] = field(
        default_factory=lambda: {name: [] for name in _VECTORS}
    )
    policy: _JSON | None = None
    parent_id: str | None = None


def _destination(raw_dir: Path, relative: Path, category: str | None) -> Path:
    if category is not None:
        return raw_dir / category / relative
    parts = relative.parts
    if parts[0] == "sessions":
        return raw_dir / "sessions" / Path(*parts[1:])
    if "hook-traces" in parts:
        index = parts.index("hook-traces")
        return raw_dir / "hook-traces" / Path(*parts[index + 1 :])
    for name in ("hook-payloads", "hook_payloads", "jcode-tool-payloads"):
        if name in parts:
            index = parts.index(name)
            return raw_dir / "hook-payloads" / Path(*parts[index + 1 :])
    if relative.suffix == ".log":
        return raw_dir / "logs" / relative
    return raw_dir / "other" / relative


def retain_tree(
    source_root: Path,
    raw_dir: Path,
    redactor: Redactor,
    *,
    skip_private: bool,
    category: str | None = None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Copy complete regular-file evidence without following symlink escapes."""

    gaps: list[str] = []
    copied: list[dict[str, Any]] = []
    if source_root.is_symlink() or not source_root.is_dir():
        return [f"runtime evidence directory missing or unsafe: {source_root}"], copied
    for source in sorted(source_root.rglob("*")):
        relative = source.relative_to(source_root)
        if source.is_symlink():
            gaps.append(f"symlinked runtime evidence rejected: {relative}")
            continue
        if source.is_dir():
            continue
        if not source.is_file():
            copied.append(
                {
                    "source": relative.as_posix(),
                    "skipped": "non-regular runtime endpoint",
                }
            )
            continue
        if skip_private and source.name in _SKIPPED_PRIVATE_NAMES:
            copied.append(
                {"source": relative.as_posix(), "skipped": "private configuration"}
            )
            continue
        target = _destination(raw_dir, relative, category)
        try:
            payload = source.read_bytes()
            write_bytes(target, payload, redactor)
            copied.append(
                {
                    "source": relative.as_posix(),
                    "path": target.relative_to(raw_dir).as_posix(),
                    "bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
            )
        except OSError as error:
            gaps.append(f"unreadable runtime evidence {relative}: {error.strerror}")
    return gaps, copied


def retain_native_runtime(
    *,
    home: Path,
    runtime_dir: Path,
    raw_dir: Path,
    redactor: Redactor,
) -> tuple[list[str], list[_JSON]]:
    """Stage native snapshots and journals after shutdown, before temp cleanup.

    ``raw_dir`` must be temporary collection storage. Persist journals and
    cited slices separately with :func:`write_cited_evidence` after review.
    """
    del runtime_dir
    gaps: list[str] = []
    copied: list[_JSON] = []
    source = home / "sessions"
    if source.is_symlink() or not source.is_dir():
        return [f"native evidence directory is missing or unsafe: {source}"], copied
    for path in sorted(source.glob("session_*")):
        if not (path.name.endswith(".json") or path.name.endswith(".journal.jsonl")):
            continue
        if path.is_symlink() or not path.is_file():
            gaps.append(f"unsafe native session evidence: {path.name}")
            continue
        try:
            write_bytes(
                raw_dir / "sessions" / path.name,
                _redact_serialized(path.read_bytes(), redactor),
            )
        except OSError as error:
            gaps.append(f"unreadable native session {path.name}: {error.strerror}")
            continue
        copied.append({"source": path.name, "path": f"sessions/{path.name}"})
    return gaps, copied


def _event_id(frame: _JSON) -> str:
    session = str(frame.get("session_id", "unattributed"))
    event = str(frame.get("ev", "unknown"))
    native = (
        frame.get("message_id") or frame.get("call_id") or frame.get("tool_call_id")
    )
    suffix = (
        str(native)
        if native
        else hashlib.sha256(
            json.dumps(frame, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
    )
    return f"outer:{session}:{event}:{suffix}"


def _worker_name(policy: _JSON | None, session_id: str, coordinator: str | None) -> str:
    if session_id == coordinator:
        return "coordinator"
    if policy and isinstance(policy.get("name"), str):
        return str(policy["name"])
    return "unknown"


def _message_map(
    value: Any,
    session_id: str,
    source: str,
    gaps: list[str],
) -> dict[str, _JSON]:
    if value is None:
        return {}
    if not isinstance(value, list):
        gaps.append(f"{source}: messages for {session_id} are not a list")
        return {}
    messages: dict[str, _JSON] = {}
    for index, message in enumerate(value):
        if not isinstance(message, dict) or not isinstance(message.get("id"), str):
            gaps.append(f"{source}: message {index} in {session_id} has no native ID")
            continue
        message_id = str(message["id"])
        if message_id in messages:
            gaps.append(
                f"{source}: duplicate native message ID {message_id} in {session_id}"
            )
        messages[message_id] = message
    return messages


def _journal_path(snapshot: Path) -> Path:
    name = snapshot.name[:-5] if snapshot.name.endswith(".json") else snapshot.stem
    return snapshot.with_name(name + ".journal.jsonl")


def _initial_replay(snapshot: _JSON, session_id: str, gaps: list[str]) -> _Replay:
    replay = _Replay(
        messages=_message_map(
            snapshot.get("messages"), session_id, "session snapshot", gaps
        ),
        policy=snapshot.get("worker_blueprint_policy")
        if isinstance(snapshot.get("worker_blueprint_policy"), dict)
        else None,
        parent_id=snapshot.get("parent_id")
        if isinstance(snapshot.get("parent_id"), str)
        else None,
    )
    for name in _VECTORS:
        value = snapshot.get(name, [])
        if not isinstance(value, list):
            gaps.append(f"snapshot {name} for {session_id} is not a list")
        else:
            replay.vectors[name] = list(value)
    return replay


def _append_message_ids(
    entry: _JSON, gaps: list[str], session: str, position: int
) -> list[str]:
    value = entry.get("append_messages", [])
    if not isinstance(value, list):
        gaps.append(f"journal {position} append_messages for {session} is not a list")
        return []
    ids: list[str] = []
    for message in value:
        if isinstance(message, dict) and isinstance(message.get("id"), str):
            ids.append(message["id"])
        else:
            gaps.append(
                f"journal {position} has a message without native ID in {session}"
            )
    return ids


def _stale_prefix(
    entry: _JSON,
    replay: _Replay,
    snapshot_updated: Any,
    seen_new: bool,
    append_ids: list[str],
) -> bool:
    if seen_new or "replace_messages" in entry:
        return False
    if append_ids:
        return all(message_id in replay.messages for message_id in append_ids)
    meta = entry.get("meta")
    updated = meta.get("updated_at") if isinstance(meta, dict) else None
    if (
        isinstance(updated, int | float)
        and not isinstance(updated, bool)
        and isinstance(snapshot_updated, int | float)
        and not isinstance(snapshot_updated, bool)
    ):
        return updated <= snapshot_updated
    return False


def _apply_entry(
    entry: _JSON,
    replay: _Replay,
    session_id: str,
    position: int,
    gaps: list[str],
) -> None:
    if "replace_messages" in entry:
        replay.messages = _message_map(
            entry["replace_messages"], session_id, f"journal {position}", gaps
        )
    appended = entry.get("append_messages", [])
    if isinstance(appended, list):
        for message in appended:
            if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                continue
            message_id = message["id"]
            # Native replay removes duplicate IDs inside a genuinely new entry.
            if message_id not in replay.messages or "replace_messages" in entry:
                replay.messages[message_id] = message
    for name, append_key in _VECTORS.items():
        if append_key not in entry:
            continue
        values = entry[append_key]
        if not isinstance(values, list):
            gaps.append(
                f"journal {position} {append_key} for {session_id} is not a list"
            )
            continue
        replay.vectors[name].extend(values)
    meta = entry.get("meta")
    if not isinstance(meta, dict):
        return
    policy = meta.get("worker_blueprint_policy")
    if isinstance(policy, dict):
        replay.policy = policy
    if isinstance(meta.get("parent_id"), str):
        replay.parent_id = meta["parent_id"]


def collect_evidence(
    *,
    raw_dir: Path,
    coordinator_session_id: str | None,
    outer_events: Sequence[_JSON],
    session_listing: Sequence[_JSON],
    tool_snapshots: Sequence[_JSON] = (),
    retention_gaps: Sequence[str] = (),
) -> EvidenceBundle:
    """Replay native snapshots/journals once from ephemeral storage."""

    records: list[_JSON] = []
    sessions: list[_JSON] = []
    usage_messages: list[_JSON] = []
    gaps = list(retention_gaps)
    used_ids: set[str] = set()

    def add(record: _JSON) -> None:
        citation = str(record["id"])
        if citation in used_ids:
            digest = hashlib.sha256(
                json.dumps(record, sort_keys=True, default=str).encode()
            ).hexdigest()[:12]
            gaps.append(f"duplicate evidence citation: {citation}")
            record = {**record, "id": f"{citation}:duplicate:{digest}"}
        used_ids.add(str(record["id"]))
        records.append(record)

    normalized_outer: list[_JSON] = []
    grouped_outer: dict[str, _JSON] = {}
    for frame in outer_events:
        citation = _event_id(frame)
        group = grouped_outer.get(citation)
        if group is None:
            group = {
                "id": citation,
                "kind": "outer-event",
                "session_id": frame.get("session_id"),
                "data": {"events": [frame]},
            }
            grouped_outer[citation] = group
            normalized_outer.append(group)
        else:
            group["data"]["events"].append(frame)
    for record in normalized_outer:
        add(record)
    for snapshot in tool_snapshots:
        session_id = str(snapshot.get("session_id", "unknown"))
        phase = str(snapshot.get("phase", "point-in-time"))
        add(
            {
                "id": f"session:{session_id}:effective-tools:{phase}",
                "kind": "effective-tools",
                "session_id": session_id,
                "data": snapshot,
            }
        )

    listing_by_id: dict[str, _JSON] = {}
    for item in session_listing:
        listed_session_id = item.get("session_id")
        if isinstance(listed_session_id, str):
            listing_by_id[listed_session_id] = item
            citation = f"session-listing:{listed_session_id}"
        else:
            citation = (
                "session-listing:"
                + hashlib.sha256(
                    json.dumps(item, sort_keys=True, default=str).encode()
                ).hexdigest()[:16]
            )
            gaps.append("native session listing entry has no session_id")
        add(
            {
                "id": citation,
                "kind": "session-listing",
                "session_id": listed_session_id,
                "data": item,
            }
        )

    session_root = raw_dir / "sessions"
    paths = sorted(session_root.glob("session_*.json")) if session_root.is_dir() else []
    snapshot_ids: set[str] = set()
    for path in paths:
        fallback_id = path.name[:-5] if path.name.endswith(".json") else path.stem
        try:
            if path.is_symlink():
                raise ValueError("symlinked native session")
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(snapshot, dict):
                raise ValueError("native session is not an object")
        except (OSError, ValueError, UnicodeError) as error:
            gaps.append(f"unreadable native session {fallback_id}: {error}")
            add(
                {
                    "id": f"evidence-gap:session:{fallback_id}",
                    "kind": "evidence-gap",
                    "session_id": fallback_id,
                    "data": {"path": str(path), "error": str(error)},
                }
            )
            continue
        session_id = str(
            snapshot.get("session_id") or snapshot.get("id") or fallback_id
        )
        snapshot_ids.add(session_id)
        replay = _initial_replay(snapshot, session_id, gaps)
        usage_by_id = dict(replay.messages)
        journal = _journal_path(path)
        seen_new = False
        if journal.exists():
            try:
                if journal.is_symlink():
                    raise ValueError("symlinked native journal")
                lines = journal.read_text(encoding="utf-8").splitlines()
                for position, line in enumerate(lines, start=1):
                    citation = f"session:{session_id}:journal:{position}"
                    try:
                        entry = json.loads(line)
                        if not isinstance(entry, dict):
                            raise ValueError("journal entry is not an object")
                        for key in ("replace_messages", "append_messages"):
                            values = entry.get(key, [])
                            if isinstance(values, list):
                                for message in values:
                                    if isinstance(message, dict) and isinstance(
                                        message.get("id"), str
                                    ):
                                        previous = usage_by_id.get(message["id"])
                                        if previous is None or not isinstance(
                                            previous.get("token_usage"), dict
                                        ):
                                            usage_by_id[message["id"]] = message
                        append_ids = _append_message_ids(
                            entry, gaps, session_id, position
                        )
                        stale = _stale_prefix(
                            entry,
                            replay,
                            snapshot.get("updated_at"),
                            seen_new,
                            append_ids,
                        )
                        if not stale:
                            seen_new = True
                            _apply_entry(
                                entry, replay, session_id, position, gaps
                            )
                    except (ValueError, UnicodeError) as error:
                        gaps.append(
                            f"invalid journal entry {position} "
                            f"for {session_id}: {error}"
                        )
                        add(
                            {
                                "id": citation,
                                "kind": "evidence-gap",
                                "session_id": session_id,
                                "journal_position": position,
                                "data": {"raw": line, "error": str(error)},
                            }
                        )
            except (OSError, ValueError, UnicodeError) as error:
                gaps.append(f"unreadable native journal for {session_id}: {error}")
        worker = _worker_name(replay.policy, session_id, coordinator_session_id)
        usage_messages.extend(
            {
                "session_id": session_id,
                "message_id": message_id,
                "worker": worker,
                "data": message,
            }
            for message_id, message in usage_by_id.items()
            if isinstance(message.get("token_usage"), dict)
        )
        listing = listing_by_id.get(session_id, {})
        # Safe fallback only: when the listing carries no usable report-back
        # parent, the actual transcript parent is the native ancestry fact.
        # Both raw fields stay retained; nothing is fabricated when both miss.
        listed_parent = listing.get("parent_session_id")
        parent_session_id: str | None
        if isinstance(listed_parent, str) and listed_parent:
            parent_session_id = listed_parent
        else:
            parent_session_id = replay.parent_id
        edit_stats = listing.get("edit_stats")
        edit_sidecar = session_root / "edit-stats" / f"{session_id}.json"
        if (
            edit_stats is None
            and edit_sidecar.is_file()
            and not edit_sidecar.is_symlink()
        ):
            try:
                edit_stats = json.loads(edit_sidecar.read_text(encoding="utf-8"))
                add(
                    {
                        "id": f"session:{session_id}:edit-stats",
                        "kind": "edit-stats",
                        "session_id": session_id,
                        "data": edit_stats,
                    }
                )
            except (OSError, ValueError) as error:
                gaps.append(f"unreadable edit stats for {session_id}: {error}")
        sessions.append(
            {
                "session_id": session_id,
                "parent_session_id": parent_session_id,
                "parent_id": replay.parent_id,
                "worker": worker,
                "agent_label": listing.get("agent_label"),
                "swarm_status": listing.get("swarm_status"),
                "status": listing.get("status"),
                "worker_blueprint_policy": replay.policy,
                "edit_stats": edit_stats,
                "snapshot_path": path.relative_to(raw_dir).as_posix(),
            }
        )
        reconstructed = {
            "session_id": session_id,
            "parent_id": replay.parent_id,
            "worker_blueprint_policy": replay.policy,
            "model": snapshot.get("model"),
            "reasoning_effort": snapshot.get("reasoning_effort"),
        }
        add(
            {
                "id": f"session:{session_id}:snapshot",
                "kind": "session-snapshot",
                "session_id": session_id,
                "worker": worker,
                "raw_path": path.relative_to(raw_dir).as_posix(),
                "data": reconstructed,
            }
        )
        for index, tools in enumerate(replay.vectors["request_tool_snapshots"]):
            add({
                "id": f"session:{session_id}:request-tools:{index}",
                "kind": "request-tools",
                "session_id": session_id,
                "worker": worker,
                "snapshot_index": index,
                "data": {"tools": tools},
            })
        for message_id, message in replay.messages.items():
            reference = message.get("request_snapshot")
            if reference is not None and not isinstance(reference, dict):
                gaps.append(
                    f"{session_id}/{message_id}: request_snapshot is not an object"
                )
            if isinstance(reference, dict):
                for key, vector_name in (
                    ("prompt", "request_prompt_snapshots"),
                    ("tools", "request_tool_snapshots"),
                ):
                    index = reference.get(key)
                    if index is None:
                        continue
                    if (
                        isinstance(index, bool)
                        or not isinstance(index, int)
                        or index < 0
                        or index >= len(replay.vectors[vector_name])
                    ):
                        gaps.append(
                            f"{session_id}/{message_id}: {key} request snapshot "
                            f"reference {index!r} is unresolved"
                        )
            add(
                {
                    "id": f"session:{session_id}:message:{message_id}",
                    "kind": "message",
                    "session_id": session_id,
                    "parent_session_id": parent_session_id,
                    "parent_id": replay.parent_id,
                    "worker": worker,
                    "message_id": message_id,
                    "data": message,
                }
            )

    for session_id in listing_by_id:
        if session_id not in snapshot_ids:
            gaps.append(f"listed native session has no retained snapshot: {session_id}")
    if coordinator_session_id and coordinator_session_id not in snapshot_ids:
        gaps.append("coordinator native session snapshot is missing")
    if not paths:
        gaps.append("no native session snapshots were retained")
    return EvidenceBundle(
        records=records, sessions=sessions, gaps=gaps, usage_messages=usage_messages
    )


def format_transcript(records: Sequence[_JSON]) -> str:
    """Render every reconstructed turn without shortening tool content."""

    lines = ["# Coordinator and worker transcript", ""]
    current_session: str | None = None
    for item in records:
        kind = item.get("kind")
        if kind == "request-prompt":
            lines.extend(
                [
                    f"## [{item['id']}] Actual request prompt snapshot",
                    "",
                    str(item.get("data", {}).get("prompt", "")),
                    "",
                ]
            )
            continue
        if kind != "message":
            continue
        session_id = str(item.get("session_id", "unknown"))
        if session_id != current_session:
            current_session = session_id
            worker = item.get("worker", "unknown")
            lines.extend(
                [
                    f"## Session `{session_id}` ({worker})",
                    "",
                    f"Report-back parent: `{item.get('parent_session_id')}`; "
                    f"transcript parent: `{item.get('parent_id')}`",
                    "",
                ]
            )
        message = item.get("data", {})
        role = message.get("display_role") or message.get("role", "unknown")
        lines.extend([f"### [{item['id']}] {role}", ""])
        content = message.get("content", [])
        if isinstance(content, str):
            lines.extend([content, ""])
            continue
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                lines.extend([json.dumps(block, ensure_ascii=False), ""])
                continue
            block_type = block.get("type")
            if block_type in {"text", "reasoning"}:
                label = "Reasoning" if block_type == "reasoning" else "Text"
                lines.extend([f"**{label}:**", "", str(block.get("text", "")), ""])
            elif block_type == "tool_use":
                lines.extend(
                    [
                        f"**Tool call `{block.get('name', '')}` "
                        f"ID `{block.get('id', block.get('call_id', ''))}`:**",
                        "```json",
                        json.dumps(
                            block.get("input", {}), indent=2, ensure_ascii=False
                        ),
                        "```",
                        "",
                    ]
                )
            elif block_type == "tool_result":
                lines.extend(
                    [
                        f"**Tool result (error={block.get('is_error', False)}):**",
                        "```text",
                        str(block.get("content", "")),
                        "```",
                        "",
                    ]
                )
            else:
                lines.extend(
                    [
                        "```json",
                        json.dumps(block, indent=2, ensure_ascii=False),
                        "```",
                        "",
                    ]
                )
    return "\n".join(lines)


def write_bundle(output: Path, bundle: EvidenceBundle, redactor: Redactor) -> None:
    write_text(
        output / "transcript.jsonl",
        "".join(
            json.dumps(redactor.value(record), ensure_ascii=False) + "\n"
            for record in bundle.records
        ),
    )
    write_text(output / "transcript.md", format_transcript(bundle.records), redactor)
    write_json(
        output / "sessions.json",
        {"sessions": bundle.sessions, "evidence_gaps": bundle.gaps},
        redactor,
    )
