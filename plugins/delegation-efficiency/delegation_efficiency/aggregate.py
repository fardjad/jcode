"""Privacy-safe summaries and non-identifying monthly aggregates."""
from __future__ import annotations
import calendar
from datetime import datetime, timezone
from typing import Any
from .schema import connect


def month_for(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m")


def _rollup_in_transaction(db, before_ms: int | None = None) -> int:
    params: tuple[Any, ...] = (before_ms,) if before_ms is not None else ()
    where = "AND e.occurred_at_ms < ?" if before_ms is not None else ""
    rows = db.execute(f"""SELECT e.event_id,
      substr(datetime(e.occurred_at_ms / 1000, 'unixepoch'), 1, 7) month,
      e.event_kind, '' provider, '' model, '' guard_outcome, e.evidence_level,
      e.provider_metric_micros
      FROM events e LEFT JOIN aggregate_event_keys k ON k.event_id = e.event_id
      WHERE k.event_id IS NULL {where}""", params).fetchall()
    grouped: dict[tuple[str, ...], list[int | None]] = {}
    for row in rows:
        key = tuple(row[1:7])
        counters = grouped.setdefault(key, [0, 0, 0, 0])
        counters[0] += 1
        if row[7] is None:
            counters[3] += 1
        else:
            counters[1] += row[7]
            counters[2] += 1
    for key, counters in grouped.items():
        db.execute("""INSERT INTO monthly_aggregates(month,event_kind,provider,model,guard_outcome,evidence_class,event_count,known_metric_micros,known_metric_count,unknown_metric_count)
          VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(month,event_kind,provider,model,guard_outcome,evidence_class) DO UPDATE SET event_count=monthly_aggregates.event_count+excluded.event_count,known_metric_micros=COALESCE(monthly_aggregates.known_metric_micros, 0)+COALESCE(excluded.known_metric_micros, 0),known_metric_count=monthly_aggregates.known_metric_count+excluded.known_metric_count,unknown_metric_count=monthly_aggregates.unknown_metric_count+excluded.unknown_metric_count""", (*key, *counters))
    db.executemany("INSERT INTO aggregate_event_keys(event_id, rolled_up_at) VALUES(?, datetime('now'))", ((row[0],) for row in rows))
    return db.execute("SELECT count(*) FROM monthly_aggregates").fetchone()[0]


def rollup(db=None, before_ms: int | None = None) -> int:
    own = db is None
    db = db or connect()
    db.execute("BEGIN IMMEDIATE")
    try:
        result = _rollup_in_transaction(db, before_ms)
        db.execute("COMMIT")
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    if own: db.close()
    return result


def rollup_and_delete(db, cutoff_ms: int) -> tuple[int, int]:
    """Durably roll old detail into aggregates before deleting that detail."""
    db.execute("BEGIN IMMEDIATE")
    try:
        months = _rollup_in_transaction(db, cutoff_ms)
        deleted = db.execute("DELETE FROM events WHERE occurred_at_ms < ?", (cutoff_ms,)).rowcount
        db.execute("COMMIT")
        return months, deleted
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise


def report(local_detail: bool = False) -> dict[str, Any]:
    db = connect()
    try:
        result: dict[str, Any] = {"privacy": "content_free", "scope": "local_detail" if local_detail else "aggregate", "provider_metrics": {"label": "provider_reported_or_unknown", "estimated": "separate_from_source"}}
        if local_detail:
            result["events"] = [dict(row) | {"source_json": None} for row in db.execute("SELECT event_id,event_kind,occurred_at_ms,session_id,privacy_result,provider_metric_micros,provider_metric_currency,provider_metric_source,provider_metric_status FROM events ORDER BY occurred_at_ms").fetchall()]
        else:
            result["monthly"] = [dict(row) for row in db.execute("SELECT month,event_kind,event_count,known_metric_micros,known_metric_count,unknown_metric_count,evidence_class FROM monthly_aggregates ORDER BY month,event_kind").fetchall()]
        return result
    finally: db.close()
