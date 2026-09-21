#!/usr/bin/env -S uv run --script
# /// script
# dependencies = []
# ///
"""Record one observer envelope. Hook failures are intentionally fail-open."""
from __future__ import annotations
import argparse
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from delegation_efficiency.ingest import ingest
from delegation_efficiency.schema import configure_state_dir

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", help="override the plugin state directory")
    args = parser.parse_args()
    configure_state_dir(args.state_dir)
    try:
        payload = os.environ.get("JCODE_HOOK_PAYLOAD")
        raw = payload.encode() if payload else sys.stdin.buffer.read()
        result = ingest(raw)
        print(json.dumps({"status": result}, separators=(",", ":")))
    except Exception as exc:
        # Never echo input or exception details, which could contain hostile data.
        print(json.dumps({"status": "dropped"}, separators=(",", ":")))
    return 0

if __name__ == "__main__": raise SystemExit(main())
