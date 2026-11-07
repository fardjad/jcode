"""Four-milestone sensor-record pipeline.

Baseline stubs fail by design. Implement each milestone per README.md and
verify with `python3 diagnostics.py --stage <1|2|3|4>`.
"""


def normalize_records(raw):
    """Milestone 1: normalize raw input records. Not implemented yet."""
    raise NotImplementedError("milestone 1: normalize_records")


def deduplicate_records(records):
    """Milestone 2: deduplicate records by stable key. Not implemented yet."""
    raise NotImplementedError("milestone 2: deduplicate_records")


def schedule_subtasks(records):
    """Milestone 3: order records after their dependencies. Not implemented yet."""
    raise NotImplementedError("milestone 3: schedule_subtasks")


def aggregate_results(schedule, records):
    """Milestone 4: aggregate scheduled records. Not implemented yet."""
    raise NotImplementedError("milestone 4: aggregate_results")
