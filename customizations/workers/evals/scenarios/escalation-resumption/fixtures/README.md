# Sensor-record pipeline milestones

This workspace is an original small sensor-record pipeline authored for this
project, not an upstream benchmark. Licensed under the project's root
license.

Complete the four ordered milestones in `workflow.py`. Each milestone has an
independently runnable acceptance test class in `workflow_test.py`:

| Stage | Milestone | Function | Checks |
| --- | --- | --- | --- |
| 1 | normalize input records | `normalize_records(raw)` | `TestMilestone1Normalize` |
| 2 | deduplicate stable keys | `deduplicate_records(records)` | `TestMilestone2Deduplicate` |
| 3 | dependency schedule | `schedule_subtasks(records)` | `TestMilestone3Schedule` |
| 4 | final aggregation | `aggregate_results(schedule, records)` | `TestMilestone4Aggregate` |

`TestCrossFunctionPipeline` chains all four milestones and belongs to the full
acceptance run only. Full acceptance: `python3 -m unittest workflow_test.py`.

## Behavior specification

Input records are dicts with `id` (string), `amount` (numeric string), `tags`
(comma-separated string), and an optional `after` list of dependency ids.

1. `normalize_records(raw)` returns one dict per input record with exactly
   `id` (stripped), `amount` (float), `tags` (stripped, lowercased, empty
   entries dropped, order preserved), and `after` (stripped, unique, sorted).
   Missing `id`/`amount`/`tags`, a non-numeric `amount`, a non-list input or
   item, or a non-list `after` raise `ValueError`. The input is not mutated.

2. `deduplicate_records(records)` drops later records whose stable key, the
   case-folded `id`, was already seen, keeping the first occurrence and the
   input order. A non-list input raises `ValueError`. The input is not
   mutated.

3. `schedule_subtasks(records)` returns the record ids ordered so every id
   appears exactly once and only after all ids in its `after` list (matched
   case-insensitively). At each step pick the earliest still-unscheduled
   record in input order whose dependencies are all scheduled. Unknown
   dependencies, self-dependencies, cycles, and duplicate record ids raise
   `ValueError`. The cross-function invariant: `after` references resolve
   against the ids that survive milestone 2, so the schedule of the sample
   below is stable regardless of which duplicate casing survives.

4. `aggregate_results(schedule, records)` returns
   `{"total": <sum of amounts, 2 decimals>, "count": <scheduled records>,
   "tags": <tag to scheduled-record count, keys sorted>}`. Scheduled ids
   resolve case-insensitively and must exist and be unique; otherwise
   `ValueError`.

## Sample expected outputs (independent acceptance)

```
RAW = [
    {"id": " sensor-A ", "amount": "12.50", "tags": " temp , Indoor ", "after": []},
    {"id": "sensor-a", "amount": "3.00", "tags": "temp", "after": ["sensor-A"]},
    {"id": "sensor-b", "amount": "7", "tags": "humidity, indoor", "after": ["Sensor-C"]},
    {"id": " sensor-c", "amount": "-2.5", "tags": "", "after": []},
]
normalize_records(RAW)      -> 4 normalized records (ids sensor-A, sensor-a,
                               sensor-b, sensor-c; amounts 12.5, 3.0, 7.0, -2.5)
deduplicate_records(...)    -> [sensor-A, sensor-b, sensor-c] records
schedule_subtasks(...)      -> ["sensor-A", "sensor-c", "sensor-b"]
aggregate_results(...)       -> {"total": 17.0, "count": 3,
                                 "tags": {"humidity": 1, "indoor": 2, "temp": 1}}
```

## Diagnostics marker protocol

`python3 diagnostics.py --stage <1|2|3|4>` runs only that stage's acceptance
class and prints exactly one machine-readable marker line:

```
JCODE_EVAL_CHECK {"evidence": "...", "exit": 0, "stage": 2}
```

The marker carries `stage`, `exit` (0 passed, 1 failed), and actionable
`evidence` naming the failing check. The process exit status equals the
check result; invalid usage exits 2.

The optional `--probe primary|secondary` runs a distinct actual diagnostic
pass over a different check subset of the same acceptance class: `primary` is
the full stage acceptance class, `secondary` is the complementary error-path
subset. A probe run's marker also carries the `probe` identity, and its
evidence names that probe's own failing checks. A bare run without `--probe`
remains the full acceptance run; the partial `secondary` subset can never
resolve a stage. This fixture itself never gates on probes.
