"""Schema-native delegation facts; worker prose never proves completion."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from .tool_health import _blocks, native_calls, select_tool_errors

_JSON = dict[str, Any]


def _result_text(result: _JSON | None) -> str:
    content = (result or {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    return ""


def _timestamp(data: _JSON) -> datetime | None:
    value = data.get("timestamp")
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return stamp if stamp.tzinfo is not None else None
        except ValueError:
            pass
    return None


def build_delegation(
    records: list[_JSON],
    *,
    trace_root: Path,
    evidence_gaps: Sequence[Any] = (),
) -> _JSON:
    """Produce Delegation rows, assignments, reports and correlated errors.

    Validation is native: the matching submit_report result must confirm
    acceptance, with the effective output schema retained. No second, partial
    JSON Schema implementation or front-matter assumption substitutes for it.
    Cross-session chronology uses native timestamps, not collection order
    (the collector groups messages by session).
    """
    sessions: dict[str, _JSON] = {}
    messages: dict[str, _JSON] = {}
    gaps = list(evidence_gaps)
    for record in records:
        sid, data = record.get("session_id"), record.get("data", {})
        if not isinstance(sid, str) or not isinstance(data, dict):
            continue
        policy = data.get("worker_blueprint_policy")
        policy = policy if isinstance(policy, dict) else {}
        row = sessions.setdefault(
            sid,
            {
                "session": sid,
                "tier": "unknown",
                "parent": None,
                "model": "unknown",
                "effort": "unknown",
                "status": "unknown",
                "resumed": False,
                "checks": "unknown",
                "native_refs": [],
            },
        )
        worker = record.get("worker") or policy.get("name")
        if worker and worker != "unknown":
            row["tier"] = worker
        parent = (
            record.get("parent_session_id")
            or data.get("parent_session_id")
            or data.get("parent_id")
        )
        if parent:
            row["parent"] = parent
        if record.get("kind") == "session-snapshot":
            row["model"] = data.get("model") or "unknown"
            row["effort"] = data.get("reasoning_effort") or "unknown"
            row["native_refs"].append(record["id"])
        if record.get("kind") == "message":
            messages[record["id"]] = data
            if not row["native_refs"]:
                row["native_refs"].append(record["id"])

    calls = native_calls(records)
    assignments, reports = [], []
    resumed_at: dict[str, datetime] = {}
    for call in calls:
        tool, sid = call["tool"], call["session_id"]
        if not isinstance(tool, str) or not (
            tool.startswith("swarm_") or tool == "submit_report"
        ):
            continue
        refs = call["native_refs"]
        if call["schema_evidence_gap"]:
            gaps.append({"reason": call["schema_evidence_gap"], "native_refs": refs})
        if call["native_outcome"] == "unknown":
            gaps.append(
                {
                    "reason": "no matching terminal native tool result",
                    "native_refs": refs,
                }
            )
        if tool == "submit_report":
            outcome = "rejected" if call["native_outcome"] == "error" else "unknown"
            if (
                call["native_outcome"] == "completed"
                and isinstance(call["effective_schema"], dict)
                and call["schema_evidence_gap"] is None
                and isinstance(call["input"], dict)
                and _result_text(call["result"]).strip()
                == "Completion report accepted."
            ):
                outcome = "accepted"
            report = {
                "session_id": sid,
                "call_id": call["call_id"],
                "output_schema": call["effective_schema"],
                "arguments": call["input"],
                "accepted_or_rejected": outcome,
                "native_refs": refs,
            }
            reports.append(report)
            sessions[sid]["status"] = sessions[sid]["checks"] = "unknown"
            if outcome == "accepted":
                row = sessions[sid]
                row["status"] = call["input"].get("status", "unknown")
                row["checks"] = call["input"].get("checks", "unknown")
                row["native_refs"].extend(refs)
            else:
                gaps.append(
                    {
                        "reason": (
                            f"submit_report {outcome}; "
                            "status/checks not established by this call"
                        ),
                        "native_refs": refs,
                    }
                )
            continue

        inputs = call["input"] if isinstance(call["input"], dict) else {}
        mode = (
            "paging"
            if inputs.get("follow_up_session")
            else "resume"
            if inputs.get("resume_session")
            else "spawn"
        )
        # This line is a native blueprint tool result envelope, not worker prose.
        targets = re.findall(
            r"^Worker session: ([^\s]+)$", _result_text(call["result"]), re.MULTILINE
        )
        target = targets[0] if len(targets) == 1 else None
        if mode == "paging":
            target = inputs.get("follow_up_session")
        native = sessions.get(target, {})
        grounded = (
            call["native_outcome"] == "completed"
            and native.get("parent") == sid
            and native.get("tier") == tool.removeprefix("swarm_")
            and isinstance(call["effective_schema"], dict)
        )
        if mode == "resume":
            grounded = grounded and target == inputs.get("resume_session")
        assignments.append(
            {
                "session_id": sid,
                "call_id": call["call_id"],
                "tool": tool,
                "input_schema": call["effective_schema"],
                "input": call["input"],
                "tool_result_validation": {
                    "outcome": call["native_outcome"],
                    "result": call["result"],
                    "mode": mode,
                    "target_session_id": target if grounded else None,
                },
                "native_refs": refs,
            }
        )
        if mode != "paging" and not grounded:
            gaps.append(
                {
                    "reason": "assignment lacks confirmed native target/parent/worker",
                    "native_refs": refs,
                }
            )
        if grounded:
            native["native_refs"].extend(refs)
        if mode == "resume" and grounded:
            started = _timestamp(messages.get(refs[0], {}))
            later = [
                r
                for r in records
                if r.get("kind") == "message"
                and r.get("session_id") == target
                and isinstance(r.get("data"), dict)
                and (
                    r["data"].get("role") == "assistant"
                    or any(
                        b.get("type") in {"tool_use", "tool_result"}
                        for b in _blocks(r["data"])
                    )
                )
                and started is not None
                and _timestamp(r["data"]) is not None
                and _timestamp(r["data"]) > started
            ]
            if later:
                native["resumed"] = True
                native["native_refs"].extend(r["id"] for r in later)
                if started is not None:
                    resumed_at[target] = max(started, resumed_at.get(target, started))
            else:
                gaps.append(
                    {
                        "reason": (
                            "resume target confirmed but later "
                            "assistant/tool activity unavailable"
                        ),
                        "native_refs": refs,
                    }
                )

    # Exact structured identities only. A source claim is not native lineage.
    for report in reports:
        if report["accepted_or_rejected"] != "accepted":
            continue
        arguments = report["arguments"]
        coordination = arguments.get("coordination")
        if not isinstance(coordination, dict):
            gaps.append(
                {
                    "reason": "accepted report lacks coordination identities",
                    "native_refs": report["native_refs"],
                }
            )
            continue
        sid = report["session_id"]
        matching = [
            a
            for a in assignments
            if a["tool_result_validation"]["target_session_id"] == sid
            and a["tool_result_validation"]["mode"] != "paging"
            and isinstance(a["input"], dict)
            and isinstance(a["input"].get("assignment"), dict)
            and a["input"]["assignment"].get("task_id") == coordination.get("task_id")
        ]
        source = coordination.get("source_session_id")
        if source is not None:
            matching = [
                a
                for a in matching
                if a["input"]["assignment"].get("source_session_id") == source
                and a["input"]["assignment"].get("blocker_id") == coordination.get("blocker_id")
            ]
            origins = [
                r
                for r in reports
                if r["accepted_or_rejected"] == "accepted"
                and r["session_id"] == source
                and isinstance(r["arguments"].get("coordination"), dict)
                and r["arguments"]["coordination"].get("task_id")
                == coordination.get("task_id")
                and r["arguments"]["coordination"].get("blocker_id")
                == coordination.get("blocker_id")
            ]
            source_row = sessions.get(source, {})
            target_row = sessions[sid]
            related = (
                source_row.get("parent") == sid
                or target_row.get("parent") == source
                or (
                    target_row.get("parent") is not None
                    and source_row.get("parent") == target_row.get("parent")
                )
            )
            if not origins or source not in sessions or source == sid or not related:
                matching = []
            else:
                report["native_refs"] = [
                    *report["native_refs"],
                    *[ref for r in origins for ref in r["native_refs"]],
                ]
        if not matching:
            gaps.append(
                {
                    "reason": (
                        "report identities lack exact native assignment/source lineage"
                    ),
                    "native_refs": report["native_refs"],
                }
            )
        else:
            report["native_refs"] = [
                *report["native_refs"],
                *[ref for a in matching for ref in a["native_refs"]],
            ]
    for row in sessions.values():
        row["native_refs"] = list(dict.fromkeys(row["native_refs"]))
        if row["session"] in resumed_at:
            latest = [r for r in reports if r["session_id"] == row["session"]]
            stamp = (
                _timestamp(messages.get(latest[-1]["native_refs"][0], {}))
                if latest
                else None
            )
            if stamp is None or stamp <= resumed_at[row["session"]]:
                row["status"] = row["checks"] = "unknown"
                gaps.append(
                    {
                        "reason": "resumed worker has no later accepted report",
                        "session_id": row["session"],
                    }
                )
        if (
            row["tier"] == "unknown"
            or row["model"] == "unknown"
            or row["effort"] == "unknown"
        ):
            gaps.append(
                {
                    "reason": "native worker/model/effort context unavailable",
                    "session_id": row["session"],
                }
            )
        if row["tier"] != "coordinator" and row["status"] == "unknown":
            gaps.append(
                {
                    "reason": "worker has no accepted submit_report",
                    "session_id": row["session"],
                }
            )
    errors, hooks, error_gaps = select_tool_errors(
        records, trace_root=trace_root, calls=calls
    )
    return {
        "delegation": list(sessions.values()),
        "assignments": assignments,
        "reports": reports,
        "tool_errors": errors,
        "evidence_gaps": [*gaps, *error_gaps],
        "hook_records": hooks,
    }


def analyze_coordination(
    records: list[_JSON],
    *,
    trace_root: Path,
    evidence_gaps: list[Any],
) -> tuple[_JSON, list[_JSON]]:
    """Keep the old runner callable until its report pipeline is migrated."""
    report = build_delegation(
        records, trace_root=trace_root, evidence_gaps=evidence_gaps
    )
    hooks = report.pop("hook_records")
    return {
        **report,
        "status": "observed",
        "missing_evidence": report["evidence_gaps"],
    }, hooks
