"""Best-effort daily advisory for stable cargo-nextest releases."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import sys
import time
import urllib.request

from . import execution, validation_state

LATEST_URL = "https://api.github.com/repos/nextest-rs/nextest/releases/latest"
USER_AGENT = "jcode-nextest-update-check/1.0"
CHECK_INTERVAL = 86400


def _version(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([0-9]+)\.([0-9]+)\.([0-9]+)", value)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _checked_at(path: Path) -> int | float | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    value = state.get("checked_at") if isinstance(state, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def check(root: Path, pinned: str) -> None:
    """Check at most once daily, without changing the pin or blocking bootstrap."""
    try:
        pinned_version = _version(pinned)
        if pinned_version is None:
            return
        directory = root / ".tools" / "nextest"
        state_path = directory / "update-check.json"
        with validation_state.exclusive_lock(
            directory / "update-check.lock", "cargo-nextest update check"
        ):
            now = time.time()
            checked_at = _checked_at(state_path)
            if checked_at is not None and now - checked_at < CHECK_INTERVAL:
                return
            # Persist the attempt first, including failures and process crashes.
            execution.atomic_json(state_path, {"checked_at": now})
            request = urllib.request.Request(LATEST_URL, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=3) as response:
                release = json.load(response)
            if not isinstance(release, dict):
                return
            if release.get("draft") is not False or release.get("prerelease") is not False:
                return
            tag = release.get("tag_name")
            if not isinstance(tag, str):
                return
            match = re.fullmatch(r"cargo-nextest-([0-9]+\.[0-9]+\.[0-9]+)", tag)
            if match is None:
                return
            latest = match.group(1)
            latest_version = _version(latest)
            execution.atomic_json(state_path, {"checked_at": now, "latest": latest})
            if latest_version is not None and latest_version > pinned_version:
                print(
                    f"cargo-nextest {latest} is available (pinned: {pinned}); "
                    "update the catalog pin explicitly.",
                    file=sys.stderr,
                )
    except Exception:
        # This advisory must not interfere with use of the pinned executable.
        return
