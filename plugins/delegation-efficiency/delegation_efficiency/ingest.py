"""Short-lived, idempotent SQLite event ingestion."""
from __future__ import annotations
import json
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any
from .contract import EnvelopeError, SOURCE_FIELDS, parse_event
from .schema import connect
from .analysis import derive_event


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _i(value: Any) -> int | None:
    return int(value) if value is not None else None


def _barrier_allows(db: sqlite3.Connection, event: dict[str, Any]) -> bool:
    row = db.execute("SELECT generation FROM deletion_barriers WHERE session_id = ?", (event["session_id"],)).fetchone()
    if not row:
        return True
    generation = event.get("deletion_generation", 0)
    return isinstance(generation, int) and generation > row["generation"]


def _insert(db: sqlite3.Connection, event: dict[str, Any]) -> str:
    db.execute("BEGIN IMMEDIATE")
    try:
        if not _barrier_allows(db, event):
            db.execute("ROLLBACK")
            return "rejected_deleted_generation"
        event_id = event["event_id"]
        existing = db.execute("SELECT event_id FROM events WHERE event_id = ?", (event_id,)).fetchone()
        if existing:
            db.execute("ROLLBACK")
            return "duplicate"
        now = utc_now()
        source_event = {key: event[key] for key in event if key in SOURCE_FIELDS}
        source = json.dumps(source_event, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
        db.execute("""INSERT INTO events(event_key,event_id,envelope,schema_version,semantics_version,event_kind,occurred_at_ms,ingested_at,privacy_result,evidence_level,session_id,source_json,cost_micros,cost_currency,cost_source,cost_status)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (event_id, event_id, event["envelope"], event["schema_version"], event["semantics_version"], event["event"], event["occurred_at_unix_ms"], now, "accepted_content_free", event.get("evidence_level", "unknown"), event["session_id"], source, event.get("cost_micros"), event.get("cost_currency"), event.get("cost_source"), event.get("cost_status")))
        kind = event["event"]
        if kind in {"tool_attempt", "tool_result"}:
            db.execute("""INSERT INTO guard_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (event_id, event.get("tool_invocation_id"), event.get("provider_tool_call_id"), event.get("guard_event_id"), event.get("session_id"), event.get("tool_name"), event.get("guard_outcome"), event.get("failure_reason"), event.get("lifecycle"), event.get("threshold_classification"), event.get("transformer_classification"), event.get("original_bytes"), event.get("original_lines"), event.get("original_tokens"), event.get("final_visible_bytes"), event.get("final_visible_lines"), event.get("final_visible_tokens"), event.get("tokenizer_identity"), event.get("tokenizer_status"), event.get("evidence_level"), event["occurred_at_unix_ms"]))
        elif kind in {"delegation_spawn", "delegation_follow_up", "delegation_summary", "child_session"}:
            db.execute("""INSERT INTO delegation_observations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (event_id, event.get("delegation_id"), event.get("guard_event_id"), event.get("tool_invocation_id"), event.get("parent_session_id"), event.get("child_session_id"), event.get("session_id"), event.get("blueprint_id"), event.get("spawn_mode"), kind, event["occurred_at_unix_ms"], event.get("status"), event.get("failure_reason"), event.get("outcome"), event.get("evidence_level"), event.get("follow_up_count")))
        elif kind == "provider_usage":
            db.execute("""INSERT INTO provider_usage(event_id,guard_event_id,delegation_id,request_id,generation_id,attempt,process_role,provider,route,model,cache_read_input_tokens,cache_creation_input_tokens,provider_input_tokens,provider_output_tokens,retryable,retry_count,provider_response_id,served_model,cost_micros,cost_currency,cost_source,cost_status,cost_version,cost_timestamp_ms,attribution_status,intermediate,final,evidence_level)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (event_id, event.get("guard_event_id"), event.get("delegation_id"), event.get("request_id"), event.get("generation_id"), event.get("attempt"), event.get("process_role"), event.get("provider"), event.get("route"), event.get("model"), event.get("cache_read_input_tokens"), event.get("cache_creation_input_tokens"), event.get("provider_input_tokens"), event.get("provider_output_tokens"), event.get("retryable"), event.get("retry_count"), event.get("provider_response_id"), event.get("served_model"), event.get("cost_micros"), event.get("cost_currency"), event.get("cost_source"), event.get("cost_status"), event.get("cost_version"), event.get("cost_timestamp_unix_ms"), event.get("attribution_status"), event.get("intermediate"), event.get("final"), event.get("evidence_level")))
        elif kind == "communication_observation":
            db.execute("""INSERT INTO communication_observations(event_id,guard_event_id,delegation_id,session_id,process_role,direction,communication_kind,bytes,tokens,tokenizer_status,occurred_at_ms,cost_micros,cost_currency,cost_source,cost_status)
              VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (event_id, event.get("guard_event_id"), event.get("delegation_id"), event.get("session_id"), event.get("process_role"), event["direction"], event["communication_kind"], event["bytes"], event["tokens"], event["tokenizer_status"], event["occurred_at_unix_ms"], event.get("cost_micros"), event.get("cost_currency"), event.get("cost_source"), event.get("cost_status")))
        derive_event(db, event_id)
        db.execute("COMMIT")
    except Exception:
        db.execute("ROLLBACK")
        raise
    return "inserted"


def ingest(event: dict[str, Any] | str | bytes, retries: int = 6) -> str:
    validated = parse_event(event) if isinstance(event, (str, bytes)) else parse_event(json.dumps(event))
    last: Exception | None = None
    for attempt in range(retries):
        db = None
        try:
            # Schema setup and WAL negotiation in `connect` can themselves
            # briefly contend with another short-lived writer. Keep them in
            # the same retry envelope as BEGIN IMMEDIATE.
            db = connect()
            return _insert(db, validated)
        except sqlite3.OperationalError as exc:
            last = exc
            if "locked" not in str(exc).lower() or attempt + 1 == retries:
                raise
            time.sleep(0.05 * 2**attempt)
        finally:
            if db is not None:
                db.close()
    raise last or RuntimeError("ingestion failed")
