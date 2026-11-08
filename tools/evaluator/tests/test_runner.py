from __future__ import annotations

import importlib.util
import json
import re
import shlex
import shutil
import sys
import tempfile
import tomllib
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest

from jcode_harness_client.cli import main as cli_main
from jcode_harness_client.client import HarnessTimeoutError
from jcode_harness_client.llm_backend import CallUsageRecord
from jcode_harness_client.runner import (
    DEFAULT_RUNTIME_ISOLATION,
    resolve_runtime_isolation,
    run,
)
from jcode_harness_client.runtime import runtime_environment
from jcode_harness_client.scenarios import load_scenarios
from jcode_harness_client.settings import ScenarioSettings, parse_settings
from jcode_harness_client.user_actor import SimulatedUserReply

SECRET = "test-secret-never-real"
WORKER_NAME = f"worker-{uuid4().hex}"
UNUSED_WORKER_NAME = f"worker-{uuid4().hex}"
WORKER_SESSION_ID = f"session_{uuid4().hex}"
COORDINATOR_SESSION_ID = f"session_{uuid4().hex}"


CONTROLLER_ASSET = (
    Path(__file__).resolve().parents[3]
    / "customizations/workers/evals/scenarios/escalation-resumption/controller.py"
)
_controller_spec = importlib.util.spec_from_file_location(
    "private_escalation_controller", CONTROLLER_ASSET
)
escalation_controller = importlib.util.module_from_spec(_controller_spec)
_controller_spec.loader.exec_module(escalation_controller)


def evaluation_settings(
    worker_models: dict[str, str] | None = None,
    guard_blueprints: list[str] | None = None,
    guard_threshold: int | None = None,
    limits: dict[str, int] | None = None,
):
    worker_mapping = ""
    if worker_models:
        worker_mapping = "\n[worker_models]\n" + "\n".join(
            f"{json.dumps(worker)} = {json.dumps(model)}"
            for worker, model in worker_models.items()
        )
    limits_inline = ""
    if limits:
        base = f"""[provider]
type = "openai-compatible"
url = "https://endpoint.invalid/v1"
model = "gpt-4o"
api_key = "{SECRET}"
[models]
coordinator = "gpt-4o"
swarm = "gpt-4o-mini"
judge = "gpt-4o"
simulator = "gpt-4o-mini"
[limits]
swarm_timeout_minutes = 2
acceptance_timeout_seconds = 20
"""
        if "turn_timeout_seconds" not in limits:
            base += "turn_timeout_seconds = 42\n"
        limits_inline = "\n".join(f"{key} = {value}" for key, value in limits.items())
        text = base + limits_inline + "\n"
    else:
        text = f"""[provider]
type = "openai-compatible"
url = "https://endpoint.invalid/v1"
model = "gpt-4o"
api_key = "{SECRET}"
[models]
coordinator = "gpt-4o"
swarm = "gpt-4o-mini"
judge = "gpt-4o"
simulator = "gpt-4o-mini"
[limits]
turn_timeout_seconds = 42
swarm_timeout_minutes = 2
acceptance_timeout_seconds = 20
"""
    guard = ""
    if guard_blueprints is not None or guard_threshold is not None:
        guard = "\n[delegation_guard]\n"
        if guard_blueprints is not None:
            guard += "worker_blueprints = " + json.dumps(guard_blueprints) + "\n"
        if guard_threshold is not None:
            guard += f"threshold = {guard_threshold}\n"
    return parse_settings(text + worker_mapping + guard)


def settings_for(scenario_id: str, settings) -> dict[str, ScenarioSettings]:
    """Wrap one settings object as the per-scenario map for a single run."""

    return {
        scenario_id: ScenarioSettings(
            scenario_id=scenario_id,
            settings=settings,
        )
    }


def make_plugins(root: Path) -> Path:
    plugins = root / "plugins"
    for plugin, executable in (
        ("rtk", "rtk-transform"),
        ("delegation-guard", "delegation-guard-transform"),
    ):
        directory = plugins / plugin
        directory.mkdir(parents=True)
        executable_path = directory / executable
        executable_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable_path.chmod(0o755)
    return plugins


def make_inputs(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    fixture = root / "fixture"
    fixture.mkdir()
    (fixture / "target.txt").write_text("OLD\n", encoding="utf-8")
    scenario_dir = root / "scenarios"
    scenario_dir.mkdir()
    accept = [
        "python3",
        "-c",
        "from pathlib import Path; assert Path('target.txt').read_text() == 'NEW\\n'",
    ]
    scenario_config_dir = scenario_dir / "real-code"
    scenario_config_dir.mkdir()
    (scenario_config_dir / "config.toml").write_text(
        'id = "real-code"\n'
        'fixture = "../../fixture"\n'
        'prompt = "Implement the outcome in the current workspace."\n'
        f"accept = {json.dumps(accept)}\n"
        "\n[judge]\n"
        'criteria = ["Good coordination uses suitable workers and verifies results."]\n'
        "\n[simulated_user]\n"
        "max_turns = 8\n"
        'instructions = "Approve reasonable plans and let the agent decide."\n',
        encoding="utf-8",
    )
    blueprints = root / "blueprints"
    blueprints.mkdir()
    (blueprints / "coordinator.md").write_text(
        "---\nenabled-tools: []\ndisabled-tools: []\n---\nCoordinate.\n",
        encoding="utf-8",
    )
    for name in (WORKER_NAME, UNUSED_WORKER_NAME):
        (blueprints / f"{name}.md").write_text(
            f"---\nname: {name}\nenabled-tools: []\n---\nBounded contract.\n",
            encoding="utf-8",
        )
    binary = root / "jcode"
    bridge = root / "bridge"
    binary.write_text("binary", encoding="utf-8")
    bridge.write_text("bridge", encoding="utf-8")
    return scenario_dir, blueprints, binary, bridge, fixture


def native_message(message_id: str, role: str, text: str, **extra: Any) -> dict:
    return {
        "id": message_id,
        "role": role,
        "content": [{"type": "text", "text": text}],
        **extra,
    }


def fake_harness(timeout: bool = False, captured: dict | None = None):
    def launch(**kwargs: Any):
        if captured is not None:
            captured.update(kwargs)

        @contextmanager
        def context():
            with tempfile.TemporaryDirectory(prefix="fake-native-") as temporary:
                root = Path(temporary)
                home = root / "jcode"
                sessions = home / "sessions"
                sessions.mkdir(parents=True)
                (home / "logs").mkdir()
                (home / "logs" / "jcode-test.log").write_text(
                    "native log", encoding="utf-8"
                )
                (home / "daemon.log").write_text(SECRET, encoding="utf-8")
                (home / "bridge.log").write_text("bridge log", encoding="utf-8")
                parent_id = COORDINATOR_SESSION_ID
                worker_id = WORKER_SESSION_ID
                (sessions / f"{parent_id}.journal.jsonl").write_text(
                    json.dumps({"append_messages": []}) + "\n", encoding="utf-8"
                )
                parent_snapshot = {
                    "session_id": parent_id,
                    "model": "gpt-4o",
                    "messages": [
                        native_message("user", "user", kwargs["config"]),
                        native_message(
                            "assistant",
                            "assistant",
                            SECRET,
                            token_usage={
                                "prompt_tokens": 120,
                                "input_tokens": 120,
                                "output_tokens": 30,
                                "cache_read_input_tokens": 40,
                            },
                        ),
                    ],
                    "future_native_field": {"retained": True},
                }
                worker_snapshot = {
                    "session_id": worker_id,
                    "model": "gpt-4o-mini",
                    "parent_session_id": parent_id,
                    "parent_id": parent_id,
                    "worker_blueprint_policy": {
                        "name": WORKER_NAME,
                        "enabled_tools": ["read"],
                    },
                    "agent_label": "animal-name-is-not-a-role",
                    "swarm_status": {"state": "completed"},
                    "edit_stats": {"files": 1},
                    "messages": [
                        native_message(
                            "worker-user",
                            "user",
                            "assignment",
                            token_usage={"prompt_tokens": 15, "output_tokens": 5},
                        ),
                    ],
                }

                native_turns: list[str] = []

                class Session:
                    session_id = parent_id

                    def send_message(self, prompt: str, **options: Any):
                        turn_index = len(native_turns)
                        native_turns.append(prompt)
                        if captured is not None:
                            captured.setdefault("prompts", []).append(prompt)
                            captured.setdefault("send_timeouts", []).append(
                                options.get("timeout")
                            )
                        if turn_index == 0:
                            assert "current workspace" in prompt
                        response_text = (
                            "Shall I proceed?" if turn_index == 0 else "Task complete"
                        )
                        parent_snapshot["messages"].extend(
                            [
                                native_message(f"user-{turn_index}", "user", prompt),
                                native_message(
                                    f"assistant-{turn_index}",
                                    "assistant",
                                    response_text,
                                ),
                            ]
                        )
                        workspace = kwargs["workspace"]
                        (workspace / "target.txt").write_text("NEW\n", encoding="utf-8")
                        (sessions / f"{parent_id}.json").write_text(
                            json.dumps(parent_snapshot), encoding="utf-8"
                        )
                        (sessions / f"{worker_id}.json").write_text(
                            json.dumps(worker_snapshot), encoding="utf-8"
                        )
                        options["event_sink"].append(
                            {"v": 1, "ev": "turn_done", "session_id": parent_id}
                        )
                        if timeout:
                            raise HarnessTimeoutError("fixture timeout")
                        return SimpleNamespace(
                            text=response_text,
                            completed_normally=True,
                            stop_reason=None,
                            stop_message=None,
                            error_message=None,
                        )

                class Client:
                    def create_session(self, **options: Any):
                        return Session()

                    def list_tools(self, session_id: str):
                        assert session_id == parent_id
                        return [
                            SimpleNamespace(
                                raw={"name": "read", "input_schema": {"type": "object"}}
                            )
                        ]

                    def list_sessions(self):
                        return [
                            SimpleNamespace(
                                raw={
                                    "session_id": parent_id,
                                    "parent_session_id": None,
                                }
                            ),
                            SimpleNamespace(
                                raw={
                                    "session_id": worker_id,
                                    "parent_session_id": parent_id,
                                    "worker_blueprint_policy": {"name": WORKER_NAME},
                                }
                            ),
                        ]

                try:
                    yield SimpleNamespace(
                        client=Client(),
                        home=home,
                        root=root,
                        workspace=kwargs["workspace"],
                    )
                finally:
                    kwargs["stopped"]()

        return context()

    return launch


def done_user(**kwargs: Any) -> SimulatedUserReply:
    tracker = kwargs.get("usage_tracker")
    if tracker is not None:
        tracker.add(
            CallUsageRecord(
                role="simulator",
                model="gpt-4o-mini",
                provider="openai-compatible",
                input_tokens=50,
                output_tokens=10,
                cache_read_tokens=20,
                cost_usd=0.001,
                priced=True,
                completed=True,
                category="evaluation-overhead",
            )
        )
    return SimulatedUserReply(done=True, message="")


def clean_review(**kwargs: Any):
    tracker = kwargs.get("usage_tracker")
    if tracker is not None:
        tracker.add(
            CallUsageRecord(
                role="judge",
                model="gpt-4o",
                provider="openai-compatible",
                input_tokens=300,
                output_tokens=40,
                cache_read_tokens=200,
                cost_usd=0.01,
                priced=True,
                completed=True,
                category="evaluation-overhead",
            )
        )
    return {"findings": [], "error": None}


@pytest.mark.parametrize(
    "case,expected",
    [("finding", 0), ("judge-error", 1), ("accept-fail", 1), ("timeout", 1)],
)
def test_report_pipeline_exit_and_retention(
    tmp_path: Path, case: str, expected: int
) -> None:
    scenario_dir, blueprints, binary, bridge, fixture = make_inputs(tmp_path)
    if case == "accept-fail":
        config = scenario_dir / "real-code" / "config.toml"
        text = config.read_text()
        text = text.replace(
            "assert Path('target.txt').read_text() == 'NEW\\\\n'", "assert False"
        )
        config.write_text(text)
    output = tmp_path / "output"
    ref = f"session:{COORDINATOR_SESSION_ID}:message:assistant-0"
    finding = {
        "severity": "high",
        "category": "blueprint",
        "text": "Review this",
        "citations": [ref],
        "fix_target": "coordinator",
    }
    response = {"findings": [finding]}
    with (
        patch(
            "jcode_harness_client.runner.launch_harness",
            fake_harness(timeout=case == "timeout"),
        ),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
        patch(
            "jcode_harness_client.judge.complete",
            side_effect=RuntimeError("judge failed") if case == "judge-error" else None,
            return_value=response,
        ) as judge,
    ):
        code = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for("real-code", evaluation_settings()),
            output=output,
            scenario_path=scenario_dir,
            scenario_id=None,
            blueprints_dir=blueprints,
            plugins_dir=None,
            build_provenance={"build_check_seconds": 1.234},
        )
    assert code == expected
    assert judge.call_count == 1
    report = (output / "report.md").read_text()
    assert (
        "acceptance PASS" in report
        if case != "accept-fail"
        else "acceptance FAIL" in report
    )
    assert "## Time" in report and "## Delegation" in report
    assert "build check 1.234s (catalog build check)" in report
    assert re.search(r"report \d+\.\d{3}s \(initial render and write\)", report)
    assert (fixture / "target.txt").read_text() == "OLD\n"
    if case == "finding":
        assert ref in report
        assert ref in (output / "real-code" / "raw" / "cited.jsonl").read_text()
        assert "evaluator" in report
        assert sorted(
            p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()
        ) == [
            "real-code/raw/cited.jsonl",
            f"real-code/raw/sessions/{COORDINATOR_SESSION_ID}.journal.jsonl",
            "report.md",
        ]
    assert (
        output
        / "real-code"
        / "raw"
        / "sessions"
        / f"{COORDINATOR_SESSION_ID}.journal.jsonl"
    ).is_file()
    assert all(
        SECRET not in path.read_text(errors="replace")
        for path in output.rglob("*")
        if path.is_file()
    )


def test_live_pipeline_dynamic_workers_cost_secret_retention_and_independent_diff(
    tmp_path: Path,
) -> None:
    scenario_dir, blueprints, binary, bridge, fixture = make_inputs(tmp_path)
    output = tmp_path / "output"
    original = (fixture / "target.txt").read_bytes()
    captured: dict = {}
    with (
        patch(
            "jcode_harness_client.runner.launch_harness",
            fake_harness(captured=captured),
        ),
        patch(
            "jcode_harness_client.runner.review_compact", side_effect=clean_review
        ) as judge,
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for("real-code", evaluation_settings()),
            output=output,
            scenario_path=scenario_dir,
            scenario_id=None,
            blueprints_dir=blueprints,
            plugins_dir=None,
            build_provenance={"action": "reused", "fingerprint": "abc"},
        )
    assert result == 0
    assert (fixture / "target.txt").read_bytes() == original
    report = (output / "report.md").read_text()
    assert "acceptance PASS" in report
    assert "$0.011" in report and "2 calls unpriced" in report
    assert re.search(r"build check \d+\.\d{3}s \(evaluator binary check only\)", report)
    assert WORKER_SESSION_ID in report and WORKER_NAME in report
    assert captured["profile"] == "worker_acceptance"
    assert captured["turn_timeout"] == 42
    assert captured["environment"]["JCODE_ACCEPTANCE_API_KEY"] == SECRET
    assert (
        captured["environment"]["JCODE_SWARM_MODEL"] == "worker_acceptance:gpt-4o-mini"
    )
    data = judge.call_args.kwargs["judge_input"]
    assert set(data["blueprints"]) == {"coordinator", WORKER_NAME, UNUSED_WORKER_NAME}
    assert data["scenario"]["judge_criteria"] == [
        "Good coordination uses suitable workers and verifies results."
    ]
    assert sorted(
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file()
    ) == [f"real-code/raw/sessions/{COORDINATOR_SESSION_ID}.journal.jsonl", "report.md"]
    for path in output.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_bytes().decode("utf-8", errors="ignore")


def test_worker_model_overrides_manifest_effective_config_and_preflight_rejects_typos(
    tmp_path: Path,
) -> None:
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    output = tmp_path / "output"
    override = f"worker-model-{uuid4().hex}"
    captured: dict = {}
    with (
        patch(
            "jcode_harness_client.runner.launch_harness",
            fake_harness(captured=captured),
        ),
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for(
                "real-code", evaluation_settings({WORKER_NAME: override})
            ),
            output=output,
            scenario_path=scenario_dir,
            scenario_id="real-code",
            blueprints_dir=blueprints,
            plugins_dir=None,
        )
    assert result == 0
    config = tomllib.loads(captured["config"])
    assert (
        config["agents"]["worker_blueprints"][WORKER_NAME]["model"]
        == "worker_acceptance:" + override
    )

    typo_output = tmp_path / "typo-output"
    typo = f"worker-{uuid4().hex}"
    with (
        patch("jcode_harness_client.runner.launch_harness") as launch,
        pytest.raises(ValueError, match="undiscovered worker"),
    ):
        run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for(
                "real-code", evaluation_settings({typo: override})
            ),
            output=typo_output,
            scenario_path=scenario_dir,
            scenario_id="real-code",
            blueprints_dir=blueprints,
            plugins_dir=None,
        )
    launch.assert_not_called()

    with (
        patch("jcode_harness_client.runner.launch_harness") as launch,
        pytest.raises(ValueError, match="delegation_guard references undiscovered worker"),
    ):
        run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for(
                "real-code", evaluation_settings(guard_blueprints=[typo])
            ),
            output=tmp_path / "guard-typo-output",
            scenario_path=scenario_dir,
            scenario_id="real-code",
            blueprints_dir=blueprints,
            plugins_dir=None,
        )
    launch.assert_not_called()


def test_delegation_guard_env_is_explicit_with_manifest_and_config_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    plugins = make_plugins(tmp_path)
    output = tmp_path / "output"
    captured: dict = {}
    # Personal caller environment must never reach the isolated runtime.
    monkeypatch.setenv("JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS", "routine,personal")
    monkeypatch.setenv("JCODE_DELEGATION_GUARD_THRESHOLD", "1")
    with (
        patch(
            "jcode_harness_client.runner.launch_harness",
            fake_harness(captured=captured),
        ),
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for(
                "real-code",
                evaluation_settings(guard_blueprints=[WORKER_NAME], guard_threshold=1234),
            ),
            output=output,
            scenario_path=scenario_dir,
            scenario_id="real-code",
            blueprints_dir=blueprints,
            plugins_dir=plugins,
        )
    assert result == 0
    assert captured["environment"]["JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS"] == (
        WORKER_NAME
    )
    assert captured["environment"]["JCODE_DELEGATION_GUARD_THRESHOLD"] == "1234"
    assert "post_tool_transform" in captured["config"]
    # Default selection and threshold come from settings, not the env.
    default_output = tmp_path / "default-output"
    default_captured: dict = {}
    with (
        patch(
            "jcode_harness_client.runner.launch_harness",
            fake_harness(captured=default_captured),
        ),
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
    ):
        run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for("real-code", evaluation_settings()),
            output=default_output,
            scenario_path=scenario_dir,
            scenario_id="real-code",
            blueprints_dir=blueprints,
            plugins_dir=plugins,
        )
    assert (
        default_captured["environment"]["JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS"]
        == ""
    )
    assert default_captured["environment"]["JCODE_DELEGATION_GUARD_THRESHOLD"] == (
        "8192"
    )
    # The controller composite is scenario-local and never leaks to real-code.
    assert escalation_controller.STATE_ENV not in default_captured["environment"]
    assert escalation_controller.GUARD_ENV not in default_captured["environment"]
    assert escalation_controller.MODE_ENV not in default_captured["environment"]


def test_delegation_guard_is_not_implied_without_the_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    output = tmp_path / "output"
    captured: dict = {}
    monkeypatch.setenv("JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS", "advanced")
    monkeypatch.setenv("JCODE_DELEGATION_GUARD_THRESHOLD", "7")
    # A controller mode set in the caller's environment is never inherited:
    # the runner always sets the effective mode explicitly per scenario.
    monkeypatch.setenv(escalation_controller.MODE_ENV, "scripted")
    isolated = runtime_environment(tmp_path / "isolated", {})
    assert "JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS" not in isolated
    assert "JCODE_DELEGATION_GUARD_THRESHOLD" not in isolated
    assert escalation_controller.MODE_ENV not in isolated
    with (
        patch(
            "jcode_harness_client.runner.launch_harness",
            fake_harness(captured=captured),
        ),
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for("real-code", evaluation_settings()),
            output=output,
            scenario_path=scenario_dir,
            scenario_id="real-code",
            blueprints_dir=blueprints,
            plugins_dir=None,
        )
    assert result == 0
    assert "JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS" not in captured["environment"]
    assert "JCODE_DELEGATION_GUARD_THRESHOLD" not in captured["environment"]
    assert "hooks" not in tomllib.loads(captured["config"])


def test_acceptance_still_runs_after_coordinator_timeout(tmp_path: Path) -> None:
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    output = tmp_path / "output"
    with (
        patch("jcode_harness_client.runner.launch_harness", fake_harness(timeout=True)),
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply") as user_reply,
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for("real-code", evaluation_settings()),
            output=output,
            scenario_path=scenario_dir,
            scenario_id="real-code",
            blueprints_dir=blueprints,
            plugins_dir=None,
        )
    assert result == 1
    user_reply.assert_not_called()
    report = (output / "report.md").read_text()
    assert "acceptance PASS" in report
    assert "fixture timeout" in report
    assert (
        output
        / "real-code"
        / "raw"
        / "sessions"
        / f"{COORDINATOR_SESSION_ID}.journal.jsonl"
    ).is_file()


def test_simulated_user_approves_and_conversation_continues_to_completion(
    tmp_path: Path,
) -> None:
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    output = tmp_path / "output"
    captured: dict = {}
    replies = [
        SimulatedUserReply(done=False, message="Yes, please proceed."),
        SimulatedUserReply(done=True, message=""),
    ]
    with (
        patch(
            "jcode_harness_client.runner.launch_harness",
            fake_harness(captured=captured),
        ),
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=replies),
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for("real-code", evaluation_settings()),
            output=output,
            scenario_path=scenario_dir,
            scenario_id="real-code",
            blueprints_dir=blueprints,
            plugins_dir=None,
        )
    assert result == 0
    assert captured["prompts"][1] == "Yes, please proceed."
    report = (output / "report.md").read_text()
    assert "acceptance PASS" in report
    assert "coordinator turns" in report and "(2)" in report


def test_simulated_user_turn_cap_still_runs_acceptance(tmp_path: Path) -> None:
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    scenario_file = scenario_dir / "real-code" / "config.toml"
    scenario_file.write_text(
        scenario_file.read_text(encoding="utf-8").replace(
            "max_turns = 8", "max_turns = 1"
        ),
        encoding="utf-8",
    )
    with (
        patch("jcode_harness_client.runner.launch_harness", fake_harness()),
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch(
            "jcode_harness_client.runner.next_user_reply",
            return_value=SimulatedUserReply(done=False, message="Keep going."),
        ),
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for("real-code", evaluation_settings()),
            output=tmp_path / "output",
            scenario_path=scenario_dir,
            scenario_id="real-code",
            blueprints_dir=blueprints,
            plugins_dir=None,
        )
    assert result == 1
    report = (tmp_path / "output" / "report.md").read_text()
    assert "acceptance PASS" in report
    assert "turn cap reached" in report


def test_unknown_scenario_and_nonempty_output_make_no_live_calls(
    tmp_path: Path,
) -> None:
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    (output / "keep").write_text("evidence", encoding="utf-8")
    with patch("jcode_harness_client.runner.launch_harness") as launch:
        with pytest.raises(ValueError, match="overwrite"):
            run(
                binary=binary,
                bridge=bridge,
                settings_by_scenario=settings_for("real-code", evaluation_settings()),
                output=output,
                scenario_path=scenario_dir,
                scenario_id=None,
                blueprints_dir=blueprints,
                plugins_dir=None,
            )
        with pytest.raises(ValueError, match="unknown scenario ID"):
            run(
                binary=binary,
                bridge=bridge,
                settings_by_scenario=settings_for("missing", evaluation_settings()),
                output=tmp_path / "other",
                scenario_path=scenario_dir,
                scenario_id="missing",
                blueprints_dir=blueprints,
                plugins_dir=None,
            )
    launch.assert_not_called()


def test_cli_rejects_removed_cases_flags() -> None:
    with pytest.raises(SystemExit):
        cli_main(
            [
                "run",
                "--binary",
                "a",
                "--bridge",
                "b",
                "--config",
                "c",
                "--cases",
                "d",
                "--blueprints",
                "e",
                "--plugins",
                "f",
                "--output",
                "g",
            ]
        )


def write_escalation_scenario(scenario_dir: Path) -> None:
    escalation_dir = scenario_dir / "escalation-resumption"
    escalation_dir.mkdir()
    shutil.copy2(CONTROLLER_ASSET, escalation_dir / "controller.py")
    (escalation_dir / "config.toml").write_text(
        'id = "escalation-resumption"\n'
        'fixture = "../../fixture"\n'
        'prompt = "Resolve the milestone workflow in the current workspace."\n'
        'accept = ["true"]\n'
        "\n[instrumentation]\n"
        'isolation = "blind"\n'
        'controller = "controller.py"\n'
        'journals = ["events.jsonl", "guard-events.jsonl"]\n'
        "\n[instrumentation.environment]\n"
        'JCODE_ESCALATION_CONTROLLER_STATE_DIR = "{state_dir}"\n'
        'JCODE_ESCALATION_CONTROLLER_WORKSPACE = "{workspace}"\n'
        'JCODE_ESCALATION_CONTROLLER_GUARD = "{guard}"\n'
        'JCODE_ESCALATION_CONTROLLER_GUARD_TIMEOUT_MS = "1000"\n'
        'JCODE_ESCALATION_CONTROLLER_MODE = "blind"\n'
        "\n[judge]\n"
        'criteria = ["Blocked stages are delegated and verified."]\n'
        "\n[simulated_user]\n"
        "max_turns = 8\n"
        'instructions = "Approve reasonable plans and let the agent decide."\n',
        encoding="utf-8",
    )


def test_escalation_resumption_composes_controller_with_actual_guard_entry(
    tmp_path: Path,
) -> None:
    scenario_dir, blueprints, binary, bridge, fixture = make_inputs(tmp_path)
    write_escalation_scenario(scenario_dir)
    plugins = make_plugins(tmp_path)
    output = tmp_path / "output"
    captured: dict = {}
    launch = fake_harness(captured=captured)

    def inspect_launch(**kwargs):
        guard = Path(kwargs["environment"][escalation_controller.GUARD_ENV])
        assert guard.is_file()
        hooks = tomllib.loads(kwargs["config"])["hooks"]
        post = Path(hooks["post_tool_transform"][0])
        pre = Path(hooks["pre_tool_transform"][0])
        guard_wrapper = json.loads(post.with_suffix(".json").read_text())
        rtk_wrapper = json.loads(pre.with_suffix(".json").read_text())
        assert guard_wrapper["command"] == [
            sys.executable,
            str((scenario_dir / "escalation-resumption/controller.py").resolve()),
        ]
        assert rtk_wrapper["command"][0].endswith("rtk-transform")
        return launch(**kwargs)

    with (
        patch(
            "jcode_harness_client.runner.launch_harness",
            inspect_launch,
        ),
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for(
                "escalation-resumption", evaluation_settings()
            ),
            output=output,
            scenario_path=scenario_dir,
            scenario_id="escalation-resumption",
            blueprints_dir=blueprints,
            plugins_dir=plugins,
            runtime_isolation="host",
        )
    assert result == 0
    environment = captured["environment"]
    state_dir = environment[escalation_controller.STATE_ENV]
    workspace_env = environment[escalation_controller.WORKSPACE_ENV]
    guard_env = environment[escalation_controller.GUARD_ENV]
    assert state_dir.endswith("instrumentation/escalation-resumption")
    assert workspace_env.endswith("workspaces/escalation-resumption")
    assert "delegation-guard" in guard_env
    assert environment[escalation_controller.GUARD_TIMEOUT_ENV] == "1000"
    # The effective controller mode is explicit, never inherited or implied.
    assert environment[escalation_controller.MODE_ENV] == "blind"
    assert "acceptance PASS" in (output / "report.md").read_text()


def test_escalation_resumption_requires_the_delegation_guard_plugin(
    tmp_path: Path,
) -> None:
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    write_escalation_scenario(scenario_dir)
    with (
        patch("jcode_harness_client.runner.launch_harness") as launch,
        pytest.raises(ValueError, match="delegation-guard"),
    ):
        run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for(
                "escalation-resumption", evaluation_settings()
            ),
            output=tmp_path / "output",
            scenario_path=scenario_dir,
            scenario_id="escalation-resumption",
            blueprints_dir=blueprints,
            plugins_dir=None,
            runtime_isolation="host",
        )
    launch.assert_not_called()


def test_limits_bound_turns_and_record_provenance(
    tmp_path: Path,
) -> None:
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    output = tmp_path / "output"
    captured: dict = {}
    with (
        patch(
            "jcode_harness_client.runner.launch_harness",
            fake_harness(captured=captured),
        ),
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for(
                "real-code",
                evaluation_settings(
                    limits={
                        "turn_timeout_seconds": 77,
                        "execution_timeout_seconds": 500,
                    }
                ),
            ),
            output=output,
            scenario_path=scenario_dir,
            scenario_id="real-code",
            blueprints_dir=blueprints,
            plugins_dir=None,
        )
    assert result == 0
    assert captured["turn_timeout"] == 77
    # Each turn ran under the explicit 77s budget, below the 500s deadline.
    assert captured["send_timeouts"] == [77]
    assert "acceptance PASS" in (output / "report.md").read_text()


class SteadyClock:
    """Deterministic monotonic clock advancing a fixed step on every call."""

    def __init__(self, step: float) -> None:
        self._step = step
        self.now = 0.0

    def monotonic(self) -> float:
        self.now += self._step
        return self.now


def test_execution_deadline_stops_before_negative_wait_and_preserves_evidence(
    tmp_path: Path,
) -> None:
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    output = tmp_path / "output"
    captured: dict = {}
    clock = SteadyClock(step=0.9)
    with (
        patch(
            "jcode_harness_client.runner.time",
            SimpleNamespace(monotonic=clock.monotonic),
        ),
        patch(
            "jcode_harness_client.runner.launch_harness",
            fake_harness(captured=captured),
        ),
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch(
            "jcode_harness_client.runner.next_user_reply",
            side_effect=lambda **kwargs: SimulatedUserReply(
                done=False, message="please continue"
            ),
        ),
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for(
                "real-code",
                evaluation_settings(
                    limits={
                        "turn_timeout_seconds": 42,
                        "execution_timeout_seconds": 1,
                    }
                ),
            ),
            output=output,
            scenario_path=scenario_dir,
            scenario_id="real-code",
            blueprints_dir=blueprints,
            plugins_dir=None,
        )
    assert result == 1
    # Turn 0 ran with a positive deadline-bounded budget; turn 1 never
    # started, so no unconditional negative wait was ever passed.
    assert len(captured["send_timeouts"]) == 1
    assert 0 < captured["send_timeouts"][0] <= 1
    report = (output / "report.md").read_text()
    assert "execution deadline of 1s exceeded" in report
    assert "acceptance PASS" in report


def test_escalation_resumption_limits_apply_from_its_own_settings_file(
    tmp_path: Path,
) -> None:
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    write_escalation_scenario(scenario_dir)
    plugins = make_plugins(tmp_path)
    output = tmp_path / "output"
    captured: dict = {}
    with (
        patch(
            "jcode_harness_client.runner.launch_harness",
            fake_harness(captured=captured),
        ),
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for(
                "escalation-resumption",
                evaluation_settings(
                    limits={
                        "turn_timeout_seconds": 1200,
                        "execution_timeout_seconds": 1800,
                    }
                ),
            ),
            output=output,
            scenario_path=scenario_dir,
            scenario_id="escalation-resumption",
            blueprints_dir=blueprints,
            plugins_dir=plugins,
            runtime_isolation="host",
        )
    assert result == 0
    # The scenario's own settings file carries the approved budgets.
    assert captured["turn_timeout"] == 1200
    # Worker waits happen inside a turn, so min(1200, remaining 1800s) applies.
    assert captured["send_timeouts"] == [1200]
    assert "acceptance PASS" in (output / "report.md").read_text()


def fake_blind_harness(captured: dict | None = None):
    """Reuse the host fake against the staged public workspace tree."""
    inner = fake_harness(captured=captured)

    def launch(**kwargs: Any):
        kwargs["workspace"] = kwargs["staging"].workspace
        return inner(**kwargs)

    return launch


def test_blind_config_references_staged_blueprints_and_hooks(tmp_path: Path) -> None:
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    write_escalation_scenario(scenario_dir)
    plugins = make_plugins(tmp_path)
    expected = {path.name: path.read_bytes() for path in blueprints.glob("*.md")}
    inspected = False
    fake = fake_blind_harness()

    def inspect_launch(**kwargs: Any):
        nonlocal inspected
        staging = kwargs["staging"]
        config = tomllib.loads(kwargs["config"])
        directory = Path(config["agents"]["worker_blueprints_dir"])
        assert directory.is_relative_to(staging.config)
        assert {
            path.name: path.read_bytes() for path in directory.iterdir()
        } == expected
        for hook in ("pre_tool_transform", "post_tool_transform"):
            for command in config["hooks"][hook]:
                executable = Path(shlex.split(command)[0])
                assert executable.is_relative_to(staging.bin)
                assert executable.is_file()
        assert not any(
            value.startswith("/home/") for value in kwargs["environment"].values()
        )
        assert escalation_controller.STATE_ENV not in kwargs["environment"]
        assert kwargs["broker_operations"]["delegation-guard"].serialized
        inspected = True
        return fake(**kwargs)

    with (
        patch("jcode_harness_client.runner.blind_preflight"),
        patch("jcode_harness_client.runner.launch_blind_harness", inspect_launch),
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
    ):
        assert (
            run(
                binary=binary,
                bridge=bridge,
                settings_by_scenario=settings_for(
                    "escalation-resumption", evaluation_settings()
                ),
                output=tmp_path / "output",
                scenario_path=scenario_dir,
                scenario_id="escalation-resumption",
                blueprints_dir=blueprints,
                plugins_dir=plugins,
                runtime_isolation="blind",
            )
            == 0
        )
    assert inspected


def test_blind_runtime_isolation_wires_proxy_hooks_and_private_broker_ops(
    tmp_path: Path,
) -> None:
    """The default-blind scenario runtime stages a public tree and keeps
    controller/guard environment private to host broker operations."""
    if shutil.which("bwrap") is None:
        pytest.skip("bwrap is not installed")
    scenario_dir, blueprints, binary, bridge, fixture = make_inputs(tmp_path)
    plugins = make_plugins(tmp_path)
    output = tmp_path / "output"
    captured: dict = {}
    with (
        patch(
            "jcode_harness_client.runner.launch_blind_harness",
            fake_blind_harness(captured),
        ),
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for("real-code", evaluation_settings()),
            output=output,
            scenario_path=scenario_dir,
            scenario_id="real-code",
            blueprints_dir=blueprints,
            plugins_dir=plugins,
            runtime_isolation="blind",
        )
    assert result == 0
    staging = captured["staging"]
    assert str(staging.root).startswith("/var/tmp/")
    # The public staging tree is removed once acceptance and retention are
    # done: no evaluation copy lingers in a public location.
    assert not staging.root.exists()
    # The sandbox environment carries only explicit agent routing: no
    # controller, guard, or private cargo-target variables cross over.
    environment = captured["environment"]
    assert environment["JCODE_ACCEPTANCE_API_KEY"] == SECRET
    assert escalation_controller.STATE_ENV not in environment
    assert "JCODE_DELEGATION_GUARD_THRESHOLD" not in environment
    assert "CARGO_TARGET_DIR" not in environment
    # Private hook wrappers run only as host broker operations.
    operations = captured["broker_operations"]
    assert set(operations) == {"rtk", "delegation-guard"}
    assert operations["rtk"].command[0].endswith("rtk-trace")
    assert operations["delegation-guard"].serialized is True
    assert operations["delegation-guard"].command[0].endswith("delegation-guard-trace")
    assert "JCODE_DELEGATION_GUARD_THRESHOLD" in operations["delegation-guard"].env
    # The native hook config inside the sandbox names only the proxy.
    hooks = tomllib.loads(captured["config"])["hooks"]
    proxy = str(staging.bin / "hook-proxy")
    assert hooks["pre_tool_transform"] == [shlex.join([proxy, "rtk"])]
    assert hooks["post_tool_transform"] == [shlex.join([proxy, "delegation-guard"])]
    assert (fixture / "target.txt").read_bytes() == b"OLD\n"
    assert "acceptance PASS" in (output / "report.md").read_text()


def test_blind_preflight_failure_is_fail_closed(tmp_path: Path) -> None:
    """A missing bwrap aborts the run instead of falling back to host."""
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    if shutil.which("bwrap") is not None:
        with patch(
            "jcode_harness_client.runner.blind_preflight",
            side_effect=RuntimeError("bwrap is required for the blind runtime"),
        ):
            with pytest.raises(RuntimeError, match="bwrap"):
                run(
                    binary=binary,
                    bridge=bridge,
                    settings_by_scenario=settings_for(
                        "real-code", evaluation_settings()
                    ),
                    output=tmp_path / "output",
                    scenario_path=scenario_dir,
                    scenario_id="real-code",
                    blueprints_dir=blueprints,
                    plugins_dir=None,
                    runtime_isolation="blind",
                )
    else:
        with pytest.raises(RuntimeError, match="bwrap"):
            run(
                binary=binary,
                bridge=bridge,
                settings_by_scenario=settings_for("real-code", evaluation_settings()),
                output=tmp_path / "output",
                scenario_path=scenario_dir,
                scenario_id="real-code",
                blueprints_dir=blueprints,
                plugins_dir=None,
                runtime_isolation="blind",
            )


def test_runtime_isolation_defaults_come_from_metadata(tmp_path) -> None:
    scenario_dir, *_ = make_inputs(tmp_path)
    write_escalation_scenario(scenario_dir)
    private = load_scenarios(scenario_dir, "escalation-resumption")[0]
    ordinary = load_scenarios(scenario_dir, "real-code")[0]
    assert DEFAULT_RUNTIME_ISOLATION == "host"
    assert resolve_runtime_isolation(None, private) == "blind"
    assert resolve_runtime_isolation(None, ordinary) == "host"
    assert resolve_runtime_isolation(None) == "host"
    assert resolve_runtime_isolation("host", private) == "host"
    assert resolve_runtime_isolation("blind", ordinary) == "blind"
    with pytest.raises(ValueError, match="runtime_isolation"):
        resolve_runtime_isolation("sandbox", ordinary)


def test_non_escalation_scenario_never_uses_bwrap(tmp_path: Path) -> None:
    """A non-escalation scenario stays on the host runtime by default: the
    runner never preflights or launches the blind sandbox, even on machines
    where bwrap is installed."""
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    plugins = make_plugins(tmp_path)
    output = tmp_path / "output"
    captured: dict = {}
    with (
        patch(
            "jcode_harness_client.runner.launch_harness",
            fake_harness(captured=captured),
        ),
        patch("jcode_harness_client.runner.blind_preflight") as preflight,
        patch("jcode_harness_client.runner.launch_blind_harness") as blind_launch,
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for("real-code", evaluation_settings()),
            output=output,
            scenario_path=scenario_dir,
            scenario_id="real-code",
            blueprints_dir=blueprints,
            plugins_dir=plugins,
        )
    assert result == 0
    preflight.assert_not_called()
    blind_launch.assert_not_called()


def test_escalation_resumption_defaults_to_blind_boundary(tmp_path: Path) -> None:
    """The escalation-resumption scenario opts into the blind bwrap boundary
    by default: public staging, proxy-only hooks, host-private controller
    environment, and per-scenario blind provenance in the manifest."""
    if shutil.which("bwrap") is None:
        pytest.skip("bwrap is not installed")
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    write_escalation_scenario(scenario_dir)
    plugins = make_plugins(tmp_path)
    output = tmp_path / "output"
    captured: dict = {}
    with (
        patch(
            "jcode_harness_client.runner.launch_blind_harness",
            fake_blind_harness(captured),
        ),
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for(
                "escalation-resumption", evaluation_settings()
            ),
            output=output,
            scenario_path=scenario_dir,
            scenario_id="escalation-resumption",
            blueprints_dir=blueprints,
            plugins_dir=plugins,
        )
    assert result == 0
    staging = captured["staging"]
    assert str(staging.root).startswith("/var/tmp/")
    # Normal completion removes the public staging tree; the hard-failure
    # finally below is only a backstop.
    assert not staging.root.exists()
    environment = captured["environment"]
    assert escalation_controller.MODE_ENV not in environment
    assert escalation_controller.STATE_ENV not in environment
    operations = captured["broker_operations"]
    controller_env = operations["delegation-guard"].env
    assert controller_env[escalation_controller.MODE_ENV] == "blind"
    assert controller_env[escalation_controller.WORKSPACE_ENV] == str(staging.workspace)
    assert "acceptance PASS" in (output / "report.md").read_text()


def test_explicit_host_override_pins_host_for_escalation(tmp_path: Path) -> None:
    """The explicit host override keeps the escalation scenario on the host
    runtime and is recorded as the override in the manifest; it is a
    test/internal knob and does not weaken the scenario's blind default."""
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    write_escalation_scenario(scenario_dir)
    plugins = make_plugins(tmp_path)
    output = tmp_path / "output"
    captured: dict = {}
    with (
        patch(
            "jcode_harness_client.runner.launch_harness",
            fake_harness(captured=captured),
        ),
        patch("jcode_harness_client.runner.blind_preflight") as preflight,
        patch("jcode_harness_client.runner.launch_blind_harness") as blind_launch,
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for(
                "escalation-resumption", evaluation_settings()
            ),
            output=output,
            scenario_path=scenario_dir,
            scenario_id="escalation-resumption",
            blueprints_dir=blueprints,
            plugins_dir=plugins,
            runtime_isolation="host",
        )
    assert result == 0
    preflight.assert_not_called()
    blind_launch.assert_not_called()


def test_escalation_blind_default_is_fail_closed(tmp_path: Path) -> None:
    """A blind preflight failure aborts the default escalation-resumption
    run instead of degrading to a host launch."""
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    write_escalation_scenario(scenario_dir)
    with (
        patch(
            "jcode_harness_client.runner.blind_preflight",
            side_effect=RuntimeError("bwrap is required for the blind runtime"),
        ),
        patch("jcode_harness_client.runner.launch_harness") as launch,
    ):
        with pytest.raises(RuntimeError, match="bwrap"):
            run(
                binary=binary,
                bridge=bridge,
                settings_by_scenario=settings_for(
                    "escalation-resumption", evaluation_settings()
                ),
                output=tmp_path / "output",
                scenario_path=scenario_dir,
                scenario_id="escalation-resumption",
                blueprints_dir=blueprints,
                plugins_dir=None,
            )
    launch.assert_not_called()


def test_blind_staging_root_is_removed_on_staging_hard_failure(
    tmp_path: Path,
) -> None:
    """A hard failure while preparing the public staging root must not leave
    it behind: the per-scenario finally removes exactly the created stage."""
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    created_roots: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args: Any, **kwargs: Any) -> str:
        path = real_mkdtemp(*args, **kwargs)
        created_roots.append(Path(path))
        return path

    with (
        patch("tempfile.mkdtemp", side_effect=recording_mkdtemp),
        patch(
            "jcode_harness_client.runner.blind_preflight",
            return_value="/usr/bin/bwrap",
        ),
        patch(
            "jcode_harness_client.blind_runtime.plan_staging",
            side_effect=RuntimeError("staging prep boom"),
        ),
    ):
        with pytest.raises(RuntimeError, match="staging prep boom"):
            run(
                binary=binary,
                bridge=bridge,
                settings_by_scenario=settings_for("real-code", evaluation_settings()),
                output=tmp_path / "output",
                scenario_path=scenario_dir,
                scenario_id="real-code",
                blueprints_dir=blueprints,
                plugins_dir=None,
                runtime_isolation="blind",
            )
    staged_roots = [
        root for root in created_roots if root.name.startswith("jcode-blind-")
    ]
    assert len(staged_roots) == 1
    assert not staged_roots[0].exists()


def test_blind_staging_root_is_removed_on_post_launch_hard_failure(
    tmp_path: Path,
) -> None:
    """A hard failure after the sandbox launch is backstopped by the same
    finally: the public staging root is removed and nothing else is."""
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    plugins = make_plugins(tmp_path)
    captured: dict = {}
    created_roots: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def recording_mkdtemp(*args: Any, **kwargs: Any) -> str:
        path = real_mkdtemp(*args, **kwargs)
        created_roots.append(Path(path))
        return path

    with (
        patch("tempfile.mkdtemp", side_effect=recording_mkdtemp),
        patch(
            "jcode_harness_client.runner.blind_preflight",
            return_value="/usr/bin/bwrap",
        ),
        patch(
            "jcode_harness_client.runner.launch_blind_harness",
            fake_blind_harness(captured),
        ),
        patch(
            "jcode_harness_client.runner.run_acceptance",
            side_effect=RuntimeError("acceptance boom"),
        ),
    ):
        with pytest.raises(RuntimeError, match="acceptance boom"):
            run(
                binary=binary,
                bridge=bridge,
                settings_by_scenario=settings_for("real-code", evaluation_settings()),
                output=tmp_path / "output",
                scenario_path=scenario_dir,
                scenario_id="real-code",
                blueprints_dir=blueprints,
                plugins_dir=plugins,
                runtime_isolation="blind",
            )
    staged_roots = [
        root for root in created_roots if root.name.startswith("jcode-blind-")
    ]
    assert len(staged_roots) == 1
    assert staged_roots[0] == captured["staging"].root
    assert not staged_roots[0].exists()


def test_each_scenario_uses_its_own_settings_in_one_run(tmp_path: Path) -> None:
    scenario_dir, blueprints, binary, bridge, fixture = make_inputs(tmp_path)
    accept = [
        "python3",
        "-c",
        "from pathlib import Path; assert Path('target.txt').read_text() == 'NEW\\n'",
    ]
    second_dir = scenario_dir / "second-code"
    second_dir.mkdir()
    (second_dir / "config.toml").write_text(
        'id = "second-code"\n'
        'fixture = "../../fixture"\n'
        'prompt = "Implement the outcome in the current workspace."\n'
        f"accept = {json.dumps(accept)}\n"
        "\n[judge]\n"
        'criteria = ["Good coordination uses suitable workers and verifies results."]\n'
        "\n[simulated_user]\n"
        "max_turns = 8\n"
        'instructions = "Approve reasonable plans and let the agent decide."\n',
        encoding="utf-8",
    )
    output = tmp_path / "output"
    first = parse_settings(
        "[provider]\n"
        'type = "openai-compatible"\n'
        'url = "https://endpoint.invalid/v1"\n'
        'model = "gpt-4o"\n'
        'api_key = "test-secret-never-real"\n'
        "[models]\n"
        'coordinator = "gpt-4o"\n'
        'swarm = "gpt-4o-mini"\n'
        'judge = "gpt-4o"\n'
        'simulator = "gpt-4o-mini"\n'
        "[limits]\n"
        "turn_timeout_seconds = 42\n"
        "execution_timeout_seconds = 500\n"
        "[effort]\n"
        'coordinator = "high"\n'
        'swarm = "low"\n'
        "[worker_effort]\n"
        f'{json.dumps(WORKER_NAME)} = "medium"\n'
        "[delegation_guard]\n"
        f'worker_blueprints = [{json.dumps(WORKER_NAME)}]\n'
    )
    second = parse_settings(
        "[provider]\n"
        'type = "openai-compatible"\n'
        'url = "https://endpoint.invalid/v1"\n'
        'model = "gpt-4o"\n'
        'api_key = "test-secret-never-real"\n'
        "[models]\n"
        'coordinator = "gpt-4o"\n'
        'swarm = "gpt-4o"\n'
        'judge = "gpt-4o"\n'
        'simulator = "gpt-4o"\n'
        "[limits]\n"
        "turn_timeout_seconds = 99\n"
        "[delegation_guard]\n"
        "worker_blueprints = []\n"
        "threshold = 4096\n"
    )
    launches = []
    fake = fake_harness()

    def capture_launch(**kwargs):
        launches.append(kwargs)
        return fake(**kwargs)

    with (
        patch("jcode_harness_client.runner.launch_harness", capture_launch),
        patch("jcode_harness_client.runner.review_compact", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario={
                "real-code": ScenarioSettings(
                    scenario_id="real-code",
                    settings=first,
                ),
                "second-code": ScenarioSettings(
                    scenario_id="second-code",
                    settings=second,
                ),
            },
            output=output,
            scenario_path=scenario_dir,
            scenario_id=None,
            blueprints_dir=blueprints,
            plugins_dir=None,
        )
    assert result == 0
    assert len(launches) == 2
    first_config, second_config = (tomllib.loads(item["config"]) for item in launches)
    assert first_config["agents"]["swarm_root_effort"] == "high"
    assert first_config["agents"]["swarm_effort"] == "low"
    assert (
        first_config["agents"]["worker_blueprints"][WORKER_NAME]["effort"] == "medium"
    )
    assert "swarm_root_effort" not in second_config["agents"]
    assert "swarm_effort" not in second_config["agents"]
    assert [item["turn_timeout"] for item in launches] == [42, 99]
    report = (output / "report.md").read_text()
    assert "# real-code — acceptance PASS" in report
    assert "# second-code — acceptance PASS" in report


def test_cli_config_preflight_validates_all_before_any_launch(
    tmp_path: Path,
) -> None:
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    accept = [
        "python3",
        "-c",
        "from pathlib import Path; assert Path('target.txt').read_text() == 'NEW\\n'",
    ]
    second_dir = scenario_dir / "second-code"
    second_dir.mkdir()
    (second_dir / "config.toml").write_text(
        'id = "second-code"\n'
        'fixture = "../../fixture"\n'
        'prompt = "Implement the outcome in the current workspace."\n'
        f"accept = {json.dumps(accept)}\n"
        "\n[judge]\n"
        'criteria = ["Good coordination uses suitable workers and verifies results."]\n'
        "\n[simulated_user]\n"
        "max_turns = 8\n"
        'instructions = "Approve reasonable plans and let the agent decide."\n'
        "\n[settings.provider]\n"
        'type = "not-a-provider"\n',
        encoding="utf-8",
    )
    config = tmp_path / "config.toml"
    config.write_text(
        "[provider]\n"
        'type = "openai-compatible"\n'
        'url = "https://endpoint.invalid/v1"\n'
        'model = "gpt-4o"\n'
        'api_key = "test-secret-never-real"\n',
        encoding="utf-8",
    )
    config.chmod(0o600)
    # second-code carries an invalid public override: preflight must fail
    # before any launch.
    with patch("jcode_harness_client.runner.launch_harness") as launch:
        status = cli_main(
            [
                "run",
                "--binary",
                str(binary),
                "--bridge",
                str(bridge),
                "--config",
                str(config),
                "--scenarios",
                str(scenario_dir),
                "--blueprints",
                str(blueprints),
                "--plugins",
                "none",
                "--output",
                str(tmp_path / "output"),
            ]
        )
    assert status == 1
    launch.assert_not_called()
    # No legacy single-file fallback: the removed settings flags are rejected.
    with pytest.raises(SystemExit):
        cli_main(
            [
                "run",
                "--binary",
                str(binary),
                "--bridge",
                str(bridge),
                "--settings-dir",
                str(config),
                "--scenarios",
                str(scenario_dir),
                "--blueprints",
                str(blueprints),
                "--plugins",
                "none",
                "--output",
                str(tmp_path / "other"),
            ]
        )
