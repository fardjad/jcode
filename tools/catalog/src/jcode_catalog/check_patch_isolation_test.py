"""Tests for declared patch dependency isolation."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .check_patch_isolation import check
from .isolate_config import isolated_environment
from .patch_catalog import CatalogError


class PatchIsolationTests(unittest.TestCase):
    def test_undeclared_context_dependency_fails_and_declared_dependency_passes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "patches").mkdir()
            self._run_git(root, "init", "-q")
            self._configure_repository(root)
            (root / "sample.txt").write_text("first base\nsecond base\n", encoding="utf-8")
            self._run_git(root, "add", "sample.txt")
            self._run_git(root, "commit", "-q", "-m", "base")
            base = self._run_git(root, "rev-parse", "HEAD")

            self._write_change(root, "first updated\nsecond base\n", "0001-personal-first.patch", "none")
            self._write_change(root, "first updated\nsecond final\n", "0002-personal-second.patch", "none")

            with patch("builtins.print") as output:
                with self.assertRaises(CatalogError) as raised:
                    check(root, base)
            self.assertIn("0002-personal-second.patch", str(raised.exception))
            self.assertTrue(any("FAIL 0002-personal-second.patch" in str(call) for call in output.call_args_list))

            second = root / "patches" / "0002-personal-second.patch"
            text = second.read_text(encoding="utf-8")
            second.write_text(
                text.replace("X-Jcode-Patch-Depends-On: none", "X-Jcode-Patch-Depends-On: 0001-personal-first.patch"),
                encoding="utf-8",
            )
            with patch("builtins.print") as output:
                check(root, base)
            self.assertTrue(any("ok 0002-personal-second.patch" in str(call) for call in output.call_args_list))

    def _write_change(self, root: Path, contents: str, name: str, dependency: str) -> None:
        (root / "sample.txt").write_text(contents, encoding="utf-8")
        self._run_git(root, "add", "sample.txt")
        message = (
            f"change {name}\n\n"
            f"X-Jcode-Patch-Intent: {name}\n"
            "X-Jcode-Patch-Kind: personal-feature\n"
            f"X-Jcode-Patch-Depends-On: {dependency}\n\n"
            "Patch intent:\nExercise dependency isolation.\n\n"
            "Why it exists:\nTest declared dependencies.\n\n"
            "Upstream integration points:\nGit mail patches.\n\n"
            "Update guidance:\nKeep dependencies explicit.\n\n"
            "Validation:\necho test"
        )
        self._run_git(root, "commit", "-q", "-m", message)
        patch_text = self._run_git(root, "format-patch", "--stdout", "-1")
        (root / "patches" / name).write_text(patch_text + "\n", encoding="utf-8")

    @classmethod
    def _configure_repository(cls, root: Path) -> None:
        cls._run_git(root, "config", "user.name", "Test User")
        cls._run_git(root, "config", "user.email", "test@example.invalid")

    @staticmethod
    def _run_git(cwd: Path, *arguments: str) -> str:
        with isolated_environment() as environment:
            result = subprocess.run(
                ["git", *arguments], cwd=cwd, env=environment, check=True,
                capture_output=True, text=True,
            )
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
