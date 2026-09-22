"""Evidence-preserving Part 3 analysis built from immutable source facts.

This module deliberately keeps derived values in a separate table.  It never
updates ``events`` or the normalized source observations and never joins on
time, model, ordering, or other heuristics.
"""
from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

from .schema import DERIVATION_VERSION, connect

NET_SAVINGS_VERSION = "counterfactual-net-savings-2"


def _token_measurement(row, field: str) -> tuple[int | None, str]:
    value = row[field] if row is not None else None
    status = row["tokenizer_status"] if row is not None else None
    if value is None:
        return None, "unknown"
    return value, "estimated" if status == "approximate" else "measured" if status == "provider_reported" else "unknown"


def counterfactual_net_savings(db=None, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    """Estimate linked guard net savings without heuristic joins.

    The token baseline is the guarded full output as an estimated coordinator
    input. Provider output usage is deliberately not used as a proxy for that
    later coordinator input. Monetary output is unavailable because the
    catalog has no authoritative input-token price/rate or exact
    communication-to-provider-request attribution.
    """
    own = db is None
    db = db or connect()
    try:
        rebuild_analysis(db)
        rows = _rows(db, filters)
        guards = {
            row["guard_event_id"]: row
            for row in db.execute("SELECT * FROM guard_observations WHERE guard_event_id IS NOT NULL").fetchall()
        }
        result_rows = []
        exclusions = Counter()
        for row in rows:
            # Runtime records the initial worker handoff as `child_session`.
            # `delegation_follow_up` is a later lifecycle observation for the
            # same delegation, so including it would double-count costs.
            if row["event_kind"] not in ("delegation_spawn", "child_session") or row["explicit_delegation_link"] != 1:
                continue
            delegation = db.execute(
                "SELECT * FROM delegation_observations WHERE event_id = ?", (row["event_id"],)
            ).fetchone()
            guard = guards.get(delegation["guard_event_id"] if delegation else None)
            if guard is None or guard["guard_outcome"] != "intercepted":
                exclusions["missing_or_unintercepted_guard"] += 1
                continue
            communications = db.execute(
                """SELECT * FROM communication_observations
                   WHERE delegation_id = ? AND guard_event_id = ?
                   ORDER BY event_id""",
                (delegation["delegation_id"], delegation["guard_event_id"]),
            ).fetchall()
            guard_notice = [item for item in communications if item["direction"] == "inbound" and item["communication_kind"] == "clarification" and item["process_role"] == "coordinator"]
            spawn = [item for item in communications if item["direction"] == "outbound" and item["communication_kind"] in ("follow_up", "spawn", "summary", "escalation") and item["process_role"] == "coordinator"]
            returns = [item for item in communications if item["direction"] == "inbound" and item["communication_kind"] in ("follow_up", "summary", "context_read") and item["process_role"] == "worker"]
            baseline_tokens, baseline_evidence = _token_measurement(guard, "original_tokens")
            notice_tokens = sum(item["tokens"] for item in guard_notice if item["tokens"] is not None)
            spawn_tokens = sum(item["tokens"] for item in spawn if item["tokens"] is not None)
            return_tokens = sum(item["tokens"] for item in returns if item["tokens"] is not None)
            delegation_overhead_tokens = notice_tokens + spawn_tokens
            actual_tokens = delegation_overhead_tokens + return_tokens
            component_rows = [*guard_notice, *spawn, *returns]
            token_complete = all((guard_notice, spawn, returns)) and all(
                item["tokens"] is not None for item in component_rows
            )
            token_evidence = "unknown"
            if token_complete:
                token_evidence = "estimated" if baseline_evidence == "estimated" or any(item["tokenizer_status"] == "approximate" for item in component_rows) else "measured"
            # The worker result is task-equivalent output: without delegation,
            # the coordinator would consume the tool result and produce its
            # own result. It remains visible but is not delegation overhead.
            token_net = baseline_tokens - delegation_overhead_tokens if baseline_tokens is not None and token_complete else None
            baseline_token_evidence = "estimated" if baseline_tokens is not None else "unknown"

            result_rows.append({
                "guard_linkage": "explicit", "baseline": {"tokens": baseline_tokens, "token_evidence": baseline_token_evidence, "token_basis": "estimated_coordinator_input_full_original_tool_output"},
                "actual": {"tokens": actual_tokens if token_complete else None, "delegation_overhead_tokens": delegation_overhead_tokens if token_complete else None, "token_evidence": token_evidence, "components": {"guard_notice_tokens": notice_tokens if token_complete else None, "delegation_request_tokens": spawn_tokens if token_complete else None, "worker_result_tokens": return_tokens if token_complete else None}},
                "net": {"tokens": token_net, "token_evidence": "estimated_coordinator_input_counterfactual" if token_net is not None else "unknown"},
                "component_counts": {"guard_notice": len(guard_notice), "spawn": len(spawn), "worker_returns_or_reads": len(returns)},
                "completeness": {"token_components_complete": token_complete},
            })
        complete_rows = [item for item in result_rows if item["net"]["tokens"] is not None]
        comparison = {
            "complete_delegations": len(complete_rows),
            "estimated_without_guard_tokens": sum(item["baseline"]["tokens"] for item in complete_rows),
            "guard_notice_tokens": sum(item["actual"]["components"]["guard_notice_tokens"] for item in complete_rows),
            "delegation_request_tokens": sum(item["actual"]["components"]["delegation_request_tokens"] for item in complete_rows),
            "worker_result_tokens": sum(item["actual"]["components"]["worker_result_tokens"] for item in complete_rows),
            "delegation_overhead_tokens": sum(item["actual"]["delegation_overhead_tokens"] for item in complete_rows),
            "estimated_context_savings_tokens": sum(item["net"]["tokens"] for item in complete_rows),
        }
        return {"privacy": "content_free", "scope": "counterfactual_net_savings", "derivation_version": NET_SAVINGS_VERSION, "evidence": "estimated_counterfactual", "rows": result_rows, "exclusions": dict(sorted(exclusions.items())), "summary": {"linked_intercepted_delegations": len(result_rows), "token_rows": len(complete_rows), "comparison": comparison}}
    finally:
        if own:
            db.close()


def _percentile(values: Iterable[int], percentile: float) -> int | None:
    ordered = sorted(int(value) for value in values)
    if not ordered:
        return None
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _flag(value: bool | None) -> int | None:
    return None if value is None else int(value)


def _explicit_guard(db, guard_event_id: str | None):
    """Resolve only an explicitly referenced guard observation."""
    if not guard_event_id:
        return None
    return db.execute(
        """SELECT g.* FROM guard_observations g
           JOIN events e ON e.event_id = g.event_id
           WHERE g.guard_event_id = ? AND e.event_kind IN ('tool_attempt', 'tool_result')""",
        (guard_event_id,),
    ).fetchone()


def _explicit_delegation(db, delegation_id: str | None):
    """Resolve only an explicitly referenced delegation point event."""
    if not delegation_id:
        return None
    return db.execute(
        "SELECT * FROM delegation_observations WHERE delegation_id = ?",
        (delegation_id,),
    ).fetchone()


def _delta(before: int | None, after: int | None, evidence: str):
    if before is None or after is None:
        return None, "unknown"
    return before - after, evidence


def _derive_row(db, event_id: str) -> tuple[Any, ...]:
    event = db.execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
    if event is None:
        raise KeyError(event_id)
    source = json.loads(event["source_json"])
    kind = event["event_kind"]
    guard = db.execute("SELECT * FROM guard_observations WHERE event_id = ?", (event_id,)).fetchone()
    delegation = db.execute("SELECT * FROM delegation_observations WHERE event_id = ?", (event_id,)).fetchone()
    provider_usage = db.execute("SELECT * FROM provider_usage WHERE event_id = ?", (event_id,)).fetchone()
    communication = db.execute("SELECT * FROM communication_observations WHERE event_id = ?", (event_id,)).fetchone()

    original_bytes = guard["original_bytes"] if guard else None
    original_lines = guard["original_lines"] if guard else None
    original_tokens = guard["original_tokens"] if guard else None
    final_bytes = guard["final_visible_bytes"] if guard else None
    final_lines = guard["final_visible_lines"] if guard else None
    final_tokens = guard["final_visible_tokens"] if guard else None
    tokenizer_status = guard["tokenizer_status"] if guard else source.get("tokenizer_status")
    compatible_tokens = original_tokens is not None and final_tokens is not None and tokenizer_status not in (None, "unavailable")
    byte_delta, byte_evidence = _delta(original_bytes, final_bytes, "measured")
    line_delta, line_evidence = _delta(original_lines, final_lines, "measured")
    token_evidence = (
        "estimated" if tokenizer_status == "approximate" else "measured"
    ) if compatible_tokens else "unknown"
    token_delta, token_evidence = _delta(original_tokens, final_tokens, token_evidence)
    payload_evidence = (
        "estimated" if token_evidence == "estimated"
        else "measured" if byte_evidence == "measured" or line_evidence == "measured"
        else "unknown"
    )
    eligible = kind == "tool_result" and original_bytes is not None
    measured_transformer_reduction = source.get("transformer_classification") == "applied" and any(
        before is not None and after is not None and after < before
        for before, after in (
            (original_bytes, final_bytes),
            (original_lines, final_lines),
            (original_tokens, final_tokens),
        )
    )
    intercepted = eligible and (
        source.get("guard_outcome") == "intercepted"
        or (
            source.get("guard_outcome") == "below_threshold"
            and measured_transformer_reduction
        )
    )
    explicit_delegation = None
    linkage = "not_applicable"
    if delegation:
        guard_id = delegation["guard_event_id"]
        if not guard_id:
            linkage = "proactive"
            explicit_delegation = False
        elif _explicit_guard(db, guard_id):
            linkage = "explicit"
            explicit_delegation = True
        else:
            linkage = "unknown_linkage"
    elif provider_usage:
        ids_present = all(provider_usage[field] is not None for field in ("request_id", "generation_id", "attempt"))
        explicit_target = (
            _explicit_guard(db, provider_usage["guard_event_id"])
            or _explicit_delegation(db, provider_usage["delegation_id"])
        )
        linkage = (
            "explicit_identities"
            if ids_present and provider_usage["attribution_status"] == "eligible" and explicit_target
            else "unknown_linkage"
        )
    elif guard:
        linkage = "guard_observation"

    if provider_usage:
        cost_coverage = "known_provider_reported" if provider_usage["cost_micros"] is not None and provider_usage["cost_source"] in ("provider_response", "provider_reported") else "unknown_provider_cost"
    else:
        cost_coverage = "not_expected"
    month = str(event["occurred_at_ms"])
    from datetime import datetime, timezone
    month = datetime.fromtimestamp(event["occurred_at_ms"] / 1000, timezone.utc).strftime("%Y-%m")
    return (
        event_id, DERIVATION_VERSION, month, kind, event["evidence_level"], tokenizer_status,
        _flag(eligible), _flag(intercepted), _flag(explicit_delegation), linkage,
        byte_delta, line_delta, token_delta, payload_evidence,
        byte_evidence, line_evidence, token_evidence,
        source.get("denominator_value") if source.get("denominator_name") == "threshold_bytes" else None,
        guard["guard_outcome"] if guard else None, source.get("outcome"), source.get("tool_latency_ms"),
        delegation["follow_up_count"] if delegation else None,
        communication["bytes"] if communication else None, communication["tokens"] if communication else None,
        provider_usage["provider"] if provider_usage else source.get("provider"),
        provider_usage["model"] if provider_usage else source.get("model"),
        provider_usage["process_role"] if provider_usage else source.get("process_role"),
        source.get("blueprint_name"), cost_coverage, event["cost_micros"],
        provider_usage["provider_input_tokens"] if provider_usage else None,
        provider_usage["provider_output_tokens"] if provider_usage else None,
        provider_usage["cache_read_input_tokens"] if provider_usage else None,
        provider_usage["cache_creation_input_tokens"] if provider_usage else None,
        provider_usage["retry_count"] if provider_usage else source.get("retry_count"),
        event["cost_currency"], original_bytes, original_lines, original_tokens,
        final_bytes, final_lines, final_tokens,
    )


ANALYSIS_COLUMNS = """event_id,derivation_version,month,event_kind,evidence_class,tokenizer_status,
eligible_guard,intercepted,explicit_delegation_link,linkage_class,
immediate_avoided_bytes,immediate_avoided_lines,immediate_avoided_tokens,
payload_evidence,immediate_avoided_bytes_evidence,immediate_avoided_lines_evidence,
immediate_avoided_tokens_evidence,threshold_value,guard_outcome,outcome,latency_ms,follow_up_count,
communication_bytes,communication_tokens,provider,model,process_role,blueprint_name,
cost_coverage_class,cost_micros,provider_input_tokens,provider_output_tokens,
cache_read_input_tokens,cache_creation_input_tokens,retry_count,cost_currency,
original_bytes,original_lines,original_tokens,final_visible_bytes,final_visible_lines,
final_visible_tokens""".replace("\n", "")


def derive_event(db, event_id: str) -> None:
    values = _derive_row(db, event_id)
    placeholders = ",".join("?" for _ in values)
    db.execute(f"INSERT OR REPLACE INTO event_analysis({ANALYSIS_COLUMNS}) VALUES({placeholders})", values)


def rebuild_analysis(db=None) -> int:
    own = db is None
    db = db or connect()
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("DELETE FROM event_analysis")
        ids = [row[0] for row in db.execute("SELECT event_id FROM events ORDER BY occurred_at_ms,event_id")]
        for event_id in ids:
            derive_event(db, event_id)
        db.execute("COMMIT")
        return len(ids)
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        if own:
            db.close()


def _rows(db, filters: dict[str, Any] | None = None):
    query = "SELECT * FROM event_analysis"
    values = []
    if filters:
        clauses = []
        for field in ("month", "provider", "model", "process_role", "blueprint_name", "event_id"):
            if filters.get(field) is not None:
                clauses.append(f"{field} = ?")
                values.append(filters[field])
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY month,event_id"
    return db.execute(query, values).fetchall()


def _rate(numerator: int, denominator: int, excluded: int, unknown: int, evidence: str = "measured") -> dict[str, Any]:
    return {"numerator": numerator, "denominator": denominator, "excluded": excluded,
            "unknown": unknown, "rate": (numerator / denominator if denominator else None),
            "evidence": evidence}


def analysis_report(filters: dict[str, Any] | None = None) -> dict[str, Any]:
    db = connect()
    try:
        rebuild_analysis(db)
        rows = _rows(db, filters)
        eligible = [row for row in rows if row["eligible_guard"] == 1]
        intercepted = [row for row in eligible if row["intercepted"] == 1]
        linked = []
        for row in rows:
            if row["explicit_delegation_link"] != 1 or row["linkage_class"] != "explicit":
                continue
            source = json.loads(db.execute("SELECT source_json FROM events WHERE event_id = ?", (row["event_id"],)).fetchone()[0])
            guard_id = source.get("guard_event_id")
            if guard_id:
                guard_source = db.execute(
                    """SELECT e.source_json FROM guard_observations g
                       JOIN events e ON e.event_id = g.event_id
                       WHERE g.guard_event_id = ?""",
                    (guard_id,),
                ).fetchone()
                if guard_source and json.loads(guard_source[0]).get("guard_outcome") == "intercepted":
                    linked.append(row)
        tasks_known = [row for row in rows if row["outcome"] is not None]
        successes = [row for row in tasks_known if row["outcome"] in ("success", "completed")]
        provider_rows = [row for row in rows if row["cost_coverage_class"] != "not_expected"]
        known_cost = [row for row in provider_rows if row["cost_coverage_class"] == "known_provider_reported"]
        unknown_linkage = sum(row["linkage_class"] == "unknown_linkage" for row in rows)
        failure_rows = [row for row in tasks_known if row["outcome"] in ("failed", "error", "aborted", "cancelled")]
        def values(field): return [row[field] for row in rows if row[field] is not None]
        grouped = defaultdict(lambda: {"event_count": 0, "known_cost_count": 0, "unknown_cost_count": 0})
        for row in rows:
            key = (row["month"], row["event_kind"], row["provider"] or "unknown", row["model"] or "unknown", row["process_role"] or "unknown", row["blueprint_name"] or "unknown", row["evidence_class"], row["tokenizer_status"] or "unknown", row["cost_coverage_class"])
            item = grouped[key]; item["event_count"] += 1
            if row["cost_coverage_class"] == "known_provider_reported": item["known_cost_count"] += 1
            elif row["cost_coverage_class"] == "unknown_provider_cost": item["unknown_cost_count"] += 1
        rollups = []
        for key, item in sorted(grouped.items()):
            rollups.append(dict(zip(("month", "event_kind", "provider", "model", "process_role", "blueprint", "evidence_class", "tokenizer_status", "cost_coverage_class"), key), **item))
        communication = [row for row in rows if row["communication_bytes"] is not None or row["communication_tokens"] is not None]
        cost_sums = defaultdict(int)
        for row in known_cost:
            if row["cost_currency"] is not None and row["cost_micros"] is not None:
                cost_sums[row["cost_currency"]] += row["cost_micros"]

        def impact_measurement(field: str, evidence_field: str) -> dict[str, Any]:
            observed = [row for row in intercepted if row[field] is not None]
            evidence = [row[evidence_field] for row in intercepted if row[field] is not None]
            if not observed:
                label = "unknown"
            elif "estimated" in evidence:
                label = "estimated"
            elif all(value == "measured" for value in evidence):
                label = "measured"
            else:
                label = "unknown"
            opportunity = len(intercepted)
            return {
                "total": sum(row[field] for row in observed) if observed else None,
                "available": len(observed),
                "opportunity": opportunity,
                "coverage": (len(observed) / opportunity if opportunity else None),
                "evidence": label,
            }

        return {
            "privacy": "content_free", "scope": "analysis", "derivation_version": DERIVATION_VERSION,
            "costs": {"source": "provider_reported_event_time", "billing_truth": False, "known_events": len(known_cost), "expected_events": len(provider_rows), "unknown_events": sum(row["cost_coverage_class"] == "unknown_provider_cost" for row in provider_rows), "coverage": (len(known_cost) / len(provider_rows) if provider_rows else None), "known_cost_micros_by_currency": dict(sorted(cost_sums.items())), "currency_policy": "separate_no_fx"},
            "rates": {"interception": _rate(len(intercepted), len(eligible), len(rows) - len(eligible), 0), "delegation": _rate(len(linked), len(intercepted), 0, sum(row["explicit_delegation_link"] is None or row["linkage_class"] == "unknown_linkage" for row in intercepted)), "success": _rate(len(successes), len(tasks_known), len(rows) - len(tasks_known), 0), "known_cost_coverage": _rate(len(known_cost), len(provider_rows), len(rows) - len(provider_rows), sum(row["cost_coverage_class"] == "unknown_provider_cost" for row in provider_rows)), "unknown_linkage_count": unknown_linkage},
            "impact": {
                "eligible_outputs": len(eligible),
                "intercepted_eligible_outputs": len(intercepted),
                "opportunity_evidence": "measured" if eligible else "unknown",
                "avoided": {
                    "bytes": impact_measurement("immediate_avoided_bytes", "immediate_avoided_bytes_evidence"),
                    "lines": impact_measurement("immediate_avoided_lines", "immediate_avoided_lines_evidence"),
                    "tokens": impact_measurement("immediate_avoided_tokens", "immediate_avoided_tokens_evidence"),
                },
            },
            "payload": {
                "original_bytes": {"count": len(values("original_bytes")), "p50": _percentile(values("original_bytes"), .5), "p95": _percentile(values("original_bytes"), .95), "evidence": "measured" if values("original_bytes") else "unknown"},
                "final_visible_bytes": {"count": len(values("final_visible_bytes")), "p50": _percentile(values("final_visible_bytes"), .5), "p95": _percentile(values("final_visible_bytes"), .95), "evidence": "measured" if values("final_visible_bytes") else "unknown"},
                "avoided_bytes": {"count": sum(row["immediate_avoided_bytes"] is not None for row in rows), "evidence": "measured" if any(row["immediate_avoided_bytes_evidence"] == "measured" for row in rows) else "unknown"},
                "avoided_lines": {"count": sum(row["immediate_avoided_lines"] is not None for row in rows), "evidence": "measured" if any(row["immediate_avoided_lines_evidence"] == "measured" for row in rows) else "unknown"},
                "avoided_tokens": {"count": sum(row["immediate_avoided_tokens"] is not None for row in rows), "evidence": "estimated" if any(row["immediate_avoided_tokens_evidence"] == "estimated" for row in rows) else "measured" if any(row["immediate_avoided_tokens_evidence"] == "measured" for row in rows) else "unknown"},
            },
            "latency_ms": {"count": len(values("latency_ms")), "p50": _percentile(values("latency_ms"), .5), "p95": _percentile(values("latency_ms"), .95)},
            "follow_up_count": {"count": len(values("follow_up_count")), "p50": _percentile(values("follow_up_count"), .5), "p95": _percentile(values("follow_up_count"), .95)},
            "communication": {"events": len(communication), "bytes": sum(row["communication_bytes"] or 0 for row in communication), "tokens": sum(row["communication_tokens"] or 0 for row in communication), "evidence": "measured" if communication else "unknown"},
            "outcomes": {"known": len(tasks_known), "success": len(successes), "failure_or_fallback": len(failure_rows), "unknown": len(rows) - len(tasks_known)},
            "populations": {"guard_triggered": sum(row["explicit_delegation_link"] == 1 for row in rows), "proactive": sum(row["linkage_class"] == "proactive" for row in rows), "unknown": sum(row["linkage_class"] == "unknown_linkage" for row in rows)},
            "rollups": rollups,
        }
    finally:
        db.close()


def export_analysis(path: str | Path, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    db = connect()
    try:
        rebuild_analysis(db)
        rows = _rows(db, filters)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Exports are event-level but omit every identifier, timestamp, and raw
        # source field. They are safe aggregate-analysis artifacts, not detail.
        fields = ["month", "event_kind", "evidence_class", "tokenizer_status", "eligible_guard", "intercepted", "explicit_delegation_link", "linkage_class", "immediate_avoided_bytes", "immediate_avoided_bytes_evidence", "immediate_avoided_lines", "immediate_avoided_lines_evidence", "immediate_avoided_tokens", "immediate_avoided_tokens_evidence", "payload_evidence", "guard_outcome", "outcome", "latency_ms", "follow_up_count", "communication_bytes", "communication_tokens", "provider", "model", "process_role", "blueprint_name", "cost_coverage_class", "cost_micros", "provider_input_tokens", "provider_output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "retry_count", "cost_currency", "original_bytes", "final_visible_bytes", "derivation_version"]
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row[field] for field in fields})
        return {"format": "csv", "path": str(target), "rows": len(rows), "scope": "content_free_analysis"}
    finally:
        db.close()


def threshold_analysis(thresholds: Iterable[int], filters: dict[str, Any] | None = None) -> dict[str, Any]:
    values = sorted(set(int(value) for value in thresholds))
    if not values or any(value < 0 for value in values):
        raise ValueError("thresholds must contain non-negative integers")
    db = connect()
    try:
        rebuild_analysis(db)
        rows = _rows(db, filters)
        eligible = [row for row in rows if row["original_bytes"] is not None]
        results = []
        for threshold in values:
            selected = [row for row in eligible if row["original_bytes"] > threshold]
            observed_intercepted = [row for row in selected if row["intercepted"] == 1]
            unknown = sum(row["intercepted"] is None for row in selected)
            results.append({"threshold_bytes": threshold, "eligible": len(selected), "intercepted": len(observed_intercepted), "unknown": unknown, "excluded": len(rows) - len(selected), "interception_rate": len(observed_intercepted) / len(selected) if selected else None, "denominator": len(selected), "sample_size": len(selected), "uncertainty": "unknown_not_estimated", "evidence": "measured_observed_classification_exploratory_threshold", "cost_coverage": "see_report_costs", "automatic_tuning": False})
        return {"privacy": "content_free", "scope": "threshold_analysis", "derivation_version": DERIVATION_VERSION, "thresholds": results, "cost_coverage": analysis_report(filters)["costs"], "note": "Threshold comparison is analysis only; it does not edit configuration or claim billing savings."}
    finally:
        db.close()
