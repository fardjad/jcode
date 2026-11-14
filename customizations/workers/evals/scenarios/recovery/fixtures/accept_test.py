import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class Acceptance(unittest.TestCase):
    def test_exact_results_and_checkpoint(self):
        self.assertEqual(Path("results.txt").read_text().splitlines(), ["AMBER", "BIRCH", "CEDAR"])
        self.assertEqual(json.loads(Path("checkpoint.json").read_text()), {"completed": 3})

    def test_interrupt_and_resume_fresh_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            home = Path(temp) / "home"
            home.mkdir()
            shutil.copytree(Path.cwd(), workspace)
            (workspace / "input.txt").write_text("maple\n\nwillow\nash\npoplar\n")
            for name in ("results.txt", "checkpoint.json"):
                (workspace / name).unlink(missing_ok=True)
            env = {**os.environ, "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1"}
            def run():
                return subprocess.run([sys.executable, "batch.py"], cwd=workspace, env=env, capture_output=True, text=True, timeout=10, check=False)
            first = run()
            self.assertNotEqual(first.returncode, 0)
            self.assertIn("batch interrupted after checkpoint", first.stderr)
            self.assertEqual((workspace / "results.txt").read_text().splitlines(), ["MAPLE"])
            self.assertEqual(json.loads((workspace / "checkpoint.json").read_text()), {"completed": 1})
            for _ in range(2):
                result = run()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual((workspace / "results.txt").read_text().splitlines(), ["MAPLE", "WILLOW", "ASH", "POPLAR"])
                self.assertEqual(json.loads((workspace / "checkpoint.json").read_text()), {"completed": 4})
