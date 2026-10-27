#!/usr/bin/env python3
"""Contract tests for the RTK jcode pre-tool-transform adapter."""

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

PLUGIN = pathlib.Path(__file__).with_name("rtk-transform")


class RtkTransformTests(unittest.TestCase):
    def run_plugin(self, payload, *, tool_name="bash", rtk_script=None):
        env = os.environ.copy()
        env["JCODE_HOOK_TOOL_NAME"] = tool_name
        with tempfile.TemporaryDirectory() as temp_dir:
            if rtk_script is not None:
                rtk = pathlib.Path(temp_dir) / "rtk"
                rtk.write_text(rtk_script)
                rtk.chmod(0o755)
                env["PATH"] = f"{temp_dir}{os.pathsep}{env['PATH']}"
            return subprocess.run(
                [sys.executable, str(PLUGIN)],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )

    def test_replaces_bash_command_from_nonzero_rtk_exit(self):
        """RTK/OpenCode accepts rewrite from `.nothrow()` on nonzero exit."""
        result = self.run_plugin(
            {"command": "git status", "timeout": 30},
            rtk_script="#!/bin/sh\necho 'rtk git status'\nexit 17\n",
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            json.loads(result.stdout),
            {"command": "rtk git status", "timeout": 30},
        )

    def test_keeps_original_input_when_rtk_has_no_rewrite(self):
        result = self.run_plugin(
            {"command": "git status"},
            rtk_script="#!/bin/sh\necho 'git status'\nexit 0\n",
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_is_a_no_op_for_non_bash_tools(self):
        result = self.run_plugin(
            {"command": "git status"},
            tool_name="read",
            rtk_script="#!/bin/sh\necho should-not-run >&2\nexit 1\n",
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")

    def test_invalid_input_is_a_no_op(self):
        env = os.environ.copy()
        env["JCODE_HOOK_TOOL_NAME"] = "bash"
        result = subprocess.run(
            [sys.executable, str(PLUGIN)],
            input="not-json",
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
