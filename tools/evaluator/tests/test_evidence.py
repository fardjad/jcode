from __future__ import annotations

import json
import os
from pathlib import Path

from jcode_harness_client.evidence import (
    collect_evidence,
    format_transcript,
    paginate,
    retain_tree,
)
from jcode_harness_client.redaction import Redactor


def message(message_id: str, role: str, text: str, **extra: object) -> dict:
    return {
        "id": message_id,
        "role": role,
        "content": [{"type": "text", "text": text}],
        **extra,
    }


def write_session(
    root: Path,
    session_id: str,
    messages: list[dict],
    **extra: object,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{session_id}.json"
    path.write_text(
        json.dumps({"session_id": session_id, "messages": messages, **extra}),
        encoding="utf-8",
    )
    return path


def test_journal_replacement_multiple_workers_and_stable_citations(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "raw" / "sessions"
    parent = write_session(
        sessions,
        "session_parent",
        [message("user-1", "user", "task")],
        future_field={"retained": True},
    )
    parent.with_name("session_parent.journal.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"append_messages": [message("old", "assistant", "old")]}),
                json.dumps(
                    {
                        "replace_messages": [
                            message("user-1", "user", "task"),
                            message("assistant-1", "assistant", "complete"),
                        ],
                        "future_journal_field": 42,
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    for worker in ("arbitrary-worker", "renamed-later"):
        write_session(
            sessions,
            f"session_{worker}",
            [message(f"{worker}-user", "user", "assignment")],
            parent_session_id="session_parent",
            agent_label="native-label-not-a-role",
            worker_blueprint_policy={"name": worker, "future_policy": True},
            swarm_status={"state": "completed"},
            edit_stats={"files": 1},
        )
    bundle = collect_evidence(
        raw_dir=tmp_path / "raw",
        coordinator_session_id="session_parent",
        outer_events=[
            {
                "v": 1,
                "ev": "tool_done",
                "session_id": "session_parent",
                "call_id": "call-native",
                "output": "complete",
            }
        ],
        session_listing=[
            {"session_id": "session_parent"},
            {"session_id": "session_arbitrary-worker"},
            {"session_id": "session_renamed-later"},
        ],
        tool_snapshots=[
            {
                "session_id": "session_parent",
                "phase": "initial",
                "tools": [{"name": "custom", "future": True}],
            }
        ],
    )
    assert bundle.complete, bundle.gaps
    citations = bundle.citations
    assert "session:session_parent:message:assistant-1" in citations
    assert "session:session_parent:journal:2" in citations
    assert not any(record["id"].endswith(":message:old") for record in bundle.records)
    assert "outer:session_parent:tool_done:call-native" in citations
    workers = {session["worker"] for session in bundle.sessions}
    assert workers == {"coordinator", "arbitrary-worker", "renamed-later"}
    replacement = next(
        record
        for record in bundle.records
        if record["id"] == "session:session_parent:journal:2"
    )
    assert replacement["data"]["future_journal_field"] == 42
    snapshot = next(
        record
        for record in bundle.records
        if record["id"] == "session:session_parent:snapshot"
    )
    assert snapshot["data"]["future_field"] == {"retained": True}


def test_native_journal_vectors_and_stale_prefix_replay(tmp_path: Path) -> None:
    sessions = tmp_path / "raw" / "sessions"
    snapshot = write_session(
        sessions,
        "session_parent",
        [message("assistant-1", "assistant", "already checkpointed")],
    )
    tool = {"name": "new-tool", "input_schema": {"type": "object"}}
    snapshot.with_name("session_parent.journal.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "append_messages": [
                            message("assistant-1", "assistant", "stale duplicate")
                        ],
                        "append_request_prompt_snapshots": ["stale prompt"],
                        "append_request_tool_snapshots": [[{"name": "stale"}]],
                    }
                ),
                json.dumps(
                    {
                        "append_messages": [
                            message(
                                "assistant-2",
                                "assistant",
                                "new response",
                                request_snapshot={"prompt": 0, "tools": 0},
                            )
                        ],
                        "append_request_prompt_snapshots": ["actual system prompt"],
                        "append_request_tool_snapshots": [[tool]],
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    bundle = collect_evidence(
        raw_dir=tmp_path / "raw",
        coordinator_session_id="session_parent",
        outer_events=[],
        session_listing=[{"session_id": "session_parent"}],
    )
    assert bundle.complete, bundle.gaps
    journal = [record for record in bundle.records if record["kind"] == "journal-entry"]
    assert journal[0]["replay_status"] == "stale-prefix"
    assert journal[1]["replay_status"] == "applied"
    prompts = [
        record for record in bundle.records if record["kind"] == "request-prompt"
    ]
    tools = [record for record in bundle.records if record["kind"] == "request-tools"]
    assert len(prompts) == 1
    assert prompts[0]["data"]["prompt"] == "actual system prompt"
    assert len(tools) == 1
    assert tools[0]["data"]["tools"] == [tool]
    final = next(
        record
        for record in bundle.records
        if record["id"] == "session:session_parent:message:assistant-2"
    )
    assert final["data"]["request_snapshot"] == {"prompt": 0, "tools": 0}


def test_missing_and_unreadable_sessions_are_explicit_gaps(tmp_path: Path) -> None:
    sessions = tmp_path / "raw" / "sessions"
    write_session(sessions, "session_parent", [])
    (sessions / "session_broken.json").write_text("{", encoding="utf-8")
    bundle = collect_evidence(
        raw_dir=tmp_path / "raw",
        coordinator_session_id="session_parent",
        outer_events=[],
        session_listing=[{"session_id": "session_missing"}],
    )
    assert not bundle.complete
    assert any("no retained snapshot" in gap for gap in bundle.gaps)
    assert any("unreadable native session" in gap for gap in bundle.gaps)
    assert any(record["kind"] == "evidence-gap" for record in bundle.records)


def test_repeated_stream_deltas_share_a_stable_grouped_citation(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "raw" / "sessions"
    write_session(sessions, "session_parent", [])
    events = [
        {
            "v": 1,
            "ev": "text_delta",
            "session_id": "session_parent",
            "message_id": "stream-1",
            "text": "same",
        }
        for _ in range(2)
    ]
    bundle = collect_evidence(
        raw_dir=tmp_path / "raw",
        coordinator_session_id="session_parent",
        outer_events=events,
        session_listing=[{"session_id": "session_parent"}],
    )
    assert bundle.complete, bundle.gaps
    outer = [record for record in bundle.records if record["kind"] == "outer-event"]
    assert len(outer) == 1
    assert outer[0]["data"]["events"] == events


def test_large_records_are_paged_completely_without_truncation(tmp_path: Path) -> None:
    records = [
        {"id": f"record-{index}", "data": {"text": chr(65 + index) * 2000}}
        for index in range(5)
    ]
    pages = paginate(records, 2500)
    assert [record["id"] for page in pages for record in page.records] == [
        record["id"] for record in records
    ]
    assert len(pages) > 1
    single = paginate([records[0]], 10)[0]
    assert single.oversized_single_record
    assert single.records[0]["data"]["text"] == "A" * 2000
    transcript = format_transcript(
        [
            {
                "id": "session:s:message:m",
                "kind": "message",
                "session_id": "s",
                "worker": "coordinator",
                "data": {
                    "id": "m",
                    "role": "tool",
                    "content": [
                        {
                            "type": "tool_result",
                            "content": "X" * 5000,
                            "is_error": False,
                        }
                    ],
                },
            }
        ]
    )
    assert "X" * 5000 in transcript
    assert "truncated" not in transcript


def test_retention_redacts_every_copy_and_rejects_symlinks(tmp_path: Path) -> None:
    home = tmp_path / "home"
    write_session(
        home / "sessions",
        "session_parent",
        [message("m", "user", "secret-token")],
    )
    (home / "daemon.log").write_text("secret-token\n", encoding="utf-8")
    (home / "config.toml").write_text("secret-token", encoding="utf-8")
    (home / "escape").symlink_to(home / "daemon.log")
    gaps, copied = retain_tree(
        home,
        tmp_path / "raw",
        Redactor(("secret-token",)),
        skip_private=True,
    )
    assert any("symlinked" in gap for gap in gaps)
    assert any(item.get("skipped") == "private configuration" for item in copied)
    for path in (tmp_path / "raw").rglob("*"):
        if path.is_file():
            assert b"secret-token" not in path.read_bytes()
            if os.name == "posix":
                assert path.stat().st_mode & 0o777 == 0o600
