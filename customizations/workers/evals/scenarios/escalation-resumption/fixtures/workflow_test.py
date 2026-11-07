"""Deterministic acceptance tests for the four escalation-resumption milestones.

Each milestone class is independently runnable, for example:
    python3 -m unittest workflow_test.TestMilestone2Deduplicate
TestCrossFunctionPipeline chains all four milestones and is part of the full
acceptance run only, not of the per-stage diagnostics.
"""

import copy
import unittest

import workflow

RAW = [
    {"id": " sensor-A ", "amount": "12.50", "tags": " temp , Indoor ", "after": []},
    {"id": "sensor-a", "amount": "3.00", "tags": "temp", "after": ["sensor-A"]},
    {"id": "sensor-b", "amount": "7", "tags": "humidity, indoor", "after": ["Sensor-C"]},
    {"id": " sensor-c", "amount": "-2.5", "tags": "", "after": []},
]

NORMALIZED = [
    {"id": "sensor-A", "amount": 12.5, "tags": ["temp", "indoor"], "after": []},
    {"id": "sensor-a", "amount": 3.0, "tags": ["temp"], "after": ["sensor-A"]},
    {"id": "sensor-b", "amount": 7.0, "tags": ["humidity", "indoor"], "after": ["Sensor-C"]},
    {"id": "sensor-c", "amount": -2.5, "tags": [], "after": []},
]

# Stable key is the case-folded id; the first occurrence wins.
DEDUPLICATED = [NORMALIZED[0], NORMALIZED[2], NORMALIZED[3]]

SCHEDULE = ["sensor-A", "sensor-c", "sensor-b"]

AGGREGATE = {"total": 17.0, "count": 3, "tags": {"humidity": 1, "indoor": 2, "temp": 1}}


class TestMilestone1Normalize(unittest.TestCase):
    def test_milestone1_normalizes_sample(self):
        self.assertEqual(workflow.normalize_records(copy.deepcopy(RAW)), NORMALIZED)

    def test_milestone1_does_not_mutate_input(self):
        before = copy.deepcopy(RAW)
        workflow.normalize_records(RAW)
        self.assertEqual(RAW, before)

    def test_milestone1_rejects_missing_key(self):
        with self.assertRaises(ValueError):
            workflow.normalize_records([{"id": "x", "amount": "1"}])

    def test_milestone1_rejects_non_numeric_amount(self):
        with self.assertRaises(ValueError):
            workflow.normalize_records([{"id": "x", "amount": "free", "tags": ""}])

    def test_milestone1_rejects_non_list_input(self):
        with self.assertRaises(ValueError):
            workflow.normalize_records("sensor-A")

    def test_milestone1_rejects_non_dict_item(self):
        with self.assertRaises(ValueError):
            workflow.normalize_records(["sensor-A"])

    def test_milestone1_rejects_non_list_after(self):
        with self.assertRaises(ValueError):
            workflow.normalize_records(
                [{"id": "x", "amount": "1", "tags": "", "after": "y"}]
            )


class TestMilestone2Deduplicate(unittest.TestCase):
    def test_milestone2_deduplicates_sample(self):
        self.assertEqual(
            workflow.deduplicate_records(copy.deepcopy(NORMALIZED)), DEDUPLICATED
        )

    def test_milestone2_keeps_first_occurrence(self):
        records = [
            {"id": "A", "amount": 1.0, "tags": ["t"], "after": []},
            {"id": "a", "amount": 2.0, "tags": ["u"], "after": []},
        ]
        self.assertEqual(workflow.deduplicate_records(records), [records[0]])

    def test_milestone2_does_not_mutate_input(self):
        before = copy.deepcopy(NORMALIZED)
        workflow.deduplicate_records(NORMALIZED)
        self.assertEqual(NORMALIZED, before)

    def test_milestone2_rejects_non_list_input(self):
        with self.assertRaises(ValueError):
            workflow.deduplicate_records({"id": "x"})


class TestMilestone3Schedule(unittest.TestCase):
    def test_milestone3_schedules_sample(self):
        self.assertEqual(
            workflow.schedule_subtasks(copy.deepcopy(DEDUPLICATED)), SCHEDULE
        )

    def test_milestone3_rejects_unknown_dependency(self):
        records = [{"id": "x", "amount": 1.0, "tags": [], "after": ["ghost"]}]
        with self.assertRaises(ValueError):
            workflow.schedule_subtasks(records)

    def test_milestone3_rejects_cycle(self):
        records = [
            {"id": "x", "amount": 1.0, "tags": [], "after": ["y"]},
            {"id": "y", "amount": 2.0, "tags": [], "after": ["x"]},
        ]
        with self.assertRaises(ValueError):
            workflow.schedule_subtasks(records)

    def test_milestone3_rejects_self_dependency(self):
        records = [{"id": "x", "amount": 1.0, "tags": [], "after": ["X"]}]
        with self.assertRaises(ValueError):
            workflow.schedule_subtasks(records)

    def test_milestone3_rejects_duplicate_ids(self):
        records = [
            {"id": "x", "amount": 1.0, "tags": [], "after": []},
            {"id": "X", "amount": 2.0, "tags": [], "after": []},
        ]
        with self.assertRaises(ValueError):
            workflow.schedule_subtasks(records)

    def test_milestone3_rejects_non_list_input(self):
        with self.assertRaises(ValueError):
            workflow.schedule_subtasks("records")


class TestMilestone4Aggregate(unittest.TestCase):
    def test_milestone4_aggregates_sample(self):
        self.assertEqual(
            workflow.aggregate_results(SCHEDULE, copy.deepcopy(DEDUPLICATED)),
            AGGREGATE,
        )

    def test_milestone4_returns_sorted_tag_keys(self):
        result = workflow.aggregate_results(SCHEDULE, DEDUPLICATED)
        self.assertEqual(list(result["tags"]), sorted(result["tags"]))

    def test_milestone4_rejects_unknown_scheduled_id(self):
        with self.assertRaises(ValueError):
            workflow.aggregate_results(["ghost"], DEDUPLICATED)

    def test_milestone4_rejects_repeated_scheduled_id(self):
        with self.assertRaises(ValueError):
            workflow.aggregate_results(["sensor-A", "SENSOR-a"], DEDUPLICATED)

    def test_milestone4_rejects_duplicate_record_ids(self):
        records = [DEDUPLICATED[0], dict(DEDUPLICATED[0])]
        with self.assertRaises(ValueError):
            workflow.aggregate_results(["sensor-A"], records)

    def test_milestone4_rejects_non_list_arguments(self):
        with self.assertRaises(ValueError):
            workflow.aggregate_results("schedule", DEDUPLICATED)


class TestCrossFunctionPipeline(unittest.TestCase):
    """Full-chain checks: milestone 3 and 4 invariants across all milestones."""

    def test_cross_function_schedule_invariant(self):
        schedule = workflow.schedule_subtasks(
            workflow.deduplicate_records(workflow.normalize_records(RAW))
        )
        self.assertEqual(schedule, SCHEDULE)
        positions = {name.casefold(): i for i, name in enumerate(schedule)}
        for record in DEDUPLICATED:
            for dep in record["after"]:
                self.assertIn(dep.casefold(), positions)
                self.assertLess(positions[dep.casefold()], positions[record["id"].casefold()])

    def test_cross_function_aggregate_matches(self):
        schedule = workflow.schedule_subtasks(
            workflow.deduplicate_records(workflow.normalize_records(RAW))
        )
        records = workflow.deduplicate_records(workflow.normalize_records(RAW))
        self.assertEqual(workflow.aggregate_results(schedule, records), AGGREGATE)


if __name__ == "__main__":
    unittest.main()
