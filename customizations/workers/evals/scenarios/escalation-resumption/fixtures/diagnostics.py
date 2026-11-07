#!/usr/bin/env python3
"""Stage diagnostics for the sensor-record pipeline milestones.

Usage:
    python3 diagnostics.py --stage <1|2|3|4> [--probe primary|secondary]

Runs the real milestone acceptance checks and prints one machine-readable
marker line:

    JCODE_EVAL_CHECK {"probe": "primary", "stage": 1, "exit": 0, ...}

Exit status is the actual check result: 0 passed, 1 failed, 2 invalid usage.
The two probes are distinct actual diagnostic passes over different check
subsets of the same acceptance class: ``primary`` runs the stage's full
acceptance class, ``secondary`` runs the complementary error-path subset.
Both report their own failing-check evidence; neither is a label-only rerun.
A bare run without ``--probe`` is the full acceptance class, identical to
``--probe primary`` except that its marker carries no ``probe`` field.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

STAGE_CLASSES = {
    1: "TestMilestone1Normalize",
    2: "TestMilestone2Deduplicate",
    3: "TestMilestone3Schedule",
    4: "TestMilestone4Aggregate",
}

PROBES = ("primary", "secondary")
USAGE_EXIT = 2


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _run_checks(stage: int, probe: str | None) -> tuple[int, str, str]:
    target = "workflow_test." + STAGE_CLASSES[stage]
    argv = [sys.executable, "-m", "unittest", "-q"]
    if probe == "secondary":
        # A genuinely different check subset: only the error-path checks of
        # the same real acceptance class. Every stage class has at least one.
        argv += ["-k", "reject"]
    argv.append(target)
    proc = subprocess.run(
        argv,
        cwd=str(HERE),
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode, _collapse(proc.stderr), _collapse(proc.stdout)


def _evidence(stage: int, output: str, probe: str | None) -> str:
    prefix = "" if probe is None else f"probe {probe}: "
    match = re.search(r"(?:FAIL|ERROR): (test_[A-Za-z0-9_]+)", output)
    if match:
        excerpt = output[match.start() : match.start() + 220]
        counts = re.search(r"Ran \d+ tests.*", output)
        detail = excerpt + ("; " + counts.group(0) if counts else "")
        return f"{prefix}stage {stage} failed at {match.group(1)}: {detail}"
    if re.search(r"\bOK\b", output):
        return (
            f"{prefix}stage {stage} passed: "
            f"{STAGE_CLASSES[stage]} acceptance checks green"
        )
    return f"{prefix}stage {stage} produced no check result: {output[:180] or 'no output'}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one milestone's acceptance checks and print a JCODE_EVAL_CHECK marker."
    )
    parser.add_argument("--stage", type=int, required=True, choices=sorted(STAGE_CLASSES))
    parser.add_argument(
        "--probe",
        default=None,
        choices=PROBES,
        help=(
            "distinct actual diagnostic pass: primary runs the full stage"
            " acceptance class, secondary the complementary error-path subset"
        ),
    )
    args = parser.parse_args(argv)

    stage = args.stage
    returncode, stderr, _stdout = _run_checks(stage, args.probe)
    exit_code = 0 if returncode == 0 else 1
    marker = {
        "stage": stage,
        "exit": exit_code,
        "evidence": _evidence(stage, stderr, args.probe),
    }
    if args.probe is not None:
        marker["probe"] = args.probe
    print("JCODE_EVAL_CHECK " + json.dumps(marker, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
