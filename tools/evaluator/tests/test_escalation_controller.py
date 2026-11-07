"""Controller tests for the escalation-resumption scenario.

These tests drive the controller with synthetic native envelopes and hook
environments. They prove the controller's orchestration logic only; they do
not claim anything about a live native run, the delegation guard's view of
replacements, or model capability. The scripted tests opt into the exact
choreography explicitly; the blind tests cover the default agent-blind
fault plan, its non-disclosure contract, and the real-guard composite path.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from jcode_harness_client import escalation_controller as ec


def make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspaces" / ec.SCENARIO_ID
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "workflow.py").write_text("# initial solution\n", encoding="utf-8")
    return workspace


def make_env(
    state: Path, workspace: Path, session: str, call: str, blueprint: str
) -> dict[str, str]:
    return {
        ec.STATE_ENV: str(state),
        ec.WORKSPACE_ENV: str(workspace),
        ec.MODE_ENV: ec.MODE_SCRIPTED,
        "JCODE_HOOK_EVENT": "post_tool_transform",
        "JCODE_HOOK_SESSION_ID": session,
        "JCODE_HOOK_TOOL_CALL_ID": call,
        "JCODE_HOOK_PROCESS_ROLE": "worker",
        "JCODE_HOOK_WORKER_BLUEPRINT_ID": blueprint,
    }


def envelope(
    workspace: Path,
    stage: int,
    session: str,
    call: str,
    blueprint: str,
    marker_exit: int = 0,
    evidence: str = "stage green",
    probe: str | None = None,
) -> bytes:
    marker = {"stage": stage, "exit": marker_exit, "evidence": evidence}
    command = f"python3 diagnostics.py --stage {stage}"
    if probe is not None:
        marker["probe"] = probe
        command += f" --probe {probe}"
    return json.dumps(
        {
            "event": "post_tool_transform",
            "session_id": session,
            "working_dir": str(workspace),
            "tool_name": "bash",
            "tool_call_id": call,
            "tool_input": command,
            "tool_result": ec._marker_line(marker) + "\n",
            "tool_result_file": None,
            "tool_input_file": None,
            "output_bytes": len(marker) + 1,
            "tool_success": True,
            "swarm": {"blueprint_id": blueprint},
        }
    ).encode("utf-8")


def read_state(state: Path) -> dict:
    return json.loads((state / "state.json").read_text(encoding="utf-8"))


def events(state: Path) -> list[dict]:
    lines = (state / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def marker_of(replacement: str) -> dict:
    markers = [
        line.strip()
        for line in replacement.splitlines()
        if line.strip().startswith(ec.MARKER_PREFIX)
    ]
    assert markers
    return json.loads(markers[0][len(ec.MARKER_PREFIX) :].strip())


def drive(
    tmp_path: Path,
    state: Path,
    workspace: Path,
    stage: int,
    session: str,
    call: str,
    blueprint: str,
    marker_exit: int = 0,
    env_extra: dict[str, str] | None = None,
    probe: str | None = None,
) -> str | None:
    env = make_env(state, workspace, session, call, blueprint)
    env.update(env_extra or {})
    return ec.handle(
        envelope(workspace, stage, session, call, blueprint, marker_exit, probe=probe),
        env,
    )


def test_full_scripted_trace_completes_with_retained_identities(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)

    # R1 pass: passthrough, routine identity recorded.
    assert drive(tmp_path, state, workspace, 1, "s-r", "c1", "routine") is None
    assert read_state(state)["routine_session"] == "s-r"
    assert read_state(state)["step"] == 1

    # A bare no-probe R2 run never counts: it directs the primary probe.
    bare = drive(tmp_path, state, workspace, 2, "s-r", "c1b", "routine")
    assert bare is not None
    bare_marker = marker_of(bare)
    assert bare_marker["task"] == "B2"
    assert bare_marker["next_probe"] == "primary"
    assert "--probe primary" in bare_marker["evidence"]
    assert "2" not in read_state(state)["blockers"]
    assert events(state)[-1]["kind"] == "duplicate-attempt"

    # R2: first synthetic B2 failure on the primary probe; no source edit.
    replacement = drive(
        tmp_path, state, workspace, 2, "s-r", "c2", "routine", probe="primary"
    )
    assert replacement is not None
    marker = marker_of(replacement)
    assert marker["synthetic"] is True and marker["task"] == "B2"
    assert marker["attempt_number"] == 1 and marker["failure_budget"] == 2
    assert marker["probe"] == "primary"
    assert marker["counted_probes"] == ["primary"]
    assert marker["next_probe"] == "secondary"
    assert "1 of 2" in marker["evidence"] and "stays locked" in marker["evidence"]
    assert "--probe secondary" in marker["evidence"]
    assert marker["actual_exit"] == 0 and "stage green" in marker["actual_evidence"]
    assert "retained" in replacement
    assert read_state(state)["step"] == 1
    assert read_state(state)["blockers"]["2"]["task"] == "B2"
    assert read_state(state)["blockers"]["2"]["failures"] == 1
    assert read_state(state)["blockers"]["2"]["probes"] == {"primary": "c2"}

    # A next-tier request before the budget is met is rejected explicitly.
    premature = drive(tmp_path, state, workspace, 2, "s-a2", "c2x", "advanced")
    assert premature is None
    assert events(state)[-1]["kind"] == "premature-escalation"
    assert events(state)[-1]["task"] == "B2"
    assert read_state(state)["step"] == 1

    # An already-counted probe rerun never counts as the second failure.
    duplicate = drive(
        tmp_path, state, workspace, 2, "s-r", "c3", "routine", probe="primary"
    )
    assert duplicate is not None and marker_of(duplicate)["task"] == "B2"
    assert marker_of(duplicate)["attempt_number"] == 1
    assert marker_of(duplicate)["next_probe"] == "secondary"
    assert read_state(state)["step"] == 1
    assert events(state)[-1]["kind"] == "duplicate-attempt"

    # Second meaningful B2 failure on the distinct secondary probe, with no
    # source edit at all, advances the script.
    second = drive(
        tmp_path, state, workspace, 2, "s-r", "c3b", "routine", probe="secondary"
    )
    assert second is not None
    second_marker = marker_of(second)
    assert second_marker["task"] == "B2"
    assert second_marker["probe"] == "secondary"
    assert second_marker["attempt_number"] == 2
    assert second_marker["failure_budget"] == 2
    assert second_marker["counted_probes"] == ["primary", "secondary"]
    assert "2 of 2" in second_marker["evidence"]
    assert "exhausted" in second_marker["evidence"]
    assert read_state(state)["blockers"]["2"]["failures"] == 2
    assert read_state(state)["step"] == 2
    assert (workspace / "workflow.py").read_text(
        encoding="utf-8"
    ) == "# initial solution\n"

    # A further run is flagged over budget, never a third injected failure.
    assert drive(tmp_path, state, workspace, 2, "s-r", "c3c", "routine") is None
    assert events(state)[-1]["kind"] == "over-budget"
    assert read_state(state)["blockers"]["2"]["failures"] == 2
    assert read_state(state)["step"] == 2

    # A2 artifact: oversized synthetic artifact for the guard to spill.
    artifact = drive(tmp_path, state, workspace, 2, "s-a2", "c4", "advanced")
    assert artifact is not None
    assert len(artifact.encode("utf-8")) > ec.GUARD_THRESHOLD_DEFAULT
    artifact_marker = marker_of(artifact)
    assert artifact_marker["synthetic"] is True
    assert artifact_marker["artifact_bytes"] > ec.GUARD_THRESHOLD_DEFAULT
    assert Path(artifact_marker["artifact_path"]).is_file()
    assert read_state(state)["a2_session"] == "s-a2"
    assert read_state(state)["step"] == 3

    # A2 resolve: actual failure does not advance.
    assert (
        drive(
            tmp_path,
            state,
            workspace,
            2,
            "s-a2",
            "c5",
            "advanced",
            marker_exit=1,
        )
        is None
    )
    assert read_state(state)["step"] == 3
    # Unchanged duplicate resolve must not advance either.
    assert (
        drive(
            tmp_path,
            state,
            workspace,
            2,
            "s-a2",
            "c6",
            "advanced",
            marker_exit=0,
        )
        is None
    )
    assert read_state(state)["step"] == 3
    assert events(state)[-1]["kind"] == "duplicate-attempt"
    # Changed source fingerprint plus actual acceptance resolves stage 2.
    (workspace / "workflow.py").write_text("# fixed\n", encoding="utf-8")
    assert drive(tmp_path, state, workspace, 2, "s-a2", "c7", "advanced") is None
    resolved = read_state(state)
    assert resolved["step"] == 4 and "2" not in resolved["blockers"]

    # R3: first synthetic B3 failure on the retained routine identity.
    blocker = drive(
        tmp_path, state, workspace, 3, "s-r", "c8", "routine", probe="primary"
    )
    assert marker_of(blocker)["task"] == "B3"
    assert marker_of(blocker)["attempt_number"] == 1
    assert read_state(state)["step"] == 4
    # Second meaningful B3 failure on the distinct probe unlocks stage 3.
    blocker_two = drive(
        tmp_path, state, workspace, 3, "s-r", "c8b", "routine", probe="secondary"
    )
    assert marker_of(blocker_two)["task"] == "B3"
    assert marker_of(blocker_two)["attempt_number"] == 2
    assert read_state(state)["step"] == 5

    # A3: first synthetic B3A assistance request toward the expert tier.
    assist = drive(
        tmp_path, state, workspace, 3, "s-a3", "c9", "advanced", probe="primary"
    )
    marker = marker_of(assist)
    assert marker["task"] == "B3A" and marker["synthetic"] is True
    assert marker["artifact_request"]["consumer"] == "expert"
    assert marker["attempt_number"] == 1
    assert read_state(state)["a3_session"] == "s-a3"
    assert read_state(state)["step"] == 5
    # The second B3A failure must come from the same A3 owner session.
    assert (
        drive(
            tmp_path, state, workspace, 3, "s-a9", "c9x", "advanced", probe="secondary"
        )
        is None
    )
    assert events(state)[-1]["kind"] == "identity-mismatch"
    assert read_state(state)["step"] == 5
    assist_two = drive(
        tmp_path, state, workspace, 3, "s-a3", "c9b", "advanced", probe="secondary"
    )
    assert marker_of(assist_two)["task"] == "B3A"
    assert marker_of(assist_two)["attempt_number"] == 2
    assert read_state(state)["step"] == 6

    # E3 artifact then actual resolve.
    expert_artifact = drive(tmp_path, state, workspace, 3, "s-e3", "c10", "expert")
    assert len(expert_artifact.encode("utf-8")) > ec.GUARD_THRESHOLD_DEFAULT
    assert read_state(state)["e3_session"] == "s-e3"
    assert read_state(state)["step"] == 7
    assert drive(tmp_path, state, workspace, 3, "s-e3", "c11", "expert") is None
    assert read_state(state)["step"] == 8

    # A3 verify requires the same A3 session.
    assert drive(tmp_path, state, workspace, 3, "s-a9", "c12", "advanced") is None
    assert read_state(state)["step"] == 8
    assert events(state)[-1]["kind"] == "identity-mismatch"
    assert drive(tmp_path, state, workspace, 3, "s-a3", "c13", "advanced") is None
    assert read_state(state)["step"] == 9

    # R4 final acceptance on the retained routine identity.
    assert drive(tmp_path, state, workspace, 4, "s-r", "c14", "routine") is None
    final = read_state(state)
    assert final["complete"] is True
    # After completion every check passes through untouched.
    assert drive(tmp_path, state, workspace, 2, "s-r", "c15", "routine") is None
    assert events(state)[-1]["kind"] == "script-complete"

    log = events(state)
    assert [record["seq"] for record in log] == list(range(1, len(log) + 1)), (
        "event sequence must be monotonic"
    )
    injected = [record for record in log if record["kind"] == "injected"]
    by_task: dict[str, list[dict]] = {}
    for record in injected:
        by_task.setdefault(str(record["task"]), []).append(record)
    assert set(by_task) == {"B2", "B3", "B3A"}
    for task, records in by_task.items():
        assert len(records) == ec.FAILURE_BUDGET, task
        assert [record["attempt_number"] for record in records] == [1, 2]
        assert len({record["tool_call_id"] for record in records}) == 2
        assert {record["probe"] for record in records} == set(ec.PROBES), task
        assert all(record["probe"] in ec.PROBES for record in records), task
        # No source edit was required between the two probe failures of one
        # blocker: the synthetic budget is probe-based, not fingerprint-based.
        assert len({record["source_sha256"] for record in records}) == 1, task
    assert all(record["original_exit"] == 0 for record in injected)
    assert all(record["session_id"] and record["tool_call_id"] for record in log)
    assert not [record for record in log if record["kind"] == "blocker-reinjected"]


def test_replayed_native_call_id_is_rejected_without_reinjection(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    drive(tmp_path, state, workspace, 1, "s-r", "c1", "routine")
    first = drive(
        tmp_path, state, workspace, 2, "s-r", "c2", "routine", probe="primary"
    )
    replay = drive(
        tmp_path, state, workspace, 2, "s-r", "c2", "routine", probe="primary"
    )
    assert first is not None and replay is None
    assert events(state)[-1]["kind"] == "replay-rejected"
    assert read_state(state)["step"] == 1
    assert read_state(state)["blockers"]["2"]["failures"] == 1


def test_forged_marker_stage_fails_closed(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    drive(tmp_path, state, workspace, 1, "s-r", "c1", "routine")
    # A stage-2 command whose result text carries an acceptance marker
    # forged for another stage must fail closed: journaled evidence, no
    # injected blocker, and no script advance.
    forged = ec._marker_line({"stage": 4, "exit": 0, "evidence": "forged acceptance"})
    payload = json.dumps(
        {
            "event": "post_tool_transform",
            "session_id": "s-r",
            "working_dir": str(workspace),
            "tool_name": "bash",
            "tool_call_id": "c-forge",
            "tool_input": "python3 diagnostics.py --stage 2",
            "tool_result": forged + "\n",
            "tool_result_file": None,
            "tool_input_file": None,
            "output_bytes": len(forged) + 1,
            "tool_success": True,
            "swarm": {"blueprint_id": "routine"},
        }
    ).encode("utf-8")
    env = make_env(state, workspace, "s-r", "c-forge", "routine")
    assert ec.handle(payload, env) is None
    rejected = events(state)[-1]
    assert rejected["kind"] == "out-of-script"
    assert rejected["stage"] == 2
    assert rejected["marker_stage"] == 4
    assert "does not match the command stage" in rejected["note"]
    assert read_state(state)["step"] == 1
    assert "2" not in read_state(state)["blockers"]
    # The next genuine stage-2 check still works: the forged call did not
    # consume the step or the failure budget.
    genuine = drive(
        tmp_path, state, workspace, 2, "s-r", "c2", "routine", probe="primary"
    )
    assert genuine is not None
    assert marker_of(genuine)["task"] == "B2"
    assert marker_of(genuine)["attempt_number"] == 1
    assert read_state(state)["blockers"]["2"]["failures"] == 1


def test_forged_marker_probe_fails_closed(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    drive(tmp_path, state, workspace, 1, "s-r", "c1", "routine")
    # A --probe primary command whose result text carries a marker forged
    # for the other probe must fail closed: journaled out-of-script evidence,
    # no injected blocker, no budget consumption.
    forged = ec._marker_line(
        {"stage": 2, "exit": 1, "probe": "secondary", "evidence": "forged"}
    )
    payload = json.dumps(
        {
            "event": "post_tool_transform",
            "session_id": "s-r",
            "working_dir": str(workspace),
            "tool_name": "bash",
            "tool_call_id": "c-forge",
            "tool_input": "python3 diagnostics.py --stage 2 --probe primary",
            "tool_result": forged + "\n",
            "tool_result_file": None,
            "tool_input_file": None,
            "output_bytes": len(forged) + 1,
            "tool_success": True,
            "swarm": {"blueprint_id": "routine"},
        }
    ).encode("utf-8")
    env = make_env(state, workspace, "s-r", "c-forge", "routine")
    assert ec.handle(payload, env) is None
    rejected = events(state)[-1]
    assert rejected["kind"] == "out-of-script"
    assert rejected["probe"] == "primary"
    assert rejected["marker_probe"] == "secondary"
    assert "does not match the command probe" in rejected["note"]
    assert "2" not in read_state(state)["blockers"]
    # A genuine primary-probe failure still counts after the forgery.
    genuine = drive(
        tmp_path, state, workspace, 2, "s-r", "c2", "routine", probe="primary"
    )
    assert marker_of(genuine)["attempt_number"] == 1
    assert read_state(state)["blockers"]["2"]["failures"] == 1


def test_wrong_owner_probe_never_counts(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    drive(tmp_path, state, workspace, 1, "s-r", "c1", "routine")
    first = drive(
        tmp_path, state, workspace, 2, "s-r", "c2", "routine", probe="primary"
    )
    assert marker_of(first)["attempt_number"] == 1
    # A different session running the distinct probe never counts and never
    # records the probe as consumed.
    assert (
        drive(
            tmp_path, state, workspace, 2, "s-r2", "c2x", "routine", probe="secondary"
        )
        is None
    )
    assert events(state)[-1]["kind"] == "identity-mismatch"
    assert events(state)[-1]["expected_session"] == "s-r"
    assert read_state(state)["blockers"]["2"]["failures"] == 1
    assert read_state(state)["step"] == 1
    # The owner session's distinct probe run is still the second failure.
    second = drive(
        tmp_path, state, workspace, 2, "s-r", "c2b", "routine", probe="secondary"
    )
    assert marker_of(second)["attempt_number"] == 2
    assert read_state(state)["step"] == 2


def test_partial_probe_never_resolves_a_stage(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    # A passing secondary-probe marker cannot even record stage 1.
    partial_record = drive(
        tmp_path, state, workspace, 1, "s-r", "c0", "routine", probe="secondary"
    )
    assert partial_record is None
    assert events(state)[-1]["kind"] == "out-of-script"
    assert "partial probe subset never records" in events(state)[-1]["note"]
    assert read_state(state)["step"] == 0
    drive(tmp_path, state, workspace, 1, "s-r", "c1", "routine")
    drive(tmp_path, state, workspace, 2, "s-r", "c2", "routine", probe="primary")
    drive(tmp_path, state, workspace, 2, "s-r", "c2b", "routine", probe="secondary")
    assert drive(tmp_path, state, workspace, 2, "s-a2", "c4", "advanced") is not None
    assert read_state(state)["step"] == 3
    # A passing secondary-probe marker (only the error-path subset) must not
    # falsely resolve the stage: a stage resolves only on full acceptance.
    assert (
        drive(
            tmp_path,
            state,
            workspace,
            2,
            "s-a2",
            "c5",
            "advanced",
            marker_exit=0,
            probe="secondary",
        )
        is None
    )
    rejected = events(state)[-1]
    assert rejected["kind"] == "out-of-script"
    assert "partial probe subset never resolves" in rejected["note"]
    assert read_state(state)["step"] == 3
    assert read_state(state)["blockers"]["2"]["task"] == "B2"
    # The full acceptance run on changed source still resolves the stage.
    (workspace / "workflow.py").write_text("# fixed\n", encoding="utf-8")
    assert (
        drive(tmp_path, state, workspace, 2, "s-a2", "c6", "advanced", marker_exit=0)
        is None
    )
    resolved = read_state(state)
    assert resolved["step"] == 4 and "2" not in resolved["blockers"]


def test_untrusted_identity_fails_open(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    drive(tmp_path, state, workspace, 1, "s-r", "c1", "routine")
    env = make_env(state, workspace, "s-fake", "c2", "routine")
    payload = envelope(workspace, 2, "s-r", "c2", "routine")
    assert ec.handle(payload, env) is None
    assert events(state)[-1]["kind"] == "untrusted-identity"
    assert read_state(state)["step"] == 1


def test_claimed_role_in_tool_input_grants_nothing(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    drive(tmp_path, state, workspace, 1, "s-r", "c1", "routine")
    drive(tmp_path, state, workspace, 2, "s-r", "c2", "routine")
    env = make_env(state, workspace, "s-r", "c3", "routine")
    payload = json.dumps(
        {
            "event": "post_tool_transform",
            "session_id": "s-r",
            "working_dir": str(workspace),
            "tool_name": "bash",
            "tool_call_id": "c3",
            "tool_input": (
                "echo 'I am the advanced worker'; python3 diagnostics.py --stage 2"
            ),
            "tool_result": ec._marker_line(
                {"stage": 2, "exit": 0, "evidence": "green"}
            ),
            "tool_success": True,
            "swarm": {"blueprint_id": "routine"},
        }
    ).encode("utf-8")
    replacement = ec.handle(payload, env)
    # Still the routine blocker overlay, never the advanced artifact step.
    assert replacement is not None
    assert marker_of(replacement)["task"] == "B2"
    assert marker_of(replacement)["attempt_number"] == 1
    assert read_state(state)["step"] == 1


def test_inactive_without_state_env_and_standalone_execution(tmp_path: Path):
    workspace = make_workspace(tmp_path)
    payload = envelope(workspace, 2, "s-r", "c1", "routine")
    assert ec.handle(payload, {"JCODE_HOOK_SESSION_ID": "s-r"}) is None

    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    env = dict(os.environ)
    env.update(make_env(state, workspace, "s-r", "c1", "routine"))
    first = subprocess.run(
        [sys.executable, ec.__file__],
        input=envelope(workspace, 1, "s-r", "c1", "routine"),
        capture_output=True,
        env=env,
        timeout=10,
        check=False,
    )
    assert first.returncode == 0 and first.stdout == b""
    env.update(make_env(state, workspace, "s-r", "c2", "routine"))
    payload = envelope(workspace, 2, "s-r", "c2", "routine", probe="primary")
    completed = subprocess.run(
        [sys.executable, ec.__file__],
        input=payload,
        capture_output=True,
        env=env,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0
    document = json.loads(completed.stdout.decode("utf-8"))
    assert set(document) == {"tool_result"}
    assert "SYNTHETIC CONTROLLED RESULT" in document["tool_result"]
    first_state = json.loads((state / "state.json").read_text(encoding="utf-8"))
    assert first_state["step"] == 1
    assert first_state["blockers"]["2"]["failures"] == 1
    # A second distinct standalone probe completes the exact budget with no
    # source edit at all.
    env.update(make_env(state, workspace, "s-r", "c3", "routine"))
    second = subprocess.run(
        [sys.executable, ec.__file__],
        input=envelope(workspace, 2, "s-r", "c3", "routine", probe="secondary"),
        capture_output=True,
        env=env,
        timeout=10,
        check=False,
    )
    assert second.returncode == 0
    assert (
        "SYNTHETIC CONTROLLED RESULT"
        in json.loads(second.stdout.decode("utf-8"))["tool_result"]
    )
    second_state = json.loads((state / "state.json").read_text(encoding="utf-8"))
    assert second_state["step"] == 2
    assert second_state["blockers"]["2"]["failures"] == 2


def test_non_diagnostics_and_foreign_workspace_calls_pass_through(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    env = make_env(state, workspace, "s-r", "c1", "routine")
    payload = json.dumps(
        {
            "event": "post_tool_transform",
            "session_id": "s-r",
            "working_dir": str(other),
            "tool_name": "bash",
            "tool_call_id": "c1",
            "tool_input": "python3 diagnostics.py --stage 2",
            "tool_result": "ok",
            "swarm": {"blueprint_id": "routine"},
        }
    ).encode("utf-8")
    assert ec.handle(payload, env) is None
    assert not (state / "state.json").exists()
    plain = json.dumps(
        {
            "session_id": "s-r",
            "working_dir": str(workspace),
            "tool_name": "read",
            "tool_call_id": "c2",
            "tool_input": "read workflow.py",
            "tool_result": "ok",
            "swarm": {"blueprint_id": "routine"},
        }
    ).encode("utf-8")
    assert ec.handle(plain, env) is None


REAL_GUARD = (
    Path(__file__).resolve().parents[3]
    / "customizations"
    / "plugins"
    / "delegation-guard"
    / "delegation-guard-transform"
)


def composite_env(
    state: Path,
    workspace: Path,
    session: str,
    call: str,
    blueprint: str,
    runtime: Path,
    guard: Path | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            ec.STATE_ENV: str(state),
            ec.WORKSPACE_ENV: str(workspace),
            ec.MODE_ENV: ec.MODE_SCRIPTED,
            ec.GUARD_ENV: str(guard or REAL_GUARD),
            "JCODE_HOOK_EVENT": "post_tool_transform",
            "JCODE_HOOK_SESSION_ID": session,
            "JCODE_HOOK_TOOL_CALL_ID": call,
            "JCODE_HOOK_PROCESS_ROLE": "worker",
            "JCODE_HOOK_WORKER_BLUEPRINT_ID": blueprint,
            "JCODE_HOOK_SWARM_ENABLED": "1",
            "JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS": "advanced,expert",
            "JCODE_DELEGATION_GUARD_THRESHOLD": str(ec.GUARD_THRESHOLD_DEFAULT),
            "JCODE_RUNTIME_DIR": str(runtime),
        }
    )
    env.update(extra or {})
    return env


def file_envelope(
    workspace: Path,
    stage: int,
    session: str,
    call: str,
    blueprint: str,
    result_file: Path | None,
) -> bytes:
    marker = ec._marker_line({"stage": stage, "exit": 0, "evidence": "stage green"})
    return json.dumps(
        {
            "event": "post_tool_transform",
            "session_id": session,
            "working_dir": str(workspace),
            "tool_name": "bash",
            "tool_call_id": call,
            "tool_input": f"python3 diagnostics.py --stage {stage}",
            "tool_result": marker + "\n",
            "tool_result_file": None if result_file is None else str(result_file),
            "output_bytes": 64,
            "tool_success": True,
            "swarm": {"blueprint_id": blueprint},
        }
    ).encode("utf-8")


def guard_events(state: Path) -> list[dict]:
    path = state / "guard-events.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_composite_artifact_spills_and_real_guard_nudges_with_native_output_kept(
    tmp_path: Path,
):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    runtime = tmp_path / "runtime"
    payload_dir = runtime / "jcode-tool-payloads" / "s-a2"
    payload_dir.mkdir(parents=True)
    native_payload = payload_dir / "output-native.bin"
    native_payload.write_text("native worker output", encoding="utf-8")

    # R1 and both R2 failures advance the script to the A2 artifact step.
    assert drive(tmp_path, state, workspace, 1, "s-r", "c1", "routine") is None
    assert (
        drive(tmp_path, state, workspace, 2, "s-r", "c2", "routine", probe="primary")
        is not None
    )
    assert (
        drive(tmp_path, state, workspace, 2, "s-r", "c2b", "routine", probe="secondary")
        is not None
    )

    env = composite_env(state, workspace, "s-a2", "c4", "advanced", runtime)
    payload = file_envelope(workspace, 2, "s-a2", "c4", "advanced", native_payload)
    output = ec.composite(payload, env, str(REAL_GUARD))

    document = json.loads(output.decode("utf-8"))
    assert set(document) == {"tool_result"}
    nudge = document["tool_result"]
    # The actual installed guard produced the nudge, never the controller.
    assert "delegation guard threshold" in nudge
    assert "swarm_routine" in nudge
    spilled = next(payload_dir.glob("output-c4-*.json"))
    # The visible spill path carries no instrumentation name.
    assert "controller" not in spilled.name
    artifact = spilled.read_text(encoding="utf-8")
    size = len(artifact.encode("utf-8"))
    assert size > ec.GUARD_THRESHOLD_DEFAULT
    # The guard saw the controller artifact, not the original native output.
    assert f"Tool result was {size} bytes" in nudge
    assert str(spilled) in nudge
    assert "output-native.bin" not in nudge
    # The spill is immutable and the counterpart native output is untouched.
    assert spilled.stat().st_mode & 0o777 == 0o400
    assert native_payload.read_text(encoding="utf-8") == "native worker output"
    # Controller state and actual guard provenance are both retained.
    assert read_state(state)["a2_session"] == "s-a2"
    event = guard_events(state)[-1]
    assert event["kind"] == "guard-run"
    assert event["controller"]["replacement"] is True
    assert event["controller"]["spill_path"] == str(spilled)
    assert event["guard"]["exit_status"] == 0
    assert event["guard"]["replacement"] is True
    assert event["final"]["source"] == "guard"
    assert event["original"]["result_sha256"] != event["final"]["sha256"]
    assert marker_of(artifact)["synthetic"] is True


def test_composite_r2_synthetic_keeps_actual_diagnostic_and_guard_passes_through(
    tmp_path: Path,
):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    runtime = tmp_path / "runtime"

    # R1 through the composite proves exact original-result pass-through.
    env1 = composite_env(state, workspace, "s-r", "c1", "routine", runtime)
    first = ec.composite(
        envelope(workspace, 1, "s-r", "c1", "routine"), env1, str(REAL_GUARD)
    )
    assert first == b""
    event = guard_events(state)[-1]
    assert event["controller"]["replacement"] is False
    assert event["guard"]["exit_status"] == 0
    assert event["guard"]["replacement"] is False
    assert event["final"]["source"] == "original"

    # R2: synthetic blocker from the controller, actual diagnostic retained.
    env2 = composite_env(state, workspace, "s-r", "c2", "routine", runtime)
    output = ec.composite(
        envelope(workspace, 2, "s-r", "c2", "routine", probe="primary"),
        env2,
        str(REAL_GUARD),
    )
    document = json.loads(output.decode("utf-8"))
    replacement = document["tool_result"]
    marker = marker_of(replacement)
    assert marker["synthetic"] is True and marker["task"] == "B2"
    assert marker["attempt_number"] == 1
    assert marker["actual_exit"] == 0
    assert "stage green" in replacement
    event = guard_events(state)[-1]
    assert event["controller"]["replacement"] is True
    assert event["controller"]["spill_path"] is None
    assert event["guard"]["exit_status"] == 0
    assert event["guard"]["replacement"] is False
    assert event["final"]["source"] == "controller"
    # No spill happened for the small replacement.
    assert not (runtime / "jcode-tool-payloads").exists()


def test_composite_unsafe_identity_spill_fails_open_to_inline_guard(
    tmp_path: Path,
):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    runtime = tmp_path / "runtime"
    drive(tmp_path, state, workspace, 1, "s-r", "c1", "routine")
    drive(tmp_path, state, workspace, 2, "s-r", "c2", "routine", probe="primary")
    drive(tmp_path, state, workspace, 2, "s-r", "c2b", "routine", probe="secondary")

    session = "s-a2/../escape"
    env = composite_env(state, workspace, session, "c4", "advanced", runtime)
    payload = file_envelope(workspace, 2, session, "c4", "advanced", None)
    output = ec.composite(payload, env, str(REAL_GUARD))

    document = json.loads(output.decode("utf-8"))
    nudge = document["tool_result"]
    # No spill file escaped; the honest guard nudge reports the missing path.
    assert "report that blocker to the parent" in nudge
    assert "Full output is available at" not in nudge
    assert not list((runtime / "jcode-tool-payloads").glob("**/output-*"))
    assert not list((runtime / "jcode-tool-payloads").glob("**/controller-*"))
    event = guard_events(state)[-1]
    assert event["controller"]["replacement"] is True
    assert event["controller"]["spill_path"] is None
    assert event["guard"]["replacement"] is True
    assert event["final"]["source"] == "guard"


def test_composite_guard_timeout_fails_open_to_controller_replacement(
    tmp_path: Path,
):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    drive(tmp_path, state, workspace, 1, "s-r", "c1", "routine")
    drive(tmp_path, state, workspace, 2, "s-r", "c2", "routine", probe="primary")
    drive(tmp_path, state, workspace, 2, "s-r", "c2b", "routine", probe="secondary")

    slow = tmp_path / "slow-guard"
    slow.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    slow.chmod(0o700)
    env = composite_env(
        state,
        workspace,
        "s-a2",
        "c4",
        "advanced",
        runtime,
        guard=slow,
        extra={ec.GUARD_TIMEOUT_ENV: "100"},
    )
    payload = file_envelope(workspace, 2, "s-a2", "c4", "advanced", None)
    output = ec.composite(payload, env, str(slow))
    # The A2 artifact replacement survives the bounded guard timeout honestly.
    document = json.loads(output.decode("utf-8"))
    marker = marker_of(document["tool_result"])
    assert marker["kind"] == "large-diagnostic-artifact"
    assert marker["synthetic"] is True
    event = guard_events(state)[-1]
    assert event["guard"]["timed_out"] is True
    assert event["guard"]["exit_status"] is None
    assert event["guard"]["replacement"] is False
    assert event["final"]["source"] == "controller"
    # The spilled artifact was still handed to the guard before it timed out.
    assert event["controller"]["spill_path"] is not None
    assert Path(event["controller"]["spill_path"]).is_file()


def test_main_composes_controller_then_real_guard_end_to_end(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    runtime = tmp_path / "runtime"
    payload_dir = runtime / "jcode-tool-payloads" / "s-a2"
    payload_dir.mkdir(parents=True)
    native_payload = payload_dir / "output-native.bin"
    native_payload.write_text("native worker output", encoding="utf-8")
    drive(tmp_path, state, workspace, 1, "s-r", "c1", "routine")
    drive(tmp_path, state, workspace, 2, "s-r", "c2", "routine", probe="primary")
    drive(tmp_path, state, workspace, 2, "s-r", "c2b", "routine", probe="secondary")

    env = composite_env(state, workspace, "s-a2", "c4", "advanced", runtime)
    payload = file_envelope(workspace, 2, "s-a2", "c4", "advanced", native_payload)
    completed = subprocess.run(
        [sys.executable, ec.__file__],
        input=payload,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0
    document = json.loads(completed.stdout.decode("utf-8"))
    assert "delegation guard threshold" in document["tool_result"]
    # The visible nudge names the generic spill artifact, and the private
    # guard journal retains its full provenance.
    spill_path = guard_events(state)[-1]["controller"]["spill_path"]
    assert "controller" not in Path(spill_path).name
    assert spill_path in document["tool_result"]
    assert guard_events(state)[-1]["final"]["source"] == "guard"


# --- Blind mode ---------------------------------------------------------
#
# Blind is the default: the agent never sees fault, tier, probe, quota,
# or controller-state language, arbitrary role names behave like any
# other, and the private journal retains the real evidence.


BLIND_FORBIDDEN = (
    "SYNTHETIC",
    "synthetic",
    "controller",
    "JCODE_ESCALATION",
    "escalation-resumption",
    "actual_exit",
    "actual_evidence",
    "task",
    "blocker",
    "probe",
    "tier",
    "quota",
    "budget",
    "assistance",
    "escalat",
    "B2",
    "B3",
    "B3A",
    "advanced",
    "expert",
    "routine",
    "ORIGINAL RESULT",
)


def bdrive(
    tmp_path: Path,
    state: Path,
    workspace: Path,
    stage: int,
    session: str,
    call: str,
    blueprint: str,
    marker_exit: int = 0,
) -> str | None:
    return drive(
        tmp_path,
        state,
        workspace,
        stage,
        session,
        call,
        blueprint,
        marker_exit=marker_exit,
        env_extra={ec.MODE_ENV: ec.MODE_BLIND},
    )


def assert_no_disclosure(text: str, state: Path) -> None:
    for word in BLIND_FORBIDDEN:
        if word == "task":
            # The fixture's real function names (schedule_subtasks) are
            # ordinary milestone content; only a bare controller "task"
            # label would be a disclosure.
            assert not re.search(r"\btask\b", text), "disclosed 'task'"
            continue
        assert word not in text, f"disclosed {word!r} to the agent"
    assert str(state) not in text
    assert str(state.parent) not in text


def test_blind_is_default_and_masks_first_passing_stage_check(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    replacement = bdrive(tmp_path, state, workspace, 2, "s-owner", "c1", "routine")
    assert replacement is not None
    marker = marker_of(replacement)
    assert marker["stage"] == 2
    assert marker["exit"] == 1
    assert "milestone behavior specification" in marker["evidence"]
    assert_no_disclosure(replacement, state)
    fault = read_state(state)["blind_faults"]["2"]
    assert fault["owner_session"] == "s-owner"
    assert fault["injected"]["s-owner"] == ["c1"]
    first = events(state)[0]
    assert first["kind"] == "fault-activated"
    assert first["mode"] == ec.MODE_BLIND
    assert first["original_exit"] == 0
    assert first["injected"] is True
    assert first["visible"] == "replaced"


def test_blind_stage2_budget_fresh_investigation_and_resolution(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    assert bdrive(tmp_path, state, workspace, 2, "s-owner", "c1", "routine")
    assert bdrive(tmp_path, state, workspace, 2, "s-owner", "c2", "routine")
    # The bounded per-session budget is spent; the real result passes through.
    assert bdrive(tmp_path, state, workspace, 2, "s-owner", "c3", "routine") is None
    third = events(state)[-1]
    assert third["kind"] == "check"
    assert third["injected"] is False
    assert third["visible"] == "original"
    assert third["original_exit"] == 0
    # A fresh investigation under an arbitrary role name is masked once and
    # automatically returns the large detailed artifact.
    replacement = bdrive(
        tmp_path, state, workspace, 2, "s-fresh", "c4", "senior-implementer"
    )
    assert replacement is not None
    assert len(replacement.encode("utf-8")) > ec.GUARD_THRESHOLD_DEFAULT
    assert '"kind": "diagnostic-details"' in replacement
    assert_no_disclosure(replacement, state)
    fresh = events(state)[-1]
    assert fresh["kind"] == "injected"
    assert fresh["artifact_bytes"] > ec.GUARD_THRESHOLD_DEFAULT
    # A subsequent fresh investigation resolves on the actual pass.
    assert (
        bdrive(tmp_path, state, workspace, 2, "s-fresh", "c5", "senior-implementer")
        is None
    )
    resolved = events(state)[-1]
    assert resolved["kind"] == "fault-resolved"
    assert resolved["injected"] is False
    assert resolved["visible"] == "original"
    assert resolved["original_exit"] == 0
    assert read_state(state)["blind_faults"]["2"]["resolved_by"] == "s-fresh"
    # After resolution every check sees the real result, and no role or
    # tier gating event ever appears.
    assert bdrive(tmp_path, state, workspace, 2, "s-owner", "c6", "routine") is None
    kinds = [record["kind"] for record in events(state)]
    assert "premature-escalation" not in kinds
    assert "identity-mismatch" not in kinds
    assert "out-of-script" not in kinds


def test_blind_stage3_persists_across_first_handoff(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    assert bdrive(tmp_path, state, workspace, 3, "s-owner", "c1", "routine")
    assert bdrive(tmp_path, state, workspace, 3, "s-owner", "c2", "routine")
    assert bdrive(tmp_path, state, workspace, 3, "s-owner", "c3", "routine") is None
    first_fresh = bdrive(
        tmp_path, state, workspace, 3, "s-fresh", "c4", "deep-investigator"
    )
    assert first_fresh is not None
    assert '"kind": "diagnostic-details"' in first_fresh
    assert read_state(state)["blind_faults"]["3"]["fresh_sessions"] == ["s-fresh"]
    assert bdrive(tmp_path, state, workspace, 3, "s-fresh", "c5", "deep-investigator")
    # The first fresh session never resolves stage 3: the fault persists
    # across the first handoff, and its spent budget passes the real
    # result through without resolving.
    assert (
        bdrive(tmp_path, state, workspace, 3, "s-fresh", "c6", "deep-investigator")
        is None
    )
    assert events(state)[-1]["kind"] == "check"
    assert "resolved" not in read_state(state)["blind_faults"]["3"]
    # A subsequent fresh investigation resolves on the actual pass.
    assert (
        bdrive(tmp_path, state, workspace, 3, "s-second", "c7", "another-reviewer")
        is None
    )
    assert events(state)[-1]["kind"] == "fault-resolved"
    assert read_state(state)["blind_faults"]["3"]["resolved_by"] == "s-second"
    kinds = [record["kind"] for record in events(state)]
    assert "premature-escalation" not in kinds
    assert "identity-mismatch" not in kinds


def test_blind_replayed_call_id_never_injects_twice(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    assert bdrive(tmp_path, state, workspace, 2, "s-owner", "c1", "routine")
    assert bdrive(tmp_path, state, workspace, 2, "s-owner", "c1", "routine") is None
    assert events(state)[-1]["kind"] == "replay-rejected"
    assert read_state(state)["blind_faults"]["2"]["injected"]["s-owner"] == ["c1"]


def test_blind_genuine_failure_and_ordinary_stages_pass_through(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    assert (
        bdrive(tmp_path, state, workspace, 2, "s-a", "c1", "routine", marker_exit=1)
        is None
    )
    assert events(state)[0]["original_exit"] == 1
    assert read_state(state)["blind_faults"] == {}
    assert bdrive(tmp_path, state, workspace, 1, "s-a", "c2", "routine") is None
    assert bdrive(tmp_path, state, workspace, 4, "s-a", "c3", "routine") is None
    assert read_state(state)["blind_faults"] == {}


def test_composite_blind_artifact_spills_and_real_guard_nudges(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    runtime = tmp_path / "runtime"
    payload_dir = runtime / "jcode-tool-payloads" / "s-fresh"
    payload_dir.mkdir(parents=True)
    native_payload = payload_dir / "output-native.bin"
    native_payload.write_text("native worker output", encoding="utf-8")
    bdrive(tmp_path, state, workspace, 2, "s-owner", "c1", "routine")
    bdrive(tmp_path, state, workspace, 2, "s-owner", "c2", "routine")
    env = composite_env(
        state,
        workspace,
        "s-fresh",
        "c4",
        "advanced",
        runtime,
        extra={ec.MODE_ENV: ec.MODE_BLIND},
    )
    payload = file_envelope(workspace, 2, "s-fresh", "c4", "advanced", native_payload)
    completed = subprocess.run(
        [sys.executable, ec.__file__],
        input=payload,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0
    document = json.loads(completed.stdout.decode("utf-8"))
    assert "delegation guard threshold" in document["tool_result"]
    # The visible nudge and spill path carry no instrumentation name.
    spill_path = guard_events(state)[-1]["controller"]["spill_path"]
    assert "controller" not in Path(spill_path).name
    assert spill_path in document["tool_result"]
    assert "controller" not in document["tool_result"]
    assert guard_events(state)[-1]["final"]["source"] == "guard"
    controller_event = events(state)[-1]
    assert controller_event["kind"] == "injected"
    assert controller_event["mode"] == ec.MODE_BLIND
    assert controller_event["original_exit"] == 0


def _ordinary_call_envelope(workspace: Path, session: str, call: str) -> bytes:
    """A non-diagnostic tool call the controller sees but never journals."""

    return json.dumps(
        {
            "event": "post_tool_transform",
            "session_id": session,
            "working_dir": str(workspace),
            "tool_name": "bash",
            "tool_call_id": call,
            "tool_input": "ls -la",
            "tool_result": "",
        }
    ).encode("utf-8")


def test_blind_mode_is_persisted_to_state_before_any_event(tmp_path: Path):
    """The evaluator-owned state records the mode before any event.

    The first hook invocation persists the effective mode even when the
    call never matches a controlled check, so a zero-event run still
    tells the analyzer which mode it ran in.
    """

    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    env = make_env(state, workspace, "s-owner", "c1", "routine")
    env[ec.MODE_ENV] = ec.MODE_BLIND
    assert ec.handle(_ordinary_call_envelope(workspace, "s-owner", "c1"), env) is None
    assert read_state(state)["mode"] == ec.MODE_BLIND
    assert not (state / "events.jsonl").exists()


def test_scripted_mode_is_persisted_to_state_before_any_event(tmp_path: Path):
    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    env = make_env(state, workspace, "s-owner", "c1", "routine")
    assert ec.handle(_ordinary_call_envelope(workspace, "s-owner", "c1"), env) is None
    assert read_state(state)["mode"] == ec.MODE_SCRIPTED
    assert not (state / "events.jsonl").exists()


def test_unknown_mode_fails_preflight_and_writes_no_evidence(tmp_path: Path):
    """An unknown mode value is a preflight failure, never silent blind."""

    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    env = make_env(state, workspace, "s-owner", "c1", "routine")
    env[ec.MODE_ENV] = "blnd"
    with pytest.raises(ValueError, match="unknown"):
        ec._mode(env)
    # The hook itself fails open: no state, no event, no replacement.
    assert ec.handle(_ordinary_call_envelope(workspace, "s-owner", "c1"), env) is None
    assert not (state / "state.json").exists()
    assert not (state / "events.jsonl").exists()


def test_multi_process_ledger_stays_unique_and_monotonic(tmp_path: Path):
    """Fresh hook processes persist the ledger on every branch.

    Each ``handle`` invocation models one fresh hook process: it reloads
    the persisted state under the controller lock. Out-of-script branches
    previously returned without saving, so a later process reused sequence
    numbers (the historical journal repeated seq 6, 9, and 15). Every
    branch must persist the ledger sequence and processed calls, keeping
    the journal unique and strictly monotonic across processes.
    """

    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    # Process one: a non-worker role is always out of script.
    env = make_env(state, workspace, "s-coordinator", "c-oos-1", "routine")
    env["JCODE_HOOK_PROCESS_ROLE"] = "coordinator"
    assert (
        ec.handle(envelope(workspace, 1, "s-coordinator", "c-oos-1", "routine"), env)
        is None
    )
    assert read_state(state)["next_seq"] == 2
    first = events(state)
    assert [event["seq"] for event in first] == [1]
    assert first[0]["kind"] == "out-of-script"

    # Process two: the first scripted step records normally and its
    # processed call identity is persisted for later processes.
    assert (
        drive(tmp_path, state, workspace, 1, "s-routine", "c-valid", "routine") is None
    )
    persisted = read_state(state)
    assert persisted["next_seq"] == 3
    assert persisted["processed_calls"]["c-valid"] == 2
    assert persisted["step"] == 1
    second = events(state)
    assert [event["seq"] for event in second] == [1, 2]
    assert second[1]["kind"] == "record"

    # Process three: another out-of-script call still advances the ledger.
    env = make_env(state, workspace, "s-coordinator", "c-oos-2", "routine")
    env["JCODE_HOOK_PROCESS_ROLE"] = "coordinator"
    assert (
        ec.handle(envelope(workspace, 2, "s-coordinator", "c-oos-2", "routine"), env)
        is None
    )
    third = events(state)
    assert [event["seq"] for event in third] == [1, 2, 3]
    assert third[2]["kind"] == "out-of-script"
    assert read_state(state)["next_seq"] == 4
    # The out-of-script calls never enter processed calls; the valid
    # call's processed identity survives the later processes.
    final_state = read_state(state)
    assert set(final_state["processed_calls"]) == {"c-valid"}


def test_append_rollback_leaves_no_half_persisted_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A failing state save rolls the journal back atomically.

    The event append and the state save advance together: when the save
    fails, the appended record is truncated away and the in-memory
    sequence is restored, so a retry never double-appends, reuses a
    sequence number, or undoes only one side.
    """

    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    assert (
        drive(tmp_path, state, workspace, 1, "s-routine", "c-first", "routine") is None
    )
    before_events = events(state)
    before_state = read_state(state)

    def failing_save(path, value):
        raise OSError("simulated state save failure")

    monkeypatch.setattr(ec, "_atomic_write_json", failing_save)
    # The hook fails open on the simulated OSError, and the ledger and
    # the state file both stay exactly as they were.
    assert (
        drive(tmp_path, state, workspace, 1, "s-routine", "c-second", "routine") is None
    )
    assert events(state) == before_events
    assert read_state(state) == before_state

    monkeypatch.undo()
    # The retry appends exactly one new record with the next sequence.
    assert (
        drive(tmp_path, state, workspace, 1, "s-routine", "c-second", "routine") is None
    )
    ledger = [event["seq"] for event in events(state)]
    assert ledger == [1, 2]
    assert read_state(state)["next_seq"] == 3


def test_stale_state_file_never_reuses_a_journal_sequence(tmp_path: Path):
    """A journal record that outlived its state file still advances the seq.

    A prior process may have appended and fsynced a record but died before
    the atomic state replace. A fresh process reconciles the next sequence
    from the retained journal tail, so no sequence is ever reused even
    against a stale state file.
    """

    state = tmp_path / "instrumentation" / ec.SCENARIO_ID
    workspace = make_workspace(tmp_path)
    assert (
        drive(tmp_path, state, workspace, 1, "s-routine", "c-first", "routine") is None
    )
    # Simulate the crash window: the state file reverts to a stale next_seq
    # while the journal keeps its last record.
    stale = read_state(state)
    stale["next_seq"] = 1
    (state / "state.json").write_text(json.dumps(stale), encoding="utf-8")
    assert (
        drive(tmp_path, state, workspace, 1, "s-routine", "c-second", "routine") is None
    )
    ledger = [event["seq"] for event in events(state)]
    assert ledger == [1, 2]
