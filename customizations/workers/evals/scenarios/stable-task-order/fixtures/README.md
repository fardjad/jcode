# Stable task order

Repair `order_tasks` without modifying the acceptance tests. The input maps task
names to dependency names. Return each task once, after its dependencies. At
every step choose the earliest currently ready task in original dictionary
insertion order, recomputing readiness after each emitted task. Duplicate
references to a dependency count once. Empty input returns an empty list.
Reject unknown dependencies, cycles, and self-dependencies with `ValueError`.
Do not mutate the caller's dictionary or dependency lists.

Acceptance: `python3 -m unittest scheduler_test.py`.

This is an original small fixture authored for this catalog, not an upstream
benchmark. Its baseline intentionally violates dependency order. It exercises
localized diagnosis and edge-case verification, but makes no claim that any
particular model must escalate. Licensed under the catalog's root license.
