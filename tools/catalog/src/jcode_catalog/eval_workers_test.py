"""Preflight-before-build contracts for eval workers."""

import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import eval_workers
from .patch_catalog import CatalogError


def _completed(returncode: int) -> SimpleNamespace:
    return SimpleNamespace(returncode=returncode)


@contextmanager
def _fake_lock(path, reason):
    yield object()


class EvalWorkersTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="eval-workers-test-"))
        python = self.root / "tools" / "evaluator" / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text("", encoding="utf-8")

    def _main_with_args(self, argv):
        original = sys.argv
        sys.argv = argv
        try:
            return eval_workers.main()
        finally:
            sys.argv = original

    def test_preflight_failure_returns_before_any_build(self):
        with (
            patch.object(eval_workers, "repository_root", return_value=self.root),
            patch.object(eval_workers, "evaluation_directory", return_value=self.root),
            patch.object(eval_workers, "exclusive_lock", _fake_lock),
            patch.object(eval_workers, "prepare") as prepare,
            patch.object(
                eval_workers.subprocess, "run", return_value=_completed(1)
            ) as run,
        ):
            code = self._main_with_args(["eval-workers"])
        self.assertEqual(code, 1)
        prepare.assert_not_called()
        self.assertEqual(len(run.call_args_list), 1)
        command = run.call_args.args[0]
        self.assertIn("validate-settings", command)
        self.assertTrue(str(command[0]).endswith("python"))

    def test_preflight_validates_settings_before_the_native_build(self):
        order = []
        with (
            patch.object(eval_workers, "repository_root", return_value=self.root),
            patch.object(eval_workers, "evaluation_directory", return_value=self.root),
            patch.object(eval_workers, "exclusive_lock", _fake_lock),
            patch.object(
                eval_workers,
                "prepare",
                side_effect=lambda *a, **k: (
                    order.append("prepare")
                    or {
                        "action": "reuse",
                        "fingerprint": "test",
                        "binary_paths": {
                            "jcode": "j",
                            "jcode-harness-api-bridge": "b",
                        },
                    }
                ),
            ),
            patch.object(
                eval_workers.subprocess,
                "run",
                side_effect=lambda command, **k: (
                    order.append(
                        "preflight" if "validate-settings" in command else "run"
                    )
                    or _completed(0)
                ),
            ) as run,
        ):
            code = self._main_with_args(["eval-workers"])
        self.assertEqual(code, 0)
        self.assertEqual(order[:2], ["preflight", "prepare"])
        self.assertNotIn("run", order[:1])
        validate_calls = [
            call for call in run.call_args_list if "validate-settings" in call.args[0]
        ]
        self.assertEqual(len(validate_calls), 1)
        command = validate_calls[0].args[0]
        self.assertEqual(
            command[command.index("--config") + 1],
            str(self.root / "customizations" / "workers" / "evals" / "config.toml"),
        )
        self.assertEqual(
            command[command.index("--scenarios") + 1],
            str(
                self.root / "customizations" / "workers" / "evals" / "scenarios"
            ),
        )

    def test_selected_scenario_is_passed_to_the_preflight(self):
        with (
            patch.object(eval_workers, "repository_root", return_value=self.root),
            patch.object(eval_workers, "evaluation_directory", return_value=self.root),
            patch.object(eval_workers, "exclusive_lock", _fake_lock),
            patch.object(eval_workers, "prepare"),
            patch.object(
                eval_workers.subprocess, "run", return_value=_completed(1)
            ) as run,
        ):
            code = self._main_with_args(
                ["eval-workers", "--scenario", "stable-task-order"]
            )
        self.assertEqual(code, 1)
        command = run.call_args.args[0]
        self.assertEqual(command[-2:], ["--scenario", "stable-task-order"])

    def test_preflight_reports_missing_evaluator_environment(self):
        with (
            patch.object(eval_workers, "repository_root", return_value=self.root),
            patch.object(
                eval_workers, "_evaluator_python", return_value=self.root / "missing"
            ),
            self.assertRaises(CatalogError),
        ):
            eval_workers.preflight_settings(self.root)


if __name__ == "__main__":
    unittest.main()
