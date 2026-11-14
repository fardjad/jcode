import csv
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class Acceptance(unittest.TestCase):
    def test_normalized_rows(self):
        with Path("output.csv").open(newline="") as stream:
            self.assertEqual(list(csv.reader(stream)), [
                ["id", "label"], ["1", "blue sky"], ["2", "red"], ["3", "green"]
            ])

    def test_program_with_fresh_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp) / "workspace"
            shutil.copytree(Path.cwd(), workspace)
            (workspace / "input.csv").write_text('id,label\n8,  GOLD  \n9," MiXeD, CASE "\n10,WHITE\n11,  Violet\n')
            (workspace / "output.csv").unlink(missing_ok=True)
            run = subprocess.run([sys.executable, "normalize.py"], cwd=workspace, capture_output=True, text=True, timeout=10, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            with (workspace / "output.csv").open(newline="") as stream:
                self.assertEqual(list(csv.reader(stream)), [["id", "label"], ["8", "gold"], ["9", "mixed, case"], ["10", "white"], ["11", "violet"]])
