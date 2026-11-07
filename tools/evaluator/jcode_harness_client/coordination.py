"""Observed delegation and structured handback facts, not quality verdicts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_JSON = dict[str, Any]
_PACKET = re.compile(r"```coordination\s*\n(.*?)\n```", re.DOTALL)
_EVENTS = {"blocked", "assistance", "handback", "done"}
_IDENTIFIER_CHAR = re.compile(r"[0-9A-Za-z_-]")


def _mentions_identity(assignment: str, identity: str) -> bool:
    """Exact identifier-token match; prefix substrings never correlate."""
    if not identity:
        return False
    for match in re.finditer(re.escape(identity), assignment):
        before = assignment[match.start() - 1] if match.start() else ""
        after = assignment[match.end()] if match.end() < len(assignment) else ""
        if not _IDENTIFIER_CHAR.match(before) and not _IDENTIFIER_CHAR.match(after):
            return True
    return False


def _grounded_spawn(
    calls: list[_JSON], spawner: Any, worker: Any, identities: tuple[str, ...]
) -> bool:
    """A native swarm call by ``spawner`` of ``worker`` carrying the identities."""
    return any(
        call["session_id"] == spawner
        and call["worker"] == worker
        and all(
            _mentions_identity(str(call["assignment"]), identity)
            for identity in identities
        )
        for call in calls
    )


def analyze_coordination(
    records: list[_JSON],
    *,
    trace_root: Path,
    evidence_gaps: list[Any],
) -> tuple[_JSON, list[_JSON]]:
    """Correlate claimed task lineage with observed native worker identities.

    Reports intentionally cannot infer task difficulty, helpfulness, or whether
    a retry was meaningful. Packet text is untrusted worker-reported evidence.
    """
    del trace_root
    sessions: dict[str, _JSON] = {}
    calls: list[_JSON] = []
    packets: list[_JSON] = []
    missing = list(evidence_gaps)
    for record in records:
        sid = record.get("session_id")
        if not isinstance(sid, str):
            continue
        data = record.get("data", {})
        if not isinstance(data, dict):
            continue
        policy = data.get("worker_blueprint_policy", {})
        worker = record.get("worker") or (
            policy.get("name") if isinstance(policy, dict) else None
        )
        parent = record.get("parent_session_id") or data.get("parent_session_id")
        session = sessions.setdefault(sid, {"session_id": sid})
        if worker:
            session["worker"] = worker
        if parent:
            session["parent_session_id"] = parent
        if record.get("kind") != "message":
            continue
        for block in data.get("content", []):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                name = block.get("name", "")
                if isinstance(name, str) and name.startswith("swarm_"):
                    inputs = block.get("input", {})
                    if isinstance(inputs, dict) and inputs.get("task"):
                        calls.append(
                            {
                                "session_id": sid,
                                "worker": name.removeprefix("swarm_"),
                                "call_id": block.get("id"),
                                "citation": record.get("id"),
                                "assignment": inputs["task"],
                            }
                        )
            if block.get("type") != "text":
                continue
            text = block.get("text", "")
            if not isinstance(text, str):
                continue
            for match in _PACKET.finditer(text):
                try:
                    packet = json.loads(match.group(1))
                except json.JSONDecodeError:
                    missing.append(
                        {
                            "citation": record.get("id"),
                            "reason": "invalid coordination JSON",
                        }
                    )
                    continue
                if (
                    not isinstance(packet, dict)
                    or not isinstance(packet.get("event"), str)
                    or packet["event"] not in _EVENTS
                ):
                    missing.append(
                        {
                            "citation": record.get("id"),
                            "reason": "invalid coordination event",
                        }
                    )
                    continue
                if not isinstance(packet.get("task_id"), str) or not packet["task_id"]:
                    missing.append(
                        {
                            "citation": record.get("id"),
                            "reason": "missing task identity",
                        }
                    )
                    continue
                packets.append(
                    {"packet": packet, "session_id": sid, "citation": record.get("id")}
                )

    links: list[_JSON] = []
    for item in packets:
        packet = item["packet"]
        if packet["event"] not in {"assistance", "handback"}:
            continue
        source_value = packet.get("source_session_id")
        source = source_value if isinstance(source_value, str) else None
        origins = [
            other
            for other in packets
            if other["session_id"] == source
            and other["packet"]["task_id"] == packet["task_id"]
            and other["packet"].get("blocker_id") == packet.get("blocker_id")
            and other["packet"]["event"]
            == ("blocked" if packet["event"] == "assistance" else "assistance")
        ]
        target_session = sessions.get(item["session_id"], {})
        source_session = sessions.get(source, {}) if source is not None else {}
        parent = target_session.get("parent_session_id")
        source_parent = source_session.get("parent_session_id")
        task_id = packet["task_id"]
        blocker_id = str(packet.get("blocker_id"))
        # Sibling helpers share the coordinator parent, whose assignment
        # carried the full task/source/blocker identity triple. The source
        # worker must also have been natively spawned for this shared task.
        sibling_source_call = (
            parent is not None
            and parent == source_parent
            and _grounded_spawn(calls, parent, source_session.get("worker"), (task_id,))
        )
        sibling_call = (
            parent is not None
            and parent == source_parent
            and sibling_source_call
            and _grounded_spawn(
                calls,
                parent,
                target_session.get("worker"),
                (task_id, str(source), blocker_id),
            )
        )
        # A child hands back to its own parent: grounded by that parent's
        # native spawn call and the task identity, not by prose claims. The
        # blocker is discovered later, so only the packets must agree on it.
        child_handback_call = (
            source is not None
            and source == parent
            and _grounded_spawn(calls, source, target_session.get("worker"), (task_id,))
        )
        # A parent assists its own child: grounded by its native spawn of the
        # reported source session and the shared task identity.
        parent_assistance_call = (
            source is not None
            and source_parent == item["session_id"]
            and _grounded_spawn(
                calls, item["session_id"], source_session.get("worker"), (task_id,)
            )
        )
        native_call = sibling_call or child_handback_call or parent_assistance_call
        correlated = bool(
            source
            and source != item["session_id"]
            and origins
            and packet.get("blocker_id")
            and parent
            and native_call
        )
        links.append(
            {
                "event": packet["event"],
                "task_id": packet["task_id"],
                "blocker_id": packet.get("blocker_id"),
                "source_session_id": source,
                "session_id": item["session_id"],
                "status": "correlated" if correlated else "unknown",
                "citation": item["citation"],
                "source_citations": [origin["citation"] for origin in origins],
            }
        )
        if not correlated:
            missing.append(
                {
                    "citation": item["citation"],
                    "reason": "lineage or native delegation not correlated",
                }
            )

    if calls and not packets:
        missing.append({"reason": "workers observed without structured task lineage"})
    repeated: list[_JSON] = []
    blocked: dict[tuple[str, str], list[Any]] = {}
    for item in packets:
        packet = item["packet"]
        if packet["event"] == "blocked" and packet.get("blocker_id"):
            key = (packet["task_id"], str(packet["blocker_id"]))
            blocked.setdefault(key, []).append(item["citation"])
    for (task_id, blocker_id), citations in blocked.items():
        if len(citations) > 1:
            repeated.append(
                {"task_id": task_id, "blocker_id": blocker_id, "citations": citations}
            )
    return {
        "status": "incomplete" if missing else "healthy",
        "delegations": calls,
        "worker_sessions": list(sessions.values()),
        "reported_events": packets,
        "links": links,
        "repeated_blockers": repeated,
        "missing_evidence": missing,
        "note": (
            "Correlated facts are not proof of useful escalation, scope, or quality."
        ),
    }, []
