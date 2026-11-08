"""Render report data without I/O or verdict inference."""

from __future__ import annotations

import json
from typing import Any


def _text(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return ", ".join(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _cell(value: Any) -> str:
    text = _text(value).replace("\n", " ")
    if len(text) > 80:
        text = text[:38] + "…" + text[-38:]
    return text.replace("|", "\\|")


def _checks(value: Any) -> str:
    if not isinstance(value, list):
        return "unknown"
    failed = sum(
        1 for check in value
        if isinstance(check, dict) and isinstance(check.get("exit_code"), int)
        and not isinstance(check["exit_code"], bool) and check["exit_code"] != 0
    )
    unknown = sum(
        1 for check in value
        if not isinstance(check, dict) or not isinstance(check.get("exit_code"), int)
        or isinstance(check.get("exit_code"), bool)
    )
    suffix = f", {unknown} unknown" if unknown else ""
    return f"{len(value)} ({failed} failed{suffix})"


def _extras_summary(extras: list[Any]) -> str | None:
    events = [item.get("journal_event") for item in extras if isinstance(item, dict)]
    events = [event for event in events if isinstance(event, dict)]
    if not events:
        return None
    injections = [event for event in events if event.get("injected") is True]
    stages = sorted({str(event["stage"]) for event in injections if event.get("stage") is not None})
    guard = sum(1 for event in events if isinstance(event.get("guard"), dict) and event["guard"].get("replacement") is True)
    mismatches = sum(1 for event in events if event.get("kind") == "marker-mismatch")
    return (f"Injections: {len(injections)} (stages {', '.join(stages) or 'none'}); "
            f"guard replacements: {guard}; marker mismatches: {mismatches}")


def render_report(report: dict[str, Any]) -> str:
    """Render one scenario; use effective effort when native effort is absent.

    Accepts a single scenario or {"scenarios": [scenario, ...]} for a run.
    Required scenario keys: scenario, acceptance, cost_usd, unpriced_calls, wall, time,
    delegation, scenario_extras, findings. Time uses build_check,
    coordinator_turns, coordinator_turn_count, simulator, acceptance, judge,
    and report. Missing optional values are rendered as unknown.
    """
    if "scenarios" in report:
        return (
            "\n\n".join(
                render_report(item).rstrip("\n") for item in report["scenarios"]
            )
            + "\n"
        )
    cost = report.get("cost_usd")
    price = "unknown" if cost is None else f"${cost:.4f}"
    status = _text(report.get("acceptance")).upper()
    rows = report.get("delegation", [])
    models = []
    seen: set[tuple[str, str, str]] = set()
    for row in rows:
        tier = row.get("tier")
        effort = row.get("effort")
        if effort in (None, "", "unknown"):
            effective = report.get("effective_effort") or {}
            if tier == "coordinator":
                effort = effective.get("coordinator")
            elif tier not in (None, "unknown"):
                effort = effective.get("workers", {}).get(tier)
        identity = (_text(tier), _text(row.get("model")), _text(effort))
        if identity not in seen:
            seen.add(identity)
            models.append(f"{identity[0]} {identity[1]}/{identity[2]}")
    times = report.get("time", {})
    timing = " · ".join(
        [
            f"build check {_text(times.get('build_check'))}",
            f"coordinator turns {_text(times.get('coordinator_turns'))} "
            f"({_text(times.get('coordinator_turn_count'))})",
            *[
                f"{name} {_text(times.get(name))}"
                for name in ("simulator", "acceptance", "judge", "report")
            ],
        ]
    )
    lines = [
        f"# {_text(report.get('scenario'))} — acceptance {status} — {price} "
        f"({report.get('unpriced_calls', 0)} calls unpriced) — "
        f"{_text(report.get('wall'))}",
        "",
        f"Models: {' · '.join(models) or 'unknown'}",
        "",
        "## Time",
        "",
        timing,
        "",
        "## Delegation",
        "",
        "| session | tier | parent | model | status | resumed | checks |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                _cell(_checks(row.get(key)) if key == "checks" else row.get(key))
                for key in (
                    "session",
                    "tier",
                    "parent",
                    "model",
                    "status",
                    "resumed",
                    "checks",
                )
            )
            + " |"
        )
    summary = _extras_summary(report.get("scenario_extras", []))
    if summary:
        lines.extend(["", summary])
    lines.extend(["", "## Findings", ""])
    findings = report.get("findings", [])
    if not findings:
        lines.append("None reported.")
    for index, finding in enumerate(findings, 1):
        citations = (
            " · ".join(_text(ref) for ref in finding.get("citations", [])) or "unknown"
        )
        lines.extend(
            [
                f"{index}. [{finding['severity']} · {finding['category']}] "
                f"{_text(finding['text'])}",
                f"   cite: {citations} · fix target: {_text(finding['fix_target'])}",
            ]
        )
    return "\n".join(lines) + "\n"
