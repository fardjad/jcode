#!/usr/bin/env python3
"""Normalize personal mail-patch ``From`` hashes for reproducible catalogs.

Personal catalog patches intentionally use an all-zero hash in their first mail
header. This avoids irrelevant diffs when their backing commits are recreated.
Upstream-candidate patches retain their real hash and are left unchanged.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ZERO_HASH = "0" * 40
PATCH_ENCODING = "utf-8"
FROM_HEADER = re.compile(r"^(From )([0-9a-f]{40})( .*)$", re.MULTILINE)
KIND_HEADER = re.compile(r"^X-Jcode-Patch-Kind:\s*(\S.*?)\s*$", re.MULTILINE)


def repository_root() -> Path:
    return Path(__file__).resolve().parent.parent


def patch_kind(path: Path, text: str) -> str:
    matches = KIND_HEADER.findall(text)
    if len(matches) != 1:
        raise ValueError(
            f"{path}: expected exactly one X-Jcode-Patch-Kind header, found {len(matches)}"
        )
    return matches[0]


def normalize_patch(path: Path) -> bool:
    """Zero one personal patch's top-level mail hash, returning whether it changed."""
    text = path.read_text(encoding=PATCH_ENCODING)
    if patch_kind(path, text) == "upstream-candidate":
        return False

    match = FROM_HEADER.search(text)
    if match is None:
        raise ValueError(f"{path}: missing a 40-character mail-patch From hash")
    if match.group(2) == ZERO_HASH:
        return False

    normalized = text[: match.start(2)] + ZERO_HASH + text[match.end(2) :]
    path.write_text(normalized, encoding=PATCH_ENCODING)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "patches",
        nargs="*",
        type=Path,
        help="patch files to normalize (defaults to patches/*.patch)",
    )
    args = parser.parse_args()
    root = repository_root()
    paths = args.patches or sorted((root / "patches").glob("*.patch"))
    if not paths:
        print("no patch files found", file=sys.stderr)
        return 1

    changed = 0
    try:
        for path in paths:
            if not path.is_file():
                raise ValueError(f"{path}: not a file")
            if normalize_patch(path):
                changed += 1
                print(f"normalized: {path}")
    except (OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"normalization complete: {changed} patch(es) changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
