"""Interrupt once after the first durable checkpoint."""
import hashlib
import os
from pathlib import Path


def saved(count):
    key = hashlib.sha256(str(Path.cwd().resolve()).encode()).hexdigest()[:16]
    marker = Path(os.environ["HOME"]) / ".batch-interruption" / key
    marker.parent.mkdir(parents=True, exist_ok=True)
    if count == 1 and not marker.exists():
        marker.write_text("interrupted\n")
        raise SystemExit("batch interrupted after checkpoint; resume it")
