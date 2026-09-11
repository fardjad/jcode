#!/usr/bin/env python3
"""Install the catalogued coordinator prompt into the global prompt overlay.

The installer owns only the section delimited by its markers. All other overlay
content remains untouched. Run it repeatedly to update the managed section.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

START = "<!-- jcode-workers:coordinator-prompt:start -->"
END = "<!-- jcode-workers:coordinator-prompt:end -->"


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    root = Path(__file__).resolve().parent
    source = root / "coordinator-prompt.md"
    if not source.is_file():
        fail(f"catalog prompt not found: {source}")

    # JCODE_HOME makes the destination testable and supports intentional custom
    # jcode homes. It never inherits XDG configuration locations.
    jcode_home = Path(os.environ.get("JCODE_HOME", Path.home() / ".jcode"))
    destination = jcode_home / "prompt-overlay.md"
    prompt = source.read_text(encoding="utf-8").rstrip()
    managed = f"{START}\n{prompt}\n{END}\n"

    if destination.is_symlink():
        target = destination.resolve(strict=True)
        if not target.is_file():
            fail(f"overlay symlink does not target a regular file: {destination}")
        destination = target
    if destination.exists() and not destination.is_file():
        fail(f"overlay path is not a regular file: {destination}")

    existing = destination.read_text(encoding="utf-8") if destination.exists() else ""
    start_count = existing.count(START)
    end_count = existing.count(END)
    if start_count != end_count or start_count > 1:
        fail(f"malformed managed prompt markers in: {destination}")

    if start_count:
        start = existing.index(START)
        end = existing.index(END, start) + len(END)
        before = existing[:start].rstrip()
        after = existing[end:].strip()
        parts = [part for part in (before, managed.rstrip(), after) if part]
        updated = "\n\n".join(parts) + "\n"
    elif existing.rstrip().endswith(prompt):
        # Adopt the prior unmarked catalog prompt without duplicating it.
        before = existing.rstrip()[: -len(prompt)].rstrip()
        updated = f"{before}\n\n{managed}" if before else managed
    else:
        updated = f"{existing.rstrip()}\n\n{managed}" if existing.strip() else managed

    if updated == existing:
        print(f"coordinator prompt already installed: {destination}")
        return

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    try:
        temporary.write_text(updated, encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"installed coordinator prompt: {destination}")


if __name__ == "__main__":
    main()
