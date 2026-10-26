"""Tests for Git failure diagnostics in patch catalog workflows."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from .apply_patches import apply
from .isolate_config import isolated_environment
from .patch_catalog import CatalogError, Patch, git


class GitDiagnosticsTests(unittest.TestCase):
    def test_apply_skips_for_commands_with_and_without_delimiter(self):
        from .validate_patch_commands import append_skips

        names = ["one::test_a", "two::test_b"]
        self.assertEqual(
            append_skips("cargo test --package one", names),
            "cargo test --package one -- --skip one::test_a --skip two::test_b",
        )
        self.assertEqual(
            append_skips("cargo test --package one -- --nocapture", names),
            "cargo test --package one -- --nocapture --skip one::test_a --skip two::test_b",
        )
        self.assertEqual(append_skips("cargo test --package one", []), "cargo test --package one")

    def test_failed_test_lines_are_sorted_unique(self):
        from .validate_patch_commands import parse_failed_tests

        output = "test z::last ... FAILED\ntest a::first ... FAILED\ntest z::last ... FAILED\ntest ignored ... ok\n"
        self.assertEqual(parse_failed_tests(output), ["a::first", "z::last"])

    def test_legacy_exclusions_load_without_validation_key_and_round_trip(self):
        import json
        from .run_nextest import config_path, load_names, load_validation_names
        from .validate_patch_commands import write_validation_names

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = "abc123"
            path = config_path(root, base)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({
                "nextest_version": "0.9.143", "nextest_pin": "0.9.143",
                "base_commit": base, "failed_tests": ["compat::fails"],
            }), encoding="utf-8")
            self.assertEqual(load_names(root, base), ["compat::fails"])
            self.assertEqual(load_validation_names(root, base), [])
            write_validation_names(root, base, ["z::fail", "a::fail", "z::fail"])
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["failed_tests"], ["compat::fails"])
            self.assertEqual(payload["failed_validation_tests"], ["a::fail", "z::fail"])
            self.assertEqual(load_names(root, base), ["compat::fails"])
            self.assertEqual(load_validation_names(root, base), ["a::fail", "z::fail"])

    @patch("jcode_catalog.apply_patches.subprocess.run")
    @patch("jcode_catalog.apply_patches.patches")
    def test_apply_uses_three_way_git_am(self, catalog_patches, run):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            worktree = root / "worktree"
            worktree.mkdir()
            (worktree / ".git").write_text("gitdir: test\n", encoding="utf-8")
            patch_path = root / "change.patch"
            catalog_patches.return_value = {
                patch_path.name: Patch(patch_path, (), "test patch", "personal-feature"),
            }

            apply(root, worktree, None)

        self.assertIn("--3way", run.call_args.args[0])

    @patch("jcode_catalog.patch_catalog.subprocess.run")
    def test_quiet_success_captures_stderr_and_strips_stdout(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["git", "status"], 0, stdout="  clean\n", stderr="",
        )

        self.assertEqual(git("status", quiet=True), "clean")
        self.assertIs(run.call_args.kwargs["stderr"], subprocess.PIPE)
        self.assertIs(run.call_args.kwargs["stdout"], subprocess.PIPE)

    @patch("jcode_catalog.patch_catalog.subprocess.run")
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

    @patch("jcode_catalog.patch_catalog.subprocess.run")
    def test_quiet_failure_without_output_keeps_fallback(self, run):
        run.side_effect = subprocess.CalledProcessError(
            1, ["git", "status"], output="  \n", stderr="",
        )

        with self.assertRaises(CatalogError) as raised:
            git("status", quiet=True)

        self.assertEqual(str(raised.exception), "git status failed")

    @patch("jcode_catalog.patch_catalog.subprocess.run")
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

    def test_apply_conflict_resumes_remaining_patches_with_am_continue(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_repo = root / "source"
            target_repo = root / "worktree"
            catalog_root = root / "catalog"
            source_repo.mkdir()
            catalog_root.mkdir()
            (catalog_root / "patches").mkdir()
            self._run_git(source_repo, "init", "-q")
            self._configure_repository(source_repo)
            (source_repo / "conflict.txt").write_text("base\n", encoding="utf-8")
            self._run_git(source_repo, "add", "conflict.txt")
            self._run_git(source_repo, "commit", "-q", "-m", "base")
            self._run_git(root, "clone", "-q", str(source_repo), str(target_repo))
            self._configure_repository(target_repo)

            (target_repo / "conflict.txt").write_text("worktree change\n", encoding="utf-8")
            self._run_git(target_repo, "commit", "-qam", "worktree change")

            patch_definitions = [
                (
                    "1001-personal-feature-first.patch",
                    "feat: first patch",
                    "first.txt",
                    "first patch\n",
                ),
                (
                    "1002-personal-feature-second.patch",
                    "feat: second patch",
                    "conflict.txt",
                    "second patch\n",
                ),
                (
                    "1003-personal-feature-third.patch",
                    "feat: third patch",
                    "third.txt",
                    "third patch\n",
                ),
            ]
            for name, subject, filename, contents in patch_definitions:
                (source_repo / filename).write_text(contents, encoding="utf-8")
                self._run_git(source_repo, "add", filename)
                commit_message = (
                    f"{subject}\n\n"
                    f"X-Jcode-Patch-Intent: {subject}\n"
                    "X-Jcode-Patch-Kind: personal-feature\n"
                    "X-Jcode-Patch-Depends-On: none\n"
                    f"X-Jcode-Patch-File: {name}\n\n"
                    "Patch intent:\nApply this test change.\n\n"
                    "Why it exists:\nExercise applying a patch stack.\n\n"
                    "Upstream integration points:\nGit mail patches.\n\n"
                    "Update guidance:\nKeep the stack ordered.\n\n"
                    "Validation:\necho test"
                )
                self._run_git(source_repo, "commit", "-q", "-m", commit_message)
                patch_text = self._run_git(source_repo, "format-patch", "--stdout", "-1")
                patch_path = catalog_root / "patches" / name
                patch_path.write_text(patch_text + "\n", encoding="utf-8")

            self._run_git(catalog_root, "init", "-q")
            self._configure_repository(catalog_root)
            self._run_git(catalog_root, "add", "patches")
            self._run_git(catalog_root, "commit", "-q", "-m", "catalog patches")

            with self.assertRaises(CatalogError) as raised:
                apply(catalog_root, target_repo, None)

            self.assertIn(
                f"Resolve conflicts, then run: git -C {target_repo} am --continue "
                "(applies the remaining patches)",
                str(raised.exception),
            )
            (target_repo / "conflict.txt").write_text("second patch\n", encoding="utf-8")
            self._run_git(target_repo, "add", "conflict.txt")
            self._run_git(target_repo, "am", "--continue")

            self.assertEqual(self._run_git(target_repo, "rev-list", "--count", "HEAD"), "5")
            self.assertEqual((target_repo / "first.txt").read_text(encoding="utf-8"), "first patch\n")
            self.assertEqual((target_repo / "conflict.txt").read_text(encoding="utf-8"), "second patch\n")
            self.assertEqual((target_repo / "third.txt").read_text(encoding="utf-8"), "third patch\n")

    @classmethod
    def _configure_repository(cls, repository: Path) -> None:
        cls._run_git(repository, "config", "user.name", "Test User")
        cls._run_git(repository, "config", "user.email", "test@example.invalid")
        cls._run_git(repository, "config", "core.editor", "true")

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
