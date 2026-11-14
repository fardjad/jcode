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
            "rows": 4, "groups": ["insect", "tree"]
        })

    def test_program_with_fresh_lookup(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            shutil.copytree(Path.cwd(), workspace)
            (workspace / "lookup.csv").write_text("name,group\na,water\nb,stone\nc,water\nd,cloud\ne,stone\n")
            (workspace / "summary.json").unlink(missing_ok=True)
            run = subprocess.run([sys.executable, "summarize.py"], cwd=workspace, capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(json.loads((workspace / "summary.json").read_text()), {"rows": 5, "groups": ["cloud", "stone", "water"]})
