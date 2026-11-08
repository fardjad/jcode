"""Known-bad and known-good oracle controls for the owned evaluation fixtures.

The reference solutions live only here, in the evaluator test tree outside the
fixture workspaces that evaluated agents receive. Each owned fixture's
baseline must fail its real acceptance command for the intended reason, and
the reference solution must pass both the acceptance command and, where the
fixture publishes one, the public diagnostics surface with its actual marker
behavior. Blindness is checked with a narrow, semantically targeted blacklist
of scenario-machinery words over the agent-visible public surfaces only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCENARIOS = REPO_ROOT / "customizations" / "workers" / "evals" / "scenarios"
STABLE_TASK_ORDER = SCENARIOS / "stable-task-order" / "fixtures"
ESCALATION_RESUMPTION = SCENARIOS / "escalation-resumption" / "fixtures"
STAGES = (1, 2, 3, 4)

# Reference solution (control only, never shipped inside the fixture).
STABLE_TASK_ORDER_REFERENCE = """def order_tasks(tasks):
    remaining = {name: set(deps) for name, deps in tasks.items()}
    if any(dep not in tasks for deps in remaining.values() for dep in deps):
        raise ValueError("unknown dependency")
    result = []
    while remaining:
        ready = next((name for name, deps in remaining.items() if not deps), None)
        if ready is None:
            raise ValueError("cycle")
        result.append(ready)
        del remaining[ready]
        for deps in remaining.values():
            deps.discard(ready)
    return result
"""

# Reference solution (control only, never shipped inside the fixture).
ESCALATION_RESUMPTION_REFERENCE = '''"""Four-milestone sensor-record pipeline (known-good reference)."""


def normalize_records(raw):
    if not isinstance(raw, list):
        raise ValueError("raw records must be a list")
    out = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each raw record must be a dict")
        for key in ("id", "amount", "tags"):
            if key not in item:
                raise ValueError("missing key: " + key)
        after = item.get("after", [])
        if not isinstance(after, list):
            raise ValueError("after must be a list")
        try:
            amount = float(str(item["amount"]).strip())
        except ValueError:
            raise ValueError("non-numeric amount: " + str(item["amount"])) from None
        tags = [t.strip().lower() for t in str(item["tags"]).split(",") if t.strip()]
        deps = sorted({str(d).strip() for d in after if str(d).strip()})
        out.append(
            {
                "id": str(item["id"]).strip(),
                "amount": amount,
                "tags": tags,
                "after": deps,
            }
        )
    return out


def deduplicate_records(records):
    if not isinstance(records, list):
        raise ValueError("records must be a list")
    seen = set()
    out = []
    for rec in records:
        key = str(rec["id"]).casefold()
        if key not in seen:
            seen.add(key)
            out.append(dict(rec))
    return out


def schedule_subtasks(records):
    if not isinstance(records, list):
        raise ValueError("records must be a list")
    by_id = {}
    for rec in records:
        key = str(rec["id"]).casefold()
        if key in by_id:
            raise ValueError("duplicate record id: " + str(rec["id"]))
        by_id[key] = rec
    deps = {}
    for key, rec in by_id.items():
        wanted = []
        for dep in rec["after"]:
            name = str(dep).casefold()
            if name == key:
                raise ValueError("self-dependency: " + str(dep))
            if name not in by_id:
                raise ValueError("unknown dependency: " + str(dep))
            wanted.append(name)
        deps[key] = wanted
    remaining = [str(rec["id"]).casefold() for rec in records]
    done = set()
    order = []
    while remaining:
        pick = next((k for k in remaining if all(d in done for d in deps[k])), None)
        if pick is None:
            raise ValueError("dependency cycle")
        remaining.remove(pick)
        done.add(pick)
        order.append(pick)
    return [str(by_id[k]["id"]) for k in order]


def aggregate_results(schedule, records):
    if not isinstance(schedule, list) or not isinstance(records, list):
        raise ValueError("schedule and records must be lists")
    by_id = {}
    for rec in records:
        key = str(rec["id"]).casefold()
        if key in by_id:
            raise ValueError("duplicate record id: " + str(rec["id"]))
        by_id[key] = rec
    total = 0.0
    count = 0
    tags = {}
    used = set()
    for name in schedule:
        key = str(name).casefold()
        if key not in by_id:
            raise ValueError("scheduled id not in records: " + str(name))
        if key in used:
            raise ValueError("scheduled id repeated: " + str(name))
        used.add(key)
        rec = by_id[key]
        total += rec["amount"]
        count += 1
        for tag in rec["tags"]:
            tags[tag] = tags.get(tag, 0) + 1
    return {
        "total": round(total, 2),
        "count": count,
        "tags": dict(sorted(tags.items())),
    }
'''


def _workspace(tmp_path: Path, fixture: Path) -> Path:
    workspace = tmp_path / "workspace"
    shutil.copytree(
        fixture, workspace, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
    )
    return workspace


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
    )


def _marker(proc: subprocess.CompletedProcess) -> dict:
    lines = [
        line for line in proc.stdout.splitlines() if line.startswith("JCODE_EVAL_CHECK")
    ]
    assert len(lines) == 1, proc.stdout + proc.stderr
    return json.loads(lines[0].split(" ", 1)[1])


def test_stable_task_order_known_bad_fails_and_reference_passes(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path, STABLE_TASK_ORDER)

    baseline = _run(workspace, "-m", "unittest", "scheduler_test.py")
    assert baseline.returncode != 0
    assert "FAIL" in baseline.stderr

    (workspace / "scheduler.py").write_text(
        STABLE_TASK_ORDER_REFERENCE, encoding="utf-8"
    )
    good = _run(workspace, "-m", "unittest", "scheduler_test.py")
    assert good.returncode == 0, good.stderr


def test_escalation_resumption_known_bad_fails_and_reference_passes(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path, ESCALATION_RESUMPTION)

    baseline = _run(workspace, "-m", "unittest", "workflow_test.py")
    assert baseline.returncode != 0
    assert "NotImplementedError" in baseline.stderr

    for stage in STAGES:
        proc = _run(workspace, "diagnostics.py", "--stage", str(stage))
        assert proc.returncode == 1, proc.stdout + proc.stderr
        marker = _marker(proc)
        # The marker exposes only stage, exit, and evidence: no routing,
        # tier, quota, or fault-mode state is ever part of the output.
        assert set(marker) == {"stage", "exit", "evidence"}
        assert marker["stage"] == stage
        assert marker["exit"] == 1
        assert f"stage {stage} failed" in marker["evidence"]

    (workspace / "workflow.py").write_text(
        ESCALATION_RESUMPTION_REFERENCE, encoding="utf-8"
    )
    good = _run(workspace, "-m", "unittest", "workflow_test.py")
    assert good.returncode == 0, good.stderr
    for stage in STAGES:
        proc = _run(workspace, "diagnostics.py", "--stage", str(stage))
        assert proc.returncode == 0, proc.stdout + proc.stderr
        marker = _marker(proc)
        assert set(marker) == {"stage", "exit", "evidence"}
        assert marker["stage"] == stage
        assert marker["exit"] == 0
        assert "passed" in marker["evidence"]


def test_agent_visible_surfaces_carry_no_scenario_machinery(tmp_path: Path) -> None:
    """Blindness over the public surfaces an evaluated agent reads.

    The scan uses narrow scenario-machinery words per surface. Everyday words
    such as "routine" are deliberately not scanned, and the probe flag is a
    documented diagnostics command, so only machinery words are rejected.
    """

    readme = (ESCALATION_RESUMPTION / "README.md").read_text(encoding="utf-8").lower()
    for word in ("synthetic", "inject", "escalat", "tier", "quota", "controller"):
        assert word not in readme, word

    diagnostics = (
        (ESCALATION_RESUMPTION / "diagnostics.py").read_text(encoding="utf-8").lower()
    )
    for word in (
        "synthetic",
        "inject",
        "controller",
        "tier",
        "quota",
        "advanced",
        "expert",
        "artifact",
    ):
        assert word not in diagnostics, word

    workspace = _workspace(tmp_path, ESCALATION_RESUMPTION)
    help_proc = _run(workspace, "diagnostics.py", "--help")
    assert help_proc.returncode == 0
    fail_proc = _run(workspace, "diagnostics.py", "--stage", "2")
    assert fail_proc.returncode == 1
    for proc in (help_proc, fail_proc):
        for word in ("synthetic", "inject", "controller", "tier", "quota", "artifact"):
            assert word not in proc.stdout.lower(), word

    scenario_config = tomllib.loads(
        (SCENARIOS / "escalation-resumption" / "config.toml").read_text(
            encoding="utf-8"
        )
    )
    prompt = scenario_config["prompt"]
    for word in (
        "synthetic",
        "inject",
        "escalat",
        "tier",
        "probe",
        "quota",
        "controller",
        "resume",
    ):
        assert word not in prompt.lower(), word
