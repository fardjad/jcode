from __future__ import annotations

from copy import deepcopy

import pytest

from jcode_harness_client.comparison import compare_runs


def run() -> dict:
    return {
        "scenarios": [
            {
                "id": name,
                "fixture_sha256": "fixture",
                "scenario_source_sha256": "scenario",
                "acceptance": {"status": status},
                "duration_seconds": duration,
                "usage": {
                    "agent": {"cost_usd": 2.0, "input_tokens": 100, "output_tokens": 50}
                },
            }
            for name, status, duration in [("one", "pass", 10), ("two", "fail", 20)]
        ]
    }


def test_failed_spend_is_included_in_accepted_task_cost() -> None:
    report = compare_runs(run(), run())
    assert report["baseline"]["accepted"] == 1
    assert report["baseline"]["cost_per_accepted_task"] == 4
    assert report["baseline"]["tokens_per_accepted_task"] == 300
    assert report["baseline"]["duration_median_seconds"] == 15


@pytest.mark.parametrize("field", ["fixture_sha256", "scenario_source_sha256", "id"])
def test_mismatched_pairs_rejected(field: str) -> None:
    other = deepcopy(run())
    other["scenarios"][0][field] = "changed"
    with pytest.raises(ValueError):
        compare_runs(run(), other)


def test_unpriced_or_legacy_usage_never_looks_like_zero_cost() -> None:
    other = run()
    other["scenarios"][0]["usage"]["agent"]["unpriced_calls"] = 1
    assert compare_runs(run(), other)["candidate"]["agent_cost_usd"] is None
    del other["scenarios"][0]["usage"]["agent"]
    report = compare_runs(run(), other)["candidate"]
    assert not report["usage_complete"]
    assert report["agent_input_output_tokens"] is None


def test_no_success_has_no_cost_per_success() -> None:
    other = run()
    for item in other["scenarios"]:
        item["acceptance"]["status"] = "fail"
    assert compare_runs(run(), other)["candidate"]["cost_per_accepted_task"] is None
