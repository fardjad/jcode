"""Complete native evidence retention, stable citations, and pagination."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
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
    "env_snapshots": "append_env_snapshots",
    "memory_injections": "append_memory_injections",
    "replay_events": "append_replay_events",
}


@dataclass(slots=True)
class EvidenceBundle:
    records: list[_JSON]
    sessions: list[_JSON]
    gaps: list[str]

    @property
    def citations(self) -> set[str]:
        return {str(record["id"]) for record in self.records}

    @property
    def complete(self) -> bool:
        return not self.gaps


@dataclass(frozen=True, slots=True)
class EvidencePage:
    index: int
    records: tuple[_JSON, ...]
    byte_count: int
    sha256: str
    oversized_single_record: bool

    @property
    def record_ids(self) -> list[str]:
        return [str(record["id"]) for record in self.records]


@dataclass(slots=True)
class _Replay:
    messages: dict[str, _JSON] = field(default_factory=dict)
    vectors: dict[str, list[Any]] = field(
        default_factory=lambda: {name: [] for name in _VECTORS}
    )
    policy: _JSON | None = None
    parent_id: str | None = None
    compaction: Any = None


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
    """Retain only native evidence allowlists after process shutdown."""

    gaps: list[str] = []
    copied: list[_JSON] = []
    for source, category in (
        (home / "sessions", "sessions"),
        (home / "logs", "logs"),
        (runtime_dir / "jcode-tool-payloads", "hook-payloads"),
        (runtime_dir / "durable-state", "durable-state"),
    ):
        if not source.exists():
            # Empty runtimes need not have payload or durable-state directories.
            if category in {"sessions", "logs"}:
                gaps.append(f"native evidence directory is missing: {source}")
            continue
        new_gaps, new_copies = retain_tree(
            source,
            raw_dir,
            redactor,
            skip_private=True,
            category=category,
        )
        gaps.extend(new_gaps)
        copied.extend(new_copies)
    for name in ("daemon.log", "bridge.log"):
        source = home / name
        if not source.is_file() or source.is_symlink():
            gaps.append(f"native log is missing or unsafe: {name}")
            continue
        try:
            payload = source.read_bytes()
            write_bytes(raw_dir / "logs" / name, payload, redactor)
        except OSError as error:
            gaps.append(f"unreadable native log {name}: {error.strerror}")
            continue
        copied.append(
            {
                "source": name,
                "path": f"logs/{name}",
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
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
        compaction=snapshot.get("compaction"),
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
    append_ids: list[str],
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
    if not meta.get("compaction_unchanged", False):
        replay.compaction = meta.get("compaction")


def _vector_records(session_id: str, worker: str, replay: _Replay) -> list[_JSON]:
    records: list[_JSON] = []
    styles = {
        "request_prompt_snapshots": ("request-prompt", "prompt"),
        "request_tool_snapshots": ("request-tools", "tools"),
        "env_snapshots": ("environment", "snapshot"),
        "memory_injections": ("memory", "snapshot"),
        "replay_events": ("replay", "events"),
    }
    for vector_name, values in replay.vectors.items():
        kind, payload_key = styles[vector_name]
        for index, value in enumerate(values):
            records.append(
                {
                    "id": f"session:{session_id}:{kind}:{index}",
                    "kind": kind,
                    "session_id": session_id,
                    "worker": worker,
                    "snapshot_index": index,
                    "data": {
                        "session_id": session_id,
                        "snapshot_index": index,
                        payload_key: value,
                    },
                }
            )
    return records


def _auxiliary_records(raw_dir: Path, gaps: list[str]) -> list[_JSON]:
    records: list[_JSON] = []
    for root, kind in (
        (raw_dir / "durable-state", "durable-state"),
        (raw_dir / "logs", "native-log"),
    ):
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            if kind == "native-log" and path.name.startswith("acceptance."):
                continue
            relative = path.relative_to(raw_dir).as_posix()
            citation = f"{kind}:{relative}"
            try:
                raw = path.read_bytes()
                if kind == "durable-state" and path.suffix == ".json":
                    data: Any = json.loads(raw)
                else:
                    data = raw.decode("utf-8", errors="replace")
                records.append(
                    {"id": citation, "kind": kind, "path": relative, "data": data}
                )
            except (OSError, ValueError) as error:
                gaps.append(f"unreadable auxiliary evidence {relative}: {error}")
    return records


def collect_evidence(
    *,
    raw_dir: Path,
    coordinator_session_id: str | None,
    outer_events: Sequence[_JSON],
    session_listing: Sequence[_JSON],
    tool_snapshots: Sequence[_JSON] = (),
    retention_gaps: Sequence[str] = (),
) -> EvidenceBundle:
    """Replay native snapshots/journals while preserving every raw entry."""

    records: list[_JSON] = []
    sessions: list[_JSON] = []
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
                        add(
                            {
                                "id": citation,
                                "kind": "journal-entry",
                                "session_id": session_id,
                                "journal_position": position,
                                "replay_status": "stale-prefix" if stale else "applied",
                                "data": entry,
                            }
                        )
                        if not stale:
                            seen_new = True
                            _apply_entry(
                                entry, replay, session_id, position, gaps, append_ids
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
        listing = listing_by_id.get(session_id, {})
        parent_session_id = listing.get("parent_session_id")
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
            **snapshot,
            "session_id": session_id,
            "parent_id": replay.parent_id,
            "worker_blueprint_policy": replay.policy,
            "messages": list(replay.messages.values()),
            **replay.vectors,
            "compaction": replay.compaction,
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
        for record in _vector_records(session_id, worker, replay):
            add(record)
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
    for record in _auxiliary_records(raw_dir, gaps):
        add(record)
    return EvidenceBundle(records=records, sessions=sessions, gaps=gaps)


def paginate(records: Sequence[_JSON], maximum_bytes: int) -> list[EvidencePage]:
    """Page every record without truncation; one huge record forms one page."""

    if maximum_bytes < 1:
        raise ValueError("evidence page size must be positive")
    pages: list[EvidencePage] = []
    current: list[_JSON] = []
    current_size = 0

    def encoded_size(items: Iterable[_JSON]) -> int:
        return len(
            json.dumps(list(items), ensure_ascii=False, separators=(",", ":")).encode()
        )

    def finish() -> None:
        nonlocal current, current_size
        if not current:
            return
        encoded = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
        pages.append(
            EvidencePage(
                index=len(pages),
                records=tuple(current),
                byte_count=len(encoded.encode()),
                sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                oversized_single_record=len(current) == 1
                and encoded_size(current) > maximum_bytes,
            )
        )
        current = []
        current_size = 0

    for record in records:
        size = encoded_size([record])
        if current and current_size + size > maximum_bytes:
            finish()
        current.append(record)
        current_size = encoded_size(current)
    finish()
    return pages


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
