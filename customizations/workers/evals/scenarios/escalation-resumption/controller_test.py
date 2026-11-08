"""Provider-free blind injection, native identity, and real guard regressions."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "private_controller", Path(__file__).with_name("controller.py")
)
ec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ec)
REAL_GUARD = (
    Path(__file__).resolve().parents[5]
    / "customizations/plugins/delegation-guard/delegation-guard-transform"
)


@pytest.fixture
def run_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    return tmp_path / "private", workspace, runtime


def env_for(paths, session="owner", call="c1", **extra):
    state, workspace, runtime = paths
    return {
        **os.environ,
        ec.STATE_ENV: str(state),
        ec.WORKSPACE_ENV: str(workspace),
        ec.GUARD_ENV: str(REAL_GUARD),
        ec.RUNTIME_DIR_ENV: str(runtime),
        "JCODE_HOOK_EVENT": "post_tool_transform",
        "JCODE_HOOK_SESSION_ID": session,
        "JCODE_HOOK_TOOL_CALL_ID": call,
        "JCODE_HOOK_PROCESS_ROLE": "worker",
        "JCODE_HOOK_WORKER_BLUEPRINT_ID": "any-worker",
        "JCODE_HOOK_SWARM_ENABLED": "1",
        "JCODE_DELEGATION_GUARD_THRESHOLD": str(ec.GUARD_THRESHOLD_DEFAULT),
        "JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS": "advanced,expert",
        **extra,
    }


def payload_for(
    paths, session="owner", call="c1", stage=2, exit=0, probe=None, **extra
):
    marker = {"stage": stage, "exit": exit, "evidence": "actual check evidence"}
    command = f"python3 diagnostics.py --stage {stage}"
    if probe is not None:
        marker["probe"] = probe
        command += f" --probe {probe}"
    result = ec._marker_line(marker) + "\n"
    return json.dumps(
        {
            "event": "post_tool_transform",
            "session_id": session,
            "working_dir": str(paths[1]),
            "tool_name": "bash",
            "tool_call_id": call,
            "tool_input": command,
            "tool_result": result,
            "tool_result_file": None,
            "output_bytes": len(result.encode()),
            "tool_success": True,
            "swarm": {"blueprint_id": "any-worker"},
            **extra,
        }
    ).encode()


def drive(paths, session="owner", call="c1", stage=2, exit=0):
    return ec.handle(
        payload_for(paths, session, call, stage, exit), env_for(paths, session, call)
    )


def state(paths):
    return json.loads((paths[0] / "state.json").read_text())


def journal(paths, name="events.jsonl"):
    return [json.loads(line) for line in (paths[0] / name).read_text().splitlines()]


def assert_blind(text, paths):
    for word in (
        "synthetic",
        "controller",
        "jcode_escalation",
        "escalation-resumption",
        "actual_exit",
        "actual_evidence",
        "blocker",
        "probe",
        "tier",
        "quota",
        "budget",
        "assistance",
        "escalat",
        "b2",
        "b3",
        "advanced",
        "expert",
        "routine",
        "original result",
    ):
        assert word not in text.lower(), word
    assert not re.search(r"\btask\b", text)
    assert str(paths[0]) not in text


@pytest.mark.parametrize("stage", [2, 3])
def test_blind_budgets_handoffs_and_real_resolution(run_paths, stage):
    first = drive(run_paths, stage=stage)
    assert ec.parse_marker(first)["exit"] == 1
    assert_blind(first, run_paths)
    assert drive(run_paths, call="c2", stage=stage)
    assert drive(run_paths, call="spent", stage=stage) is None
    artifact = drive(run_paths, "fresh", "c3", stage)
    assert len(artifact.encode()) > ec.GUARD_THRESHOLD_DEFAULT
    assert '"kind": "diagnostic-details"' in artifact
    assert_blind(artifact, run_paths)
    if stage == 3:
        assert drive(run_paths, "fresh", "c4", stage)
        assert drive(run_paths, "fresh", "spent2", stage) is None
        assert "resolved" not in state(run_paths)["blind_faults"][str(stage)]
        assert drive(run_paths, "second-fresh", "c5", stage) is None
    else:
        assert drive(run_paths, "fresh", "c4", stage) is None
    fault = state(run_paths)["blind_faults"][str(stage)]
    assert fault["resolved"] is True
    assert drive(run_paths, call="after", stage=stage) is None
    records = journal(run_paths)
    assert records[0]["original_exit"] == 0
    assert records[0]["original_evidence"] == "actual check evidence"
    assert records[0]["injected"] is True
    assert all(record["mode"] == "blind" for record in records)
    assert records[-1]["visible"] == "original"


def test_replay_genuine_failure_and_ordinary_checks_pass_through(run_paths):
    assert drive(run_paths, exit=1) is None
    assert drive(run_paths, call="ordinary1", stage=1) is None
    assert drive(run_paths, call="ordinary4", stage=4) is None
    assert state(run_paths)["blind_faults"] == {}
    assert drive(run_paths, call="success")
    assert drive(run_paths, call="success") is None
    assert journal(run_paths)[-1]["kind"] == "replay-rejected"
    assert state(run_paths)["blind_faults"]["2"]["injected"]["owner"] == ["success"]


@pytest.mark.parametrize("spilled", [False, True])
def test_native_json_diagnostic_input_injects_stage_two(run_paths, spilled):
    # Native hooks serialize the bash arguments, not just the shell command.
    payload = json.loads(payload_for(run_paths, call="call_Q6sqTK9vAOxwco7gUgRQ3lfO"))
    arguments = json.dumps({
        "command": "python3 diagnostics.py --stage 2",
        "intent": "Run bare stage 2 diagnostic",
    }, separators=(",", ":"))
    payload["tool_input"] = arguments
    if spilled:
        path = run_paths[2] / "input.json"
        path.write_text(arguments)
        payload["tool_input"] = arguments[:20]
        payload["tool_input_file"] = str(path)
    env = env_for(run_paths, call=payload["tool_call_id"])
    output = ec.handle(json.dumps(payload).encode(), env)
    assert ec.parse_marker(output)["exit"] == 1
    assert journal(run_paths)[0]["injected"] is True
    assert journal(run_paths)[0]["original_exit"] == 0
    assert ec._command_checks(json.dumps({
        "command": "echo harmless", "intent": "python3 diagnostics.py --stage 2",
    })) == set()


def test_compound_diagnostics_match_each_marker_and_reject_forgery(run_paths):
    payload = json.loads(payload_for(run_paths))
    payload["tool_input"] = " && ".join(
        f"python3 diagnostics.py --stage {stage}" for stage in (1, 2, 3, 4)
    )
    payload["tool_result"] = "".join(
        ec._marker_line({"stage": stage, "exit": 0, "evidence": f"stage {stage}"}) + "\n"
        for stage in (1, 2, 3, 4)
    )
    output = ec.handle(json.dumps(payload).encode(), env_for(run_paths))
    markers = [marker for _, marker in ec._marker_lines(output)]
    assert [(m["stage"], m["exit"]) for m in markers] == [(1, 0), (2, 1), (3, 1), (4, 0)]
    assert [(event["stage"], event["injected"]) for event in journal(run_paths)] == [
        (1, False), (2, True), (3, True), (4, False)
    ]
    assert ec.handle(json.dumps(payload).encode(), env_for(run_paths)) is None
    payload["tool_call_id"] = "forged"
    payload["tool_input"] = "python3 diagnostics.py --stage 1"
    assert ec.handle(json.dumps(payload).encode(), env_for(run_paths, call="forged")) is None
    assert journal(run_paths)[-1]["kind"] == "marker-mismatch"
    assert ec._command_checks("echo diagnostics.py --stage 2") == set()


def test_masked_native_result_has_consistent_exit_statuses(run_paths):
    payload = json.loads(payload_for(run_paths))
    payload["tool_input"] = "python3 diagnostics.py --stage 2; python3 diagnostics.py --stage 3"
    payload["tool_result"] = (
        ec._marker_line({"stage": 2, "exit": 0, "evidence": "stage 2 passed"})
        + "\nstage2_exit=0\n"
        + ec._marker_line({"stage": 3, "exit": 0, "evidence": "stage 3 passed"})
        + "\nstage3_exit=0\noverall_exit=0\n\n--- Command finished with exit code: 0 ---\n\nExit code: 0\n"
    )
    output = ec.handle(json.dumps(payload).encode(), env_for(run_paths))
    assert [(m["stage"], m["exit"]) for _, m in ec._marker_lines(output)] == [(2, 1), (3, 1)]
    assert "stage2_exit=1" in output
    assert "stage3_exit=1" in output
    assert "overall_exit=1" in output
    assert "--- Command finished with exit code: 1 ---" in output
    assert "Exit code: 1" in output
    assert not re.search(r'(?:exit(?:_code)?[=: ]+|"exit":\s*)0\b', output)
    assert_blind(output, run_paths)


@pytest.mark.parametrize("mismatch", ["session", "call", "role", "stage", "probe"])
def test_native_identity_and_command_marker_concordance(run_paths, mismatch):
    env = env_for(run_paths)
    payload = json.loads(payload_for(run_paths, probe="primary"))
    if mismatch == "session":
        env["JCODE_HOOK_SESSION_ID"] = "forged"
    elif mismatch == "call":
        env["JCODE_HOOK_TOOL_CALL_ID"] = "forged"
    elif mismatch == "role":
        env["JCODE_HOOK_PROCESS_ROLE"] = "coordinator"
        payload["tool_input"] = "echo 'I am a worker'; " + payload["tool_input"]
    else:
        marker = ec.parse_marker(payload["tool_result"])
        marker[mismatch] = 4 if mismatch == "stage" else "secondary"
        payload["tool_result"] = ec._marker_line(marker)
    assert ec.handle(json.dumps(payload).encode(), env) is None
    assert state(run_paths)["blind_faults"] == {}
    assert journal(run_paths)[-1]["kind"] in {
        "untrusted-identity",
        "non-worker",
        "marker-mismatch",
    }
    assert drive(run_paths, call="genuine")


def test_inactive_foreign_workspace_and_non_diagnostics(run_paths):
    assert ec.handle(payload_for(run_paths), {}) is None
    assert (
        ec.handle(payload_for(run_paths, working_dir="/elsewhere"), env_for(run_paths))
        is None
    )
    assert (
        ec.handle(payload_for(run_paths, tool_name="read"), env_for(run_paths)) is None
    )
    assert not (run_paths[0] / "state.json").exists()
    assert (
        ec.handle(payload_for(run_paths, tool_input="ls -la"), env_for(run_paths))
        is None
    )
    assert state(run_paths)["mode"] == "blind"
    assert not (run_paths[0] / "events.jsonl").exists()


@pytest.mark.parametrize("mode", ["scripted", "blnd"])
def test_unsupported_mode_never_silently_injects(run_paths, mode):
    env = env_for(run_paths, **{ec.MODE_ENV: mode})
    with pytest.raises(ValueError, match="unknown"):
        ec._mode(env)
    assert ec.handle(payload_for(run_paths), env) is None
    assert not (run_paths[0] / "state.json").exists()
    assert not (run_paths[0] / "events.jsonl").exists()


def test_standalone_composite_spill_real_guard_and_private_journal(run_paths):
    drive(run_paths)
    native = run_paths[2] / "jcode-tool-payloads/fresh/output-native.bin"
    native.parent.mkdir(parents=True)
    native.write_text("original native payload")
    env = env_for(run_paths, "fresh", "c2", JCODE_HOOK_WORKER_BLUEPRINT_ID="advanced")
    completed = subprocess.run(
        [sys.executable, ec.__file__],
        input=payload_for(
            run_paths,
            "fresh",
            "c2",
            tool_result_file=str(native),
            swarm={"blueprint_id": "advanced"},
        ),
        env=env,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0
    nudge = json.loads(completed.stdout)["tool_result"]
    assert "delegation guard threshold" in nudge
    record = journal(run_paths, "guard-events.jsonl")[-1]
    spill = Path(record["controller"]["spill_path"])
    assert spill.parent == native.parent
    assert spill.name.startswith("output-c2-")
    assert "controller" not in nudge
    artifact = spill.read_text()
    assert_blind(artifact, run_paths)
    assert f"Tool result was {len(artifact.encode())} bytes" in nudge
    assert str(spill) in nudge
    assert spill.stat().st_mode & 0o777 == 0o400
    assert native.read_text() == "original native payload"
    assert record["guard"]["exit_status"] == 0
    assert record["guard"]["replacement"] is True
    assert record["final"]["source"] == "guard"
    assert record["original"]["result_sha256"] != record["final"]["sha256"]
    assert journal(run_paths)[-1]["original_exit"] == 0


def test_small_controller_replacement_and_original_passthrough(run_paths):
    env = env_for(run_paths)
    output = ec.composite(payload_for(run_paths), env, str(REAL_GUARD))
    assert_blind(json.loads(output)["tool_result"], run_paths)
    record = journal(run_paths, "guard-events.jsonl")[-1]
    assert record["controller"]["spill_path"] is None
    assert record["guard"]["replacement"] is False
    assert record["final"]["source"] == "controller"
    assert not (run_paths[2] / "jcode-tool-payloads").exists()
    env = env_for(run_paths, call="ordinary")
    assert (
        ec.composite(
            payload_for(run_paths, call="ordinary", stage=1), env, str(REAL_GUARD)
        )
        == b""
    )
    assert journal(run_paths, "guard-events.jsonl")[-1]["final"]["source"] == "original"


def test_unsafe_spill_identity_cannot_escape_native_root(run_paths):
    drive(run_paths)
    session = "fresh/../escape"
    env = env_for(run_paths, session, "c2", JCODE_HOOK_WORKER_BLUEPRINT_ID="advanced")
    output = ec.composite(
        payload_for(run_paths, session, "c2", swarm={"blueprint_id": "advanced"}),
        env,
        str(REAL_GUARD),
    )
    assert "Full output is available at" not in json.loads(output)["tool_result"]
    assert not list(run_paths[2].rglob("output-*"))
    record = journal(run_paths, "guard-events.jsonl")[-1]
    assert record["controller"]["spill_path"] is None
    assert record["final"]["source"] == "guard"


def test_guard_timeout_preserves_controller_and_filters_environment(
    run_paths, tmp_path
):
    drive(run_paths)
    slow = tmp_path / "slow-guard"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o700)
    env = env_for(
        run_paths,
        "fresh",
        "c2",
        **{
            ec.GUARD_TIMEOUT_ENV: "100",
            "PRIVATE_SECRET": "never",
            "JCODE_DELEGATION_GUARD_TAIL_BYTES": "0",
        },
    )
    allowed = ec._guard_environment(env)
    assert ec.STATE_ENV not in allowed
    assert "PRIVATE_SECRET" not in allowed
    assert allowed[ec.RUNTIME_DIR_ENV] == str(run_paths[2])
    assert allowed["JCODE_DELEGATION_GUARD_TAIL_BYTES"] == "0"
    output = ec.composite(payload_for(run_paths, "fresh", "c2"), env, str(slow))
    assert_blind(json.loads(output)["tool_result"], run_paths)
    record = journal(run_paths, "guard-events.jsonl")[-1]
    assert record["guard"]["timed_out"] is True
    assert record["guard"]["exit_status"] is None
    assert record["final"]["source"] == "controller"
    assert Path(record["controller"]["spill_path"]).is_file()


def test_fresh_process_ledger_rollback_and_stale_state(run_paths, monkeypatch):
    env = env_for(run_paths, call="non-worker", JCODE_HOOK_PROCESS_ROLE="coordinator")
    assert ec.handle(payload_for(run_paths, call="non-worker"), env) is None
    assert drive(run_paths, call="first", stage=1) is None
    before_events, before_state = journal(run_paths), state(run_paths)
    original = ec._atomic_write_json

    def fail_save(*args):
        raise OSError("simulated state save failure")

    monkeypatch.setattr(ec, "_atomic_write_json", fail_save)
    assert drive(run_paths, call="second", stage=1) is None
    assert journal(run_paths) == before_events
    assert state(run_paths) == before_state
    monkeypatch.setattr(ec, "_atomic_write_json", original)
    assert drive(run_paths, call="second", stage=1) is None
    stale = state(run_paths)
    stale["next_seq"] = 1
    (run_paths[0] / "state.json").write_text(json.dumps(stale))
    assert drive(run_paths, call="third", stage=1) is None
    assert [record["seq"] for record in journal(run_paths)] == [1, 2, 3, 4]
    assert state(run_paths)["processed_calls"] == {"first": 2, "second": 3, "third": 4}
