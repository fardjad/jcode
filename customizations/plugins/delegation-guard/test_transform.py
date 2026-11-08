#!/usr/bin/env python3
"""Contract tests for the jcode delegation guard transformer."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

PLUGIN = pathlib.Path(__file__).with_name("delegation-guard-transform")


class DelegationGuardTransformTests(unittest.TestCase):
    def run_plugin(self, payload, **environment):
        env = os.environ.copy()
        env.update(environment)
        return subprocess.run(
            [sys.executable, str(PLUGIN)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def coordinator_environment(self, **overrides):
        environment = {
            "JCODE_HOOK_SWARM_ENABLED": "1",
            "JCODE_HOOK_PROCESS_ROLE": "coordinator",
        }
        environment.update(overrides)
        return environment

    def worker_environment(self, blueprint_id=None, **overrides):
        environment = {
            "JCODE_HOOK_SWARM_ENABLED": "1",
            "JCODE_HOOK_PROCESS_ROLE": "worker",
        }
        if blueprint_id is not None:
            environment["JCODE_HOOK_WORKER_BLUEPRINT_ID"] = blueprint_id
        environment.update(overrides)
        return environment

    def test_swarm_disabled_preserves_result(self):
        result = self.run_plugin(
            {"output_bytes": 9000, "tool_result_file": "/payload.txt"},
            JCODE_HOOK_SWARM_ENABLED="0",
            JCODE_HOOK_PROCESS_ROLE="coordinator",
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_non_coordinator_preserves_result(self):
        result = self.run_plugin(
            {"output_bytes": 9000, "tool_result_file": "/payload.txt"},
            JCODE_HOOK_SWARM_ENABLED="1",
            JCODE_HOOK_PROCESS_ROLE="worker",
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_worker_blueprint_selection_is_disabled_by_default(self):
        result = self.run_plugin(
            {"output_bytes": 9000, "swarm": {"blueprint_id": "expert"}},
            **self.worker_environment(),
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_selected_expert_worker_uses_swarm_blueprint_id(self):
        result = self.run_plugin(
            {
                "output_bytes": 9000,
                "tool_result_file": "/runtime/expert-result.txt",
                "swarm": {"blueprint_id": "expert"},
            },
            **self.worker_environment(
                JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS="advanced,expert"
            ),
        )

        self.assertEqual(result.returncode, 0)
        nudge = json.loads(result.stdout)["tool_result"]
        self.assertIn("an allowed child worker", nudge)
        self.assertNotIn("swarm_routine", nudge)
        self.assertIn("bounded, read-only extraction", nudge)
        self.assertIn("/runtime/expert-result.txt", nudge)

    def test_selected_advanced_worker_uses_blueprint_environment(self):
        result = self.run_plugin(
            {"output_bytes": 9000, "tool_result_file": "/runtime/advanced.txt"},
            **self.worker_environment(
                "advanced",
                JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS=" advanced, expert ",
            ),
        )

        self.assertEqual(result.returncode, 0)
        nudge = json.loads(result.stdout)["tool_result"]
        self.assertIn("an allowed child worker", nudge)
        self.assertNotIn("swarm_routine", nudge)

    def test_unlisted_blueprint_is_never_selected(self):
        result = self.run_plugin(
            {"output_bytes": 9000, "swarm": {"blueprint_id": "unlisted"}},
            **self.worker_environment(
                JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS="advanced,expert"
            ),
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_any_listed_blueprint_is_selected(self):
        result = self.run_plugin(
            {"output_bytes": 9000, "swarm": {"blueprint_id": "planner"}},
            **self.worker_environment(
                JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS="planner"
            ),
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("allowed child worker", result.stdout)

    def test_selected_worker_preserves_result_when_swarm_is_disabled(self):
        result = self.run_plugin(
            {"output_bytes": 9000, "swarm": {"blueprint_id": "expert"}},
            **self.worker_environment(
                JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS="expert",
                JCODE_HOOK_SWARM_ENABLED="0",
            ),
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_selected_worker_below_threshold_preserves_result(self):
        result = self.run_plugin(
            {"output_bytes": 8192, "swarm": {"blueprint_id": "expert"}},
            **self.worker_environment(
                JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS="expert"
            ),
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_selected_worker_without_result_file_reports_blocker(self):
        result = self.run_plugin(
            {"output_bytes": 9000, "tool_result_file": None},
            **self.worker_environment(
                "expert",
                JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS="expert",
            ),
        )

        self.assertEqual(result.returncode, 0)
        nudge = json.loads(result.stdout)["tool_result"]
        self.assertIn("report that blocker to the parent", nudge)
        self.assertIn("instead of retrying blindly", nudge)
        self.assertNotIn("available at:", nudge)

    def test_worker_tail_is_bounded_and_coordinator_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "output.txt"
            path.write_bytes(b"discard partial\nold\nnew\xff\nlast\n")
            payload = {"output_bytes": 9000, "tool_result_file": str(path)}
            environment = self.worker_environment(
                "expert",
                JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS="expert",
                JCODE_DELEGATION_GUARD_TAIL_BYTES="22",
                JCODE_DELEGATION_GUARD_TAIL_LINES="2",
            )
            result = self.run_plugin(payload, **environment)
            self.assertEqual(result.returncode, 0)
            self.assertTrue(json.loads(result.stdout)["tool_result"].endswith(
                "\nLast lines of output:\nnew\ufffd\nlast"
            ))
            environment["JCODE_DELEGATION_GUARD_TAIL_LINES"] = "20"
            nudge = json.loads(self.run_plugin(payload, **environment).stdout)["tool_result"]
            self.assertTrue(nudge.endswith("\nLast lines of output:\nold\nnew\ufffd\nlast"))
            # An invalid limit falls back to the default; the full file fits.
            environment["JCODE_DELEGATION_GUARD_TAIL_BYTES"] = "invalid"
            environment["JCODE_DELEGATION_GUARD_TAIL_LINES"] = "-1"
            nudge = json.loads(self.run_plugin(payload, **environment).stdout)["tool_result"]
            self.assertIn("Last lines of output:\ndiscard partial\nold\nnew\ufffd\nlast", nudge)
            coordinator = self.run_plugin(payload, **self.coordinator_environment())
            self.assertNotIn("Last lines of output:", json.loads(coordinator.stdout)["tool_result"])

    def test_worker_tail_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "output.txt"
            path.write_text("visible tail\n", encoding="utf-8")
            payload = {"output_bytes": 9000, "tool_result_file": str(path)}
            for variable in ("JCODE_DELEGATION_GUARD_TAIL_BYTES", "JCODE_DELEGATION_GUARD_TAIL_LINES"):
                with self.subTest(variable=variable):
                    result = self.run_plugin(payload, **self.worker_environment(
                        "expert", JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS="expert",
                        **{variable: "0"},
                    ))
                    self.assertEqual(result.returncode, 0)
                    self.assertNotIn("Last lines of output:", json.loads(result.stdout)["tool_result"])

    def test_worker_tail_missing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "missing.txt"
            for candidate in (path, pathlib.Path(directory)):
                with self.subTest(path=candidate):
                    result = self.run_plugin(
                        {"output_bytes": 9000, "tool_result_file": str(candidate)},
                        **self.worker_environment("expert", JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS="expert"),
                    )
                    self.assertEqual(result.returncode, 0)
                    nudge = json.loads(result.stdout)["tool_result"]
                    self.assertIn(f"Full output is available at: {candidate}", nudge)
                    self.assertNotIn("Last lines of output:", nudge)

    def test_below_threshold_preserves_result(self):
        result = self.run_plugin(
            {"output_bytes": 8192, "tool_result_file": "/payload.txt"},
            **self.coordinator_environment(),
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_above_threshold_nudges_with_result_file(self):
        result = self.run_plugin(
            {
                "output_bytes": 8193,
                "tool_result_file": "/runtime/tool-result.txt",
                "tool_result": "original content must not appear",
            },
            **self.coordinator_environment(),
        )

        self.assertEqual(result.returncode, 0)
        nudge = json.loads(result.stdout)["tool_result"]
        self.assertIn("8193 bytes", nudge)
        self.assertIn("/runtime/tool-result.txt", nudge)
        self.assertNotIn("original content must not appear", nudge)
        self.assertNotIn("swarm_bash-runner", nudge)

    def test_above_threshold_nudges_without_result_file(self):
        result = self.run_plugin(
            {"output_bytes": 16384, "tool_result_file": None},
            **self.coordinator_environment(),
        )

        self.assertEqual(result.returncode, 0)
        nudge = json.loads(result.stdout)["tool_result"]
        self.assertIn("16384 bytes", nudge)
        self.assertNotIn("available at:", nudge)

    def test_above_threshold_nudge_contains_no_token_marker(self):
        result = self.run_plugin(
            {"output_bytes": 9000, "tool_result_file": None},
            **self.coordinator_environment(),
        )

        self.assertEqual(result.returncode, 0)
        nudge = json.loads(result.stdout)["tool_result"]
        self.assertIn("9000 bytes", nudge)
        self.assertNotIn("delegation-token:", nudge)

    def test_invalid_threshold_uses_default(self):
        result = self.run_plugin(
            {"output_bytes": 8193, "tool_result_file": None},
            **self.coordinator_environment(JCODE_DELEGATION_GUARD_THRESHOLD="invalid"),
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("8193 bytes", json.loads(result.stdout)["tool_result"])


if __name__ == "__main__":
    unittest.main()
