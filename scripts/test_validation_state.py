"""Regression tests for dependency identities, review freshness and process locks."""
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from check_patch_reviews import evidence_errors
from patch_catalog import CatalogError
from run_all_patch_tests import load_results, run_all, save_results, should_skip
from validation_state import closure_identity, exclusive_lock, source_cache, validation_identity


def catalog(root):
    (root / "patches").mkdir()
    for name, dependency in (("0001-a.patch", "none"), ("0002-b.patch", "0001-a.patch")):
        (root / "patches" / name).write_text(
            f"X-Jcode-Patch-Depends-On: {dependency}\nX-Jcode-Patch-Intent: feature\n"
            "X-Jcode-Patch-Kind: upstream-candidate\n")
    (root / "VERSION.txt").write_text("v1.0.0\n")


class ValidationStateTests(unittest.TestCase):
    def test_dependency_changes_invalidate_unchanged_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog(root)
            before = closure_identity(root, "0002-b.patch", "base")
            (root / "patches/0001-a.patch").write_text(
                (root / "patches/0001-a.patch").read_text() + "changed\n")
            after = closure_identity(root, "0002-b.patch", "base")
            self.assertNotEqual(before, after)
            self.assertEqual(before["patches"]["0002-b.patch"], after["patches"]["0002-b.patch"])

    def test_workflow_changes_invalidate_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog(root)
            (root / "justfile").write_text("first")
            before = validation_identity(root, "0001-a.patch", "base")
            (root / "justfile").write_text("second")
            self.assertNotEqual(before, validation_identity(root, "0001-a.patch", "base"))

    def test_lock_blocks_second_process_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.lock"
            script = "from pathlib import Path; from validation_state import exclusive_lock; import sys\nwith exclusive_lock(Path(sys.argv[1]), 'test'): pass"
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).parent.resolve())
            with exclusive_lock(path, "parent"):
                result = subprocess.run([sys.executable, "-c", script, str(path)],
                                        env=environment, capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("already running", result.stderr)
            with exclusive_lock(path, "next"):
                pass

    @unittest.skipIf(os.name == "nt", "Unix descriptor inheritance")
    def test_child_keeps_lock_after_parent_context_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.lock"
            with exclusive_lock(path, "parent") as handle:
                child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.3)"],
                                         pass_fds=(handle.fileno(),))
            try:
                with self.assertRaises(CatalogError):
                    with exclusive_lock(path, "second"):
                        pass
            finally:
                child.wait(timeout=5)
            with exclusive_lock(path, "released"):
                pass

    def test_clean_source_trees_have_distinct_cache_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("validation_state.git", side_effect=["", "tree-a", "", "tree-b", "dirty"]):
                a = source_cache(root, root, "tests")
                b = source_cache(root, root, "tests")
                self.assertNotEqual(a, b)
                with self.assertRaises(CatalogError):
                    source_cache(root, root, "tests")

    def test_review_freshness_and_unresolved_claims(self):
        identity = {"base": "base", "patches": {"a": "blob"}}
        record = {"identity": identity, "commit": "commit", "status": "reviewed",
                  "reviewer": "reviewer", "claims": [{"claim": "intent", "code": "path:symbol",
                  "tests": "test_body", "finding": "checked", "outcome": "supported"}],
                  "scope_review": "all claims checked", "broader_changes": "none", "residuals": "none"}
        self.assertEqual(evidence_errors(record, identity, "commit"), [])
        self.assertTrue(evidence_errors(record, {"base": "other"}, "commit"))
        self.assertTrue(evidence_errors(record, identity, "new-commit"))
        changed = deepcopy(record)
        changed["claims"][0]["outcome"] = "accepted-gap"
        self.assertTrue(evidence_errors(changed, identity, "commit"))
        changed["claims"][0]["user_acceptance"] = "user approved missing behavior"
        self.assertEqual(evidence_errors(changed, identity, "commit"), [])

    def test_runner_records_running_pid_streamed_log_and_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog(root)
            record_path = root / ".tools/patch-tests/base/results.json"
            class Child:
                pid = 12345
                def __init__(self, command, **kwargs):
                    state = load_results(record_path)["0001-a.patch"]
                    self.assert_running = state["status"] == "running" and state["exit_code"] is None
                    kwargs["stdout"].write("live output\n")
                    kwargs["stdout"].flush()
                def wait(self):
                    if not self.assert_running:
                        raise AssertionError("running status missing before launch")
                    state = load_results(record_path)["0001-a.patch"]
                    if state["pid"] != self.pid:
                        raise AssertionError("child PID not persisted")
                    return 0
            with patch("run_all_patch_tests.resolve_base", return_value="base"), \
                 patch("run_all_patch_tests.validation_identity", return_value="identity"), \
                 patch("run_all_patch_tests.patch_blob", return_value="blob"), \
                 patch("run_all_patch_tests.subprocess.Popen", Child):
                self.assertEqual(run_all(root, ["0001-a.patch"]), 0)
            state = load_results(record_path)["0001-a.patch"]
            self.assertEqual(state["status"], "pass")
            self.assertEqual(state["exit_code"], 0)
            self.assertIn("live output", Path(state["log"]).read_text())

    def test_inputs_changed_during_run_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog(root)
            with patch("run_all_patch_tests.resolve_base", return_value="base"), \
                 patch("run_all_patch_tests.validation_identity", side_effect=["old", "old", "new", "new"]), \
                 patch("run_all_patch_tests.patch_blob", return_value="blob"), \
                 patch("run_all_patch_tests.subprocess.Popen") as launch:
                launch.return_value.pid = 123
                launch.return_value.wait.return_value = 0
                self.assertEqual(run_all(root, ["0001-a.patch"]), 1)
            state = load_results(root / ".tools/patch-tests/base/results.json")["0001-a.patch"]
            self.assertEqual(state["status"], "stale")


if __name__ == "__main__":
    unittest.main()
