"""Tests for Git failure diagnostics in patch catalog workflows."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from patch_catalog import CatalogError, git


class GitDiagnosticsTests(unittest.TestCase):
    @patch("patch_catalog.subprocess.run")
    def test_quiet_success_captures_stderr_and_strips_stdout(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["git", "status"], 0, stdout="  clean\n", stderr="",
        )

        self.assertEqual(git("status", quiet=True), "clean")
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.PIPE)
        self.assertIs(run.call_args.kwargs["stdout"], subprocess.PIPE)

    @patch("patch_catalog.subprocess.run")
    def test_quiet_failure_includes_stderr_and_stdout(self, run):
        run.side_effect = subprocess.CalledProcessError(
            1, ["git", "am", "change.patch"],
            output="stdout detail\n", stderr="stderr detail\n",
        )

        with self.assertRaises(CatalogError) as raised:
            git("am", "change.patch", quiet=True)

        self.assertEqual(
            str(raised.exception),
            "git am change.patch failed\nstderr:\nstderr detail\nstdout:\nstdout detail",
        )
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.PIPE)

    @patch("patch_catalog.subprocess.run")
    def test_quiet_failure_without_output_keeps_fallback(self, run):
        run.side_effect = subprocess.CalledProcessError(
            1, ["git", "status"], output="  \n", stderr="",
        )

        with self.assertRaises(CatalogError) as raised:
            git("status", quiet=True)

        self.assertEqual(str(raised.exception), "git status failed")

    @patch("patch_catalog.subprocess.run")
    def test_nonquiet_inherits_stderr_and_strips_stdout(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["git", "status"], 0, stdout="  clean\n", stderr=None,
        )

        self.assertEqual(git("status"), "clean")
        self.assertIsNone(run.call_args.kwargs["stderr"])

    def test_quiet_git_am_conflict_reports_git_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / "base"
            patch_repo = root / "patch"
            target_repo = root / "target"
            base.mkdir()
            self._run_git(base, "init", "-q")
            self._run_git(base, "config", "user.name", "Test User")
            self._run_git(base, "config", "user.email", "test@example.invalid")
            (base / "conflict.txt").write_text("base\n", encoding="utf-8")
            self._run_git(base, "add", "conflict.txt")
            self._run_git(base, "commit", "-q", "-m", "base")
            self._run_git(root, "clone", "-q", str(base), str(patch_repo))
            self._run_git(root, "clone", "-q", str(base), str(target_repo))
            for repository in (patch_repo, target_repo):
                self._run_git(repository, "config", "user.name", "Test User")
                self._run_git(repository, "config", "user.email", "test@example.invalid")

            (patch_repo / "conflict.txt").write_text("patch change\n", encoding="utf-8")
            self._run_git(patch_repo, "commit", "-qam", "patch change")
            patch_text = self._run_git(patch_repo, "format-patch", "--stdout", "-1")
            patch_file = root / "change.patch"
            patch_file.write_text(patch_text, encoding="utf-8")

            (target_repo / "conflict.txt").write_text("target change\n", encoding="utf-8")
            self._run_git(target_repo, "commit", "-qam", "target change")

            with self.assertRaises(CatalogError) as raised:
                git("am", str(patch_file), cwd=target_repo, quiet=True)

            message = str(raised.exception)
            self.assertIn(f"git am {patch_file} failed", message)
            self.assertIn("stderr:\n", message)
            self.assertTrue(any(word in message.lower() for word in ("conflict", "patch failed")), message)

    @staticmethod
    def _run_git(cwd: Path, *arguments: str) -> str:
        result = subprocess.run(
            ["git", *arguments], cwd=cwd, check=True,
            capture_output=True, text=True,
        )
        return result.stdout.strip()


if __name__ == "__main__":
    unittest.main()
