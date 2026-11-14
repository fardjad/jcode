"""Simulate a transient local service probe before running the task."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys

key = hashlib.sha256(str(Path.cwd().resolve()).encode()).hexdigest()[:16]
state = Path(os.environ["HOME"]) / ".normalizer-probe" / key
state.parent.mkdir(parents=True, exist_ok=True)
count = int(state.read_text()) if state.exists() else 0
state.write_text(str(count + 1))
if count == 0:
    raise SystemExit("service temporarily unavailable; try the probe again")
subprocess.run([sys.executable, "normalize.py"], check=True)
print("normalization complete")
