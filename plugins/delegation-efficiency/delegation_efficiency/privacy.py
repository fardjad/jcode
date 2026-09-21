"""Retention, deletion barriers, and detail/aggregate purge operations."""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from .aggregate import rollup, rollup_and_delete
from .schema import connect

DEFAULT_DETAIL_DAYS = 90
MAX_GENERATION = 2**63 - 1

def cutoff_ms(days: int = DEFAULT_DETAIL_DAYS) -> int:
    return int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

def retain(days: int = DEFAULT_DETAIL_DAYS) -> dict[str, int]:
    db = connect(); cutoff = cutoff_ms(days)
    try:
        derived, deleted = rollup_and_delete(db, cutoff)
        return {"aggregated_months": derived, "deleted_events": deleted}
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally: db.close()

def purge_session(session_id: str, generation: int) -> dict[str, int]:
    if not isinstance(generation, int) or isinstance(generation, bool) or not 0 <= generation <= MAX_GENERATION:
        raise ValueError("generation must be a non-negative integer")
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT INTO deletion_barriers(session_id,generation,deleted_at) VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET generation=excluded.generation,deleted_at=excluded.deleted_at WHERE excluded.generation > deletion_barriers.generation", (session_id, generation, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        deleted = db.execute("DELETE FROM events WHERE session_id = ?", (session_id,)).rowcount
        db.execute("COMMIT")
        return {"deleted_events": deleted}
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally: db.close()

def purge_detail() -> dict[str, int]:
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        deleted = db.execute("DELETE FROM events").rowcount
        db.execute("COMMIT")
        return {"deleted_events": deleted}
    except Exception:
        db.execute("ROLLBACK"); raise
    finally: db.close()

def purge_aggregates() -> dict[str, int]:
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        deleted = db.execute("DELETE FROM monthly_aggregates").rowcount
        db.execute("DELETE FROM aggregate_event_keys")
        db.execute("COMMIT")
        return {"deleted_aggregates": deleted}
    except Exception:
        db.execute("ROLLBACK"); raise
    finally: db.close()

def status() -> dict[str, int | str]:
    db = connect()
    try:
        return {"detail_days": DEFAULT_DETAIL_DAYS, "events": db.execute("SELECT count(*) FROM events").fetchone()[0], "aggregates": db.execute("SELECT count(*) FROM monthly_aggregates").fetchone()[0], "wal": "enabled"}
    finally: db.close()
