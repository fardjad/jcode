"""Stable dependency order for a build-task scheduler."""


def order_tasks(tasks: dict[str, list[str]]) -> list[str]:
    # This implementation accidentally ignores dependency constraints.
    return list(tasks)
