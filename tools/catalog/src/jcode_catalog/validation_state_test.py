"""Regression tests for dependency identities, process locks and gate results."""
import os
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from .patch_catalog import CatalogError
from .run_all_patch_tests import load_results, run_all
from .validation_state import closure_identity, exclusive_lock, source_cache, validation_identity


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

    def test_tooling_changes_preserve_pass_but_nextest_config_invalidates_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog(root)
            (root / "justfile").write_text("first")
            before = validation_identity(root, "0001-a.patch", "base")
            (root / "justfile").write_text("second")
            self.assertEqual(before, validation_identity(root, "0001-a.patch", "base"))
            (root / "nextest.toml").write_text("changed profile")
            self.assertNotEqual(before, validation_identity(root, "0001-a.patch", "base"))

    def test_lock_blocks_second_process_and_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.lock"
            script = "from pathlib import Path; from jcode_catalog.validation_state import exclusive_lock; import sys\nwith exclusive_lock(Path(sys.argv[1]), 'test'): pass"
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
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

    def test_clean_sources_share_purpose_cache_but_dirty_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("jcode_catalog.validation_state.git", side_effect=["", "", "dirty"]):
                a = source_cache(root, root, "tests")
                b = source_cache(root, root, "tests")
                self.assertEqual(a, b)
                with self.assertRaises(CatalogError):
                    source_cache(root, root, "tests")

    def test_runner_records_results_and_reuses_only_current_full_stack_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog(root)
            record_path = root / ".tools/patch-tests/base/results.json"
            class Child:
                pid = 12345
                def __init__(self, command, **kwargs):
                    self.name = command[-3]
                    state = load_results(record_path)[self.name]
                    self.assert_running = state["status"] == "running" and state["exit_code"] is None
                    kwargs["stdout"].write("live output\n")
                    kwargs["stdout"].flush()
                def wait(self):
                    if not self.assert_running:
                        raise AssertionError("running status missing before launch")
                    state = load_results(record_path)[self.name]
                    if state["pid"] != self.pid:
                        raise AssertionError("child PID not persisted")
                    return 0
            with patch("jcode_catalog.run_all_patch_tests.resolve_base", return_value="base"), \
                 patch("jcode_catalog.run_all_patch_tests.validation_identity", return_value="identity") as identity, \
                 patch("jcode_catalog.run_all_patch_tests.patch_blob", return_value="blob"), \
                 patch("jcode_catalog.run_all_patch_tests.validate_catalog"), \
                 patch("jcode_catalog.run_all_patch_tests.execute") as gate, \
                 patch("jcode_catalog.run_all_patch_tests.subprocess.Popen", side_effect=Child) as launch, \
                 patch("sys.stdout", new_callable=io.StringIO) as output:
                self.assertEqual(run_all(root, ["0001-a.patch"]), 0)
                gate.assert_not_called()
                self.assertIn("full-stack gate was not run", output.getvalue())
                self.assertNotIn("__full_stack__", load_results(record_path))
                self.assertEqual(run_all(root), 0)
                gate.assert_called_once_with(root, "gate", preflight=False)
                self.assertEqual(load_results(record_path)["__full_stack__"]["status"], "pass")
                self.assertEqual(run_all(root), 0)
                self.assertEqual(gate.call_count, 1)
                self.assertEqual(run_all(root, force=True), 0)
                self.assertEqual(gate.call_count, 2)
                identity.return_value = "changed"
                self.assertEqual(run_all(root), 0)
                self.assertEqual(gate.call_count, 3)
                children = launch.call_count
                gate.side_effect = CatalogError("gate failed")
                with self.assertRaisesRegex(CatalogError, "gate failed"):
                    run_all(root, force=True)
                self.assertEqual(launch.call_count, children)
                self.assertEqual(load_results(record_path)["__full_stack__"]["status"], "fail")
                gate.side_effect = None
                self.assertEqual(run_all(root), 0)
                self.assertEqual(gate.call_count, 5)
            state = load_results(record_path)["0001-a.patch"]
            self.assertEqual(state["status"], "pass")
            self.assertEqual(state["exit_code"], 0)
            self.assertIn("live output", Path(state["log"]).read_text())
            self.assertEqual(state["command"][-4:], [str(root), "0001-a.patch", "changed", "0"])
            self.assertIn("batch_child", state["command"][2])
            self.assertNotEqual(state["command"][0], "just")

    def test_inputs_changed_during_run_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog(root)
            with patch("jcode_catalog.run_all_patch_tests.resolve_base", return_value="base"), \
                 patch("jcode_catalog.run_all_patch_tests.validation_identity", side_effect=["old", "old", "old", "new", "new"]), \
                 patch("jcode_catalog.run_all_patch_tests.patch_blob", return_value="blob"), \
                 patch("jcode_catalog.run_all_patch_tests.validate_catalog"), \
                 patch("jcode_catalog.run_all_patch_tests.execute"), \
                 patch("jcode_catalog.run_all_patch_tests.subprocess.Popen") as launch:
                launch.return_value.pid = 123
                launch.return_value.wait.return_value = 0
                self.assertEqual(run_all(root, ["0001-a.patch"]), 1)
            state = load_results(root / ".tools/patch-tests/base/results.json")["0001-a.patch"]
            self.assertEqual(state["status"], "stale")


if __name__ == "__main__":
    unittest.main()
