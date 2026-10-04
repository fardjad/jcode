"""Focused tests for stable release pin parsing and atomic writes."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from version_base import (
    VersionError,
    latest_stable_release,
    pinned_version,
    require_stable_release,
    write_version,
)


class VersionBaseTests(unittest.TestCase):
    def test_only_stable_release_tags_are_accepted(self) -> None:
        for version in ("v0.88.0", "v1.2.3"):
            with self.subTest(version=version):
                self.assertEqual(require_stable_release(version), version)
        for version in ("master", "v1.2", "v1.2.3-rc.1", "0.88.0", "v1.2.3\nextra"):
            with self.subTest(version=version):
                with self.assertRaises(VersionError):
                    require_stable_release(version)

    def test_pin_read_and_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "VERSION.txt").write_text("v0.88.0\n", encoding="utf-8")
            self.assertEqual(pinned_version(root), "v0.88.0")
            write_version(root, "v0.89.0")
            self.assertEqual((root / "VERSION.txt").read_text(encoding="utf-8"), "v0.89.0\n")
            with self.assertRaises(VersionError):
                write_version(root, "master")

    def test_version_order_selects_latest_stable_release(self) -> None:
        self.assertEqual(
            latest_stable_release(["v0.9.0", "v0.10.0", "v0.100.0", "v1.0.0-rc.1"]),
            "v0.100.0",
        )
        with self.assertRaises(VersionError):
            latest_stable_release(["master", "v1.0.0-rc.1"])


if __name__ == "__main__":
    unittest.main()
