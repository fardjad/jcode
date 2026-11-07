"""Offline matched evaluation comparison. Never launches agents or providers."""

from __future__ import annotations

from statistics import median
from typing import Any

_JSON = dict[str, Any]


def compare_runs(baseline: _JSON, candidate: _JSON) -> _JSON:
    """Compare matching fixture/scenario identities; retain all failed spend."""

    def indexed(run: _JSON) -> dict[str, _JSON]:
        scenarios = run.get("scenarios")
        if not isinstance(scenarios, list):
            raise ValueError("run summary must contain scenarios")
        result = {}
        for scenario in scenarios:
            if not isinstance(scenario, dict) or not isinstance(
                scenario.get("id"), str
            ):
                raise ValueError("scenario must have an id")
            if scenario["id"] in result:
                raise ValueError(
                    "duplicate scenario id; compare repeated runs separately"
                )
            result[scenario["id"]] = scenario
        return result

    left, right = indexed(baseline), indexed(candidate)
    if left.keys() != right.keys():
        raise ValueError("paired runs must contain identical scenario ids")
    pairs = []
    for name in sorted(left):
        before, after = left[name], right[name]
        for key in ("fixture_sha256", "scenario_source_sha256"):
            if not before.get(key) or before.get(key) != after.get(key):
                raise ValueError(f"{name}: mismatched or missing {key}")
        pairs.append({"id": name, "baseline": before, "candidate": after})

    def aggregate(scenarios: dict[str, _JSON]) -> _JSON:
        successes = sum(
            item.get("acceptance", {}).get("status") == "pass"
            for item in scenarios.values()
        )
        agents = [item.get("usage", {}).get("agent") for item in scenarios.values()]
        usage_complete = all(
            isinstance(agent, dict)
            and all(
                isinstance(agent.get(key), int) and agent[key] >= 0
                for key in ("input_tokens", "output_tokens")
            )
            for agent in agents
        )
        agent_records = [agent for agent in agents if isinstance(agent, dict)]
        unpriced = sum(agent.get("unpriced_calls", 0) for agent in agent_records)
        cost_complete = (
            usage_complete
            and not unpriced
            and all(agent.get("cost_usd") is not None for agent in agent_records)
        )
        cost = (
            sum(agent["cost_usd"] for agent in agent_records) if cost_complete else None
        )
        tokens = (
            sum(
                agent.get("input_tokens", 0) + agent.get("output_tokens", 0)
                for agent in agent_records
            )
            if usage_complete
            else None
        )
        durations = [
            item["duration_seconds"]
            for item in scenarios.values()
            if isinstance(item.get("duration_seconds"), (int, float))
        ]
        return {
            "tasks": len(scenarios),
            "accepted": successes,
            "acceptance_rate": successes / len(scenarios) if scenarios else None,
            "agent_cost_usd": cost,
            "agent_input_output_tokens": tokens,
            "unpriced_calls": unpriced,
            "usage_complete": usage_complete,
            "cost_per_accepted_task": cost / successes
            if cost is not None and successes
            else None,
            "tokens_per_accepted_task": tokens / successes
            if tokens is not None and successes
            else None,
            "duration_complete": len(durations) == len(scenarios),
            "failed_or_unaccepted": len(scenarios) - successes,
            "execution_unknown": sum(
                "status" not in item.get("execution", {}) for item in scenarios.values()
            ),
            "execution_errors": sum(
                item.get("execution", {}).get("status") not in {None, "completed"}
                for item in scenarios.values()
            ),
            "duration_median_seconds": median(durations) if durations else None,
            "duration_range_seconds": [min(durations), max(durations)]
            if durations
            else None,
        }

    return {
        "pairs": pairs,
        "baseline": aggregate(left),
        "candidate": aggregate(right),
        "note": "Descriptive paired evidence, not statistical significance. "
        "Check run manifests for model/build/environment comparability. "
        "Cache tokens remain separate in retained per-task usage.",
    }
