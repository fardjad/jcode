from __future__ import annotations

import json
import os
from pathlib import Path

from jcode_harness_client.evidence import (
    collect_evidence,
    write_cited_evidence,
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
    assert not any(record["id"].endswith(":message:old") for record in bundle.records)
    assert "outer:session_parent:tool_done:call-native" in citations
    workers = {session["worker"] for session in bundle.sessions}
    assert workers == {"coordinator", "arbitrary-worker", "renamed-later"}


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
    tools = [record for record in bundle.records if record["kind"] == "request-tools"]
    assert len(tools) == 1
    assert tools[0]["data"]["tools"] == [tool]
    final = next(
        record
        for record in bundle.records
        if record["id"] == "session:session_parent:message:assistant-2"
    )
    assert final["data"]["request_snapshot"] == {"prompt": 0, "tools": 0}


def test_three_level_ancestry_indexed_with_transcript_parent_fallback(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "raw" / "sessions"
    write_session(sessions, "session_root", [message("r-1", "user", "objective")])
    advanced = write_session(
        sessions,
        "session_advanced",
        [],
        worker_blueprint_policy={"name": "advanced-worker"},
    )
    # The advanced session has no snapshot parent and no snapshot messages:
    # only the journal carries the actual transcript parent and its one turn.
    advanced.with_name("session_advanced.journal.jsonl").write_text(
        json.dumps(
            {
                "meta": {"parent_id": "session_root"},
                "append_messages": [
                    message("a-1", "assistant", "spawn a routine child")
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    write_session(
        sessions,
        "session_routine",
        [message("c-1", "assistant", "bounded extraction report")],
        parent_id="session_advanced",
        worker_blueprint_policy={"name": "routine-worker"},
    )
    bundle = collect_evidence(
        raw_dir=tmp_path / "raw",
        coordinator_session_id="session_root",
        outer_events=[],
        session_listing=[
            {"session_id": "session_root"},
            {"session_id": "session_advanced"},
            {
                "session_id": "session_routine",
                "parent_session_id": "session_advanced",
            },
        ],
    )
    assert bundle.complete, bundle.gaps
    assert len(bundle.sessions) == 3
    assert len([r for r in bundle.records if r["kind"] == "session-snapshot"]) == 3
    by_id = {session["session_id"]: session for session in bundle.sessions}
    assert by_id["session_root"]["parent_session_id"] is None
    assert by_id["session_advanced"]["parent_session_id"] == "session_root"
    assert by_id["session_advanced"]["parent_id"] == "session_root"
    assert by_id["session_routine"]["parent_session_id"] == "session_advanced"
    assert by_id["session_routine"]["parent_id"] == "session_advanced"
    assert {session["worker"] for session in bundle.sessions} == {
        "coordinator",
        "advanced-worker",
        "routine-worker",
    }
    advanced_message = next(
        record
        for record in bundle.records
        if record["id"] == "session:session_advanced:message:a-1"
    )
    assert advanced_message["parent_session_id"] == "session_root"
    assert advanced_message["parent_id"] == "session_root"
    routine_message = next(
        record
        for record in bundle.records
        if record["id"] == "session:session_routine:message:c-1"
    )
    assert routine_message["parent_session_id"] == "session_advanced"
    assert routine_message["parent_id"] == "session_advanced"
    assert (
        len(
            [
                record
                for record in bundle.records
                if record["kind"] == "message"
                and record["session_id"] == "session_advanced"
            ]
        )
        == 1
    )


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


def test_cited_only_retention_keeps_redacted_journals(tmp_path: Path) -> None:
    sessions = tmp_path / "ephemeral" / "sessions"
    snapshot = write_session(sessions, "session_parent", [
        message("cited", "assistant", "secret-token"),
        message("other", "assistant", "do not retain"),
    ])
    journal = snapshot.with_name("session_parent.journal.jsonl")
    journal.write_text(json.dumps({"value": 'secret"token'}) + "\n")
    (sessions / "session_escape.journal.jsonl").symlink_to(journal)
    bundle = collect_evidence(
        raw_dir=tmp_path / "ephemeral", coordinator_session_id="session_parent",
        outer_events=[], session_listing=[],
    )
    output = tmp_path / "result"
    citation = "session:session_parent:message:cited"
    gaps = write_cited_evidence(
        output, bundle.citation_map, [{"citations": [citation, "missing"]}],
        sessions, Redactor(("secret-token", 'secret"token')),
    )
    assert "missing" in gaps
    assert any("unsafe native journal" in gap for gap in gaps)
    assert sorted(
        p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()
    ) == ["raw/cited.jsonl", "raw/sessions/session_parent.journal.jsonl"]
    cited = (output / "raw/cited.jsonl").read_text()
    assert citation in cited and "[REDACTED]" in cited and "do not retain" not in cited
    retained_journal = output / "raw/sessions/session_parent.journal.jsonl"
    assert json.loads(retained_journal.read_text())["value"] == "[REDACTED]"
    for path in output.rglob("*"):
        if path.is_file():
            assert b"secret-token" not in path.read_bytes()
            if os.name == "posix":
                assert path.stat().st_mode & 0o777 == 0o600
