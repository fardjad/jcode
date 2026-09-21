#!/usr/bin/env python3
"""Contract tests for the jcode delegation guard transformer."""

import json
import os
import pathlib
import subprocess
import sys
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

    def test_above_threshold_nudges_with_delegation_token(self):
        result = self.run_plugin(
            {
                "output_bytes": 9000,
                "tool_result_file": None,
                "delegation_token": "dt1.token.signature",
            },
            **self.coordinator_environment(),
        )

        self.assertEqual(result.returncode, 0)
        nudge = json.loads(result.stdout)["tool_result"]
        self.assertIn("[delegation-token: dt1.token.signature]", nudge)

    def test_above_threshold_nudge_excludes_missing_delegation_token(self):
        result = self.run_plugin(
            {"output_bytes": 9000, "tool_result_file": None},
            **self.coordinator_environment(),
        )

        self.assertEqual(result.returncode, 0)
        nudge = json.loads(result.stdout)["tool_result"]
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
