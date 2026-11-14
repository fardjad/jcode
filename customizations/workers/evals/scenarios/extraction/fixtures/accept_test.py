import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class Acceptance(unittest.TestCase):
    def test_summary(self):
        self.assertEqual(json.loads(Path("summary.json").read_text()), {
            "ant": "red", "bee": "gold", "cat": "black", "dog": "brown",
            "eel": "blue", "fox": "orange", "hen": "white", "ibis": "pink",
        })

    def test_program_with_fresh_facts(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            shutil.copytree(Path.cwd(), workspace)
            shutil.rmtree(workspace / "facts")
            (workspace / "facts").mkdir()
            (workspace / "facts" / "new.txt").write_text("lynx: gray\nmoth: silver\nlynx: gray\n")
            (workspace / "facts" / "more.txt").write_text("wasp: yellow\n")
            (workspace / "summary.json").unlink(missing_ok=True)
            run = subprocess.run([sys.executable, "aggregate.py"], cwd=workspace, capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads((workspace / "summary.json").read_text()), {"lynx": "gray", "moth": "silver", "wasp": "yellow"})
