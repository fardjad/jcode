from __future__ import annotations

import json
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

from jcode_harness_client import escalation_controller
from jcode_harness_client.cli import main as cli_main
from jcode_harness_client.client import HarnessTimeoutError
from jcode_harness_client.inventory import file_sha256
from jcode_harness_client.llm_backend import CallUsageRecord
from jcode_harness_client.runner import (
    BLIND_DEFAULT_SCENARIOS,
    DEFAULT_RUNTIME_ISOLATION,
    METRIC_ANALYZERS,
    resolve_runtime_isolation,
    run,
)
from jcode_harness_client.runtime import runtime_environment
from jcode_harness_client.settings import ScenarioSettings, parse_settings
from jcode_harness_client.user_actor import SimulatedUserReply

SECRET = "test-secret-never-real"
WORKER_NAME = f"worker-{uuid4().hex}"
UNUSED_WORKER_NAME = f"worker-{uuid4().hex}"
WORKER_SESSION_ID = f"session_{uuid4().hex}"
COORDINATOR_SESSION_ID = f"session_{uuid4().hex}"


@pytest.fixture(autouse=True)
def _host_runtime_isolation():
    """Pin these runner tests to the host runtime by default.

    Production scopes the blind bwrap boundary to the escalation-resumption
    scenario only; every other scenario stays host. The explicit
    ``runtime_isolation`` override is a test/internal knob, never a live
    configuration option, so this fixture pins the scenario defaults to
    host and each test opts into blind explicitly where needed.
    """
    with (
        patch("jcode_harness_client.runner.DEFAULT_RUNTIME_ISOLATION", "host"),
        patch("jcode_harness_client.runner.BLIND_DEFAULT_SCENARIOS", ()),
    ):
        yield


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
judge_max_calls = 8
judge_page_bytes = 100000
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
judge_max_calls = 8
judge_page_bytes = 100000
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
        'metrics = ["tool_health"]\n'
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
    return (
        {
            "summary": "Clean diagnostic review.",
            "findings": [],
            "review": "clean",
            "review_complete": True,
            "reviewed_sessions": [COORDINATOR_SESSION_ID, WORKER_SESSION_ID],
            "unexercised_workers": [],
        },
        {
            "complete": True,
            "evidence_gaps": [],
            "judge_calls": 1,
            "judge_max_calls": 8,
        },
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
            "jcode_harness_client.runner.review_evidence", side_effect=clean_review
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
    run_summary = json.loads((output / "summary.json").read_text())
    summary = run_summary["scenarios"][0]
    assert summary["execution"]["status"] == "completed"
    assert summary["acceptance"]["status"] == "pass"
    assert summary["evidence"]["status"] == "complete"
    assert summary["workers"]["exercised"] == [WORKER_NAME]
    assert summary["workers"]["not_exercised"] == [UNUSED_WORKER_NAME]
    assert summary["review"] == "clean"
    assert "task_compliance" not in summary
    assert summary["cost_usd"] == 0.011
    assert summary["unpriced_calls"] == 2
    assert run_summary["cost_usd"] == summary["cost_usd"]
    assert summary["usage"]["input_tokens"] == 485
    assert summary["usage"]["by_role"][WORKER_NAME]["calls"] == 1
    assert summary["usage"]["by_model"]["gpt-4o-mini"]["calls"] == 2
    assert summary["usage"]["agent"]["calls"] == 2
    assert summary["usage"]["evaluation_overhead"]["calls"] == 2
    assert summary["usage"]["cache_read_tokens"] == 260
    assert summary["metrics"]["tool_health"]["status"] == "healthy"
    assert summary["simulated_user"] == {
        "calls": 1,
        "max_turns": 8,
        "termination": "simulated-user-done",
    }
    assert "+NEW" in (output / "real-code" / "changes.patch").read_text()
    assert (
        "future_native_field" in (output / "real-code" / "transcript.jsonl").read_text()
    )
    assert captured["profile"] == "worker_acceptance"
    assert captured["turn_timeout"] == 42
    assert captured["environment"]["JCODE_ACCEPTANCE_API_KEY"] == SECRET
    assert captured["environment"]["JCODE_SWARM_MODEL"] == (
        "worker_acceptance:gpt-4o-mini"
    )
    judged_blueprints = judge.call_args.kwargs["blueprints"]
    assert {item["name"] for item in judged_blueprints} == {
        "coordinator",
        WORKER_NAME,
        UNUSED_WORKER_NAME,
    }
    assert judge.call_args.kwargs["scenario"]["judge_criteria"] == [
        "Good coordination uses suitable workers and verifies results."
    ]
    manifest = json.loads((output / "manifest.json").read_text())
    scenario_settings = manifest["settings_by_scenario"]["real-code"]
    assert scenario_settings["settings"]["models"]["simulator"] == "gpt-4o-mini"
    assert scenario_settings["settings"]["worker_models"] == {}
    assert len(scenario_settings["effective_sha256"]) == 64
    assert scenario_settings["defaults"] is None
    assert "api_key" not in json.dumps(scenario_settings)
    assert SECRET not in json.dumps(scenario_settings)
    assert manifest["build"]["fingerprint"] == "abc"
    assert manifest["scenarios"][0]["id"] == "real-code"
    assert manifest["evaluator"]["files"]["jcode_harness_client/runner.py"]
    assert len(manifest["evaluator"]["sha256"]) == 64
    for path in output.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_bytes().decode("utf-8", errors="ignore")


def test_worker_model_overrides_manifest_effective_config_and_preflight_rejects_typos(
    tmp_path: Path,
) -> None:
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    output = tmp_path / "output"
    override = f"worker-model-{uuid4().hex}"
    with (
        patch("jcode_harness_client.runner.launch_harness", fake_harness()),
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
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
    manifest = json.loads((output / "manifest.json").read_text())
    assert (
        manifest["settings_by_scenario"]["real-code"]["settings"]["worker_models"][
            WORKER_NAME
        ]
        == override
    )
    effective = json.loads(
        (output / "real-code" / "effective" / "config.json").read_text()
    )
    assert effective["agents"]["worker_blueprints"][WORKER_NAME]["model"] == (
        "worker_acceptance:" + override
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
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
    ):
        result = run(
            binary=binary,
            bridge=bridge,
            settings_by_scenario=settings_for(
                "real-code",
                evaluation_settings(guard_blueprints=["expert"], guard_threshold=1234),
            ),
            output=output,
            scenario_path=scenario_dir,
            scenario_id="real-code",
            blueprints_dir=blueprints,
            plugins_dir=plugins,
        )
    assert result == 0
    assert captured["environment"]["JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS"] == (
        "expert"
    )
    assert captured["environment"]["JCODE_DELEGATION_GUARD_THRESHOLD"] == "1234"
    assert "post_tool_transform" in captured["config"]
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["settings_by_scenario"]["real-code"]["settings"][
        "delegation_guard"
    ] == {
        "worker_blueprints": ["expert"],
        "threshold": 1234,
    }
    effective = json.loads(
        (output / "real-code" / "effective" / "config.json").read_text()
    )
    assert effective["delegation_guard"] == {
        "active": True,
        "worker_blueprints": ["expert"],
        "threshold": 1234,
        "environment": {
            "JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS": "expert",
            "JCODE_DELEGATION_GUARD_THRESHOLD": "1234",
        },
    }
    # Default opt-in selection and threshold come from settings, not the env.
    default_output = tmp_path / "default-output"
    default_captured: dict = {}
    with (
        patch(
            "jcode_harness_client.runner.launch_harness",
            fake_harness(captured=default_captured),
        ),
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
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
        == "advanced,expert"
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
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
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
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["settings_by_scenario"]["real-code"]["settings"][
        "delegation_guard"
    ] == {
        "worker_blueprints": ["advanced", "expert"],
        "threshold": 8192,
    }
    effective = json.loads(
        (output / "real-code" / "effective" / "config.json").read_text()
    )
    assert effective["delegation_guard"] == {
        "active": False,
        "worker_blueprints": ["advanced", "expert"],
        "threshold": 8192,
    }


def test_acceptance_still_runs_after_coordinator_timeout(tmp_path: Path) -> None:
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    output = tmp_path / "output"
    with (
        patch("jcode_harness_client.runner.launch_harness", fake_harness(timeout=True)),
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
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
    scenario_output = output / "real-code"
    summary = json.loads((scenario_output / "scenario-summary.json").read_text())
    assert summary["execution"]["status"] == "timeout"
    assert summary["acceptance"]["status"] == "pass"
    assert summary["acceptance"]["exit_status"] == 0
    assert "fixture timeout" in summary["execution"]["error"]
    assert (scenario_output / "acceptance.log").is_file()
    assert "+NEW" in (scenario_output / "changes.patch").read_text()


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
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
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
    scenario_output = output / "real-code"
    conversation = json.loads((scenario_output / "conversation.json").read_text())
    assert [item["origin"] for item in conversation] == [
        "scenario",
        "coordinator",
        "simulated-user",
        "coordinator",
        "simulated-user",
    ]
    assert conversation[-1]["done"] is True
    assert conversation[-1]["sent"] is False
    summary = json.loads((scenario_output / "scenario-summary.json").read_text())
    assert summary["execution"]["coordinator_turns"] == 2
    assert summary["simulated_user"]["calls"] == 2
    assert summary["simulated_user"]["termination"] == "simulated-user-done"
    assert summary["acceptance"]["status"] == "pass"
    assert summary["evidence"]["status"] == "complete"


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
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
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
    summary = json.loads(
        (tmp_path / "output" / "real-code" / "scenario-summary.json").read_text()
    )
    assert summary["execution"]["status"] == "runtime error"
    assert summary["simulated_user"]["termination"] == "turn-cap"
    assert summary["acceptance"]["status"] == "pass"


def test_future_metric_registration_needs_no_scenario_schema_change(
    tmp_path: Path,
) -> None:
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    scenario_file = scenario_dir / "real-code" / "config.toml"
    scenario_file.write_text(
        scenario_file.read_text(encoding="utf-8").replace(
            'metrics = ["tool_health"]', 'metrics = ["custom_metric"]'
        ),
        encoding="utf-8",
    )

    def custom_metric(*args: Any, **kwargs: Any):
        return {"status": "healthy", "observed": 1}, [
            {"id": "custom-metric-evidence", "kind": "metric-evidence", "data": {}}
        ]

    with (
        patch.dict(METRIC_ANALYZERS, {"custom_metric": custom_metric}),
        patch("jcode_harness_client.runner.launch_harness", fake_harness()),
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
        patch("jcode_harness_client.runner.next_user_reply", side_effect=done_user),
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
    assert result == 0
    summary = json.loads((tmp_path / "output" / "summary.json").read_text())[
        "scenarios"
    ][0]
    assert summary["metrics"]["custom_metric"] == {"status": "healthy", "observed": 1}
    assert summary["tool_health"] == "not requested"
    assert not (tmp_path / "output" / "real-code" / "tool-health.json").exists()


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
        scenario_file = scenario_dir / "real-code" / "config.toml"
        scenario_file.write_text(
            scenario_file.read_text(encoding="utf-8").replace(
                'metrics = ["tool_health"]', 'metrics = ["future_metric"]'
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="unknown evaluation metrics"):
            run(
                binary=binary,
                bridge=bridge,
                settings_by_scenario=settings_for("real-code", evaluation_settings()),
                output=tmp_path / "unknown-metric",
                scenario_path=scenario_dir,
                scenario_id="real-code",
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
    (escalation_dir / "config.toml").write_text(
        'id = "escalation-resumption"\n'
        'fixture = "../../fixture"\n'
        'prompt = "Resolve the milestone workflow in the current workspace."\n'
        'accept = ["true"]\n'
        'metrics = ["tool_health"]\n'
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
    with (
        patch(
            "jcode_harness_client.runner.launch_harness",
            fake_harness(captured=captured),
        ),
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
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
    environment = captured["environment"]
    state_dir = environment[escalation_controller.STATE_ENV]
    workspace_env = environment[escalation_controller.WORKSPACE_ENV]
    guard_env = environment[escalation_controller.GUARD_ENV]
    assert state_dir.endswith("instrumentation/escalation-resumption")
    assert workspace_env.endswith("workspaces/escalation-resumption")
    assert Path(guard_env).is_file()
    assert "delegation-guard" in guard_env
    assert environment[escalation_controller.GUARD_TIMEOUT_ENV] == "1000"
    # The effective controller mode is explicit, never inherited or implied.
    assert environment[escalation_controller.MODE_ENV] == "blind"
    # Hook command records expose the composite to analyzers.
    hooks_json = json.loads(
        (output / "escalation-resumption" / "effective" / "hooks.json").read_text()
    )
    guard_record = next(
        command
        for command in hooks_json["commands"]
        if command["plugin"] == "delegation-guard"
    )
    rtk_record = next(
        command for command in hooks_json["commands"] if command["plugin"] == "rtk"
    )
    controller = Path(escalation_controller.__file__).resolve()
    assert guard_record["composite"] is True
    assert guard_record["controller_module"] == str(controller)
    assert guard_record["guard_executable"] == guard_env
    assert guard_record["production_command"] == [sys.executable, str(controller)]
    assert "delegation-guard-transform" not in guard_record["production_command"]
    assert guard_record["traced_command"][0].endswith("delegation-guard-trace")
    assert "composite" not in rtk_record
    assert rtk_record["production_command"][0].endswith("rtk-transform")
    # Exactly one entry per hook: the composite replaced the guard, and the
    # RTK pre-transform stays untouched.
    hooks = tomllib.loads(captured["config"])["hooks"]
    assert hooks["post_tool_transform"] == guard_record["traced_command"]
    assert hooks["pre_tool_transform"] == rtk_record["traced_command"]
    assert hooks["post_tool_transform_timeout_ms"] == 2000
    # Runtime settings snapshot provenance records the composite selection.
    effective = json.loads(
        (output / "escalation-resumption" / "effective" / "config.json").read_text()
    )
    provenance = effective["escalation_controller"]
    assert provenance["controller_module"] == str(controller)
    assert provenance["controller_sha256"] == file_sha256(controller)
    assert provenance["mode"] == "blind"
    assert provenance["state_dir"] == state_dir
    assert provenance["events_path"] == str(Path(state_dir) / "events.jsonl")
    assert provenance["guard_events_path"] == str(
        Path(state_dir) / "guard-events.jsonl"
    )
    assert provenance["workspace"] == workspace_env
    assert provenance["guard_executable"] == guard_env
    assert provenance["threshold"] == 8192
    assert provenance["environment"][escalation_controller.STATE_ENV] == state_dir
    assert provenance["environment"][escalation_controller.MODE_ENV] == "blind"


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
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
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
    effective = json.loads(
        (output / "real-code" / "effective" / "config.json").read_text()
    )
    assert effective["timeouts"] == {
        "scenario": "real-code",
        "turn_timeout_seconds": 77,
        "execution_timeout_seconds": 500,
        "sources": {
            "turn_timeout": "limits",
            "execution_timeout": "limits",
        },
    }
    manifest = json.loads((output / "manifest.json").read_text())
    scenario_settings = manifest["settings_by_scenario"]["real-code"]
    assert scenario_settings["settings"]["limits"]["turn_timeout_seconds"] == 77
    assert scenario_settings["settings"]["limits"]["execution_timeout_seconds"] == 500
    summary = json.loads((output / "real-code" / "scenario-summary.json").read_text())
    assert summary["execution"]["applied_timeouts"] == {
        "turn_timeout_seconds": 77,
        "execution_timeout_seconds": 500,
        "sources": {
            "turn_timeout": "limits",
            "execution_timeout": "limits",
        },
    }
    assert summary["execution"]["deadline_exceeded"] is False


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
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
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
    summary = json.loads((output / "real-code" / "scenario-summary.json").read_text())
    execution = summary["execution"]
    assert execution["status"] == "timeout"
    assert execution["termination"] == "execution-deadline"
    assert execution["deadline_exceeded"] is True
    assert "execution deadline of 1s exceeded" in execution["error"]
    assert execution["applied_timeouts"]["execution_timeout_seconds"] == 1
    # Timeout preserved evidence and never retried: acceptance still ran.
    assert summary["acceptance"]["status"] == "pass"
    assert "+NEW" in (output / "real-code" / "changes.patch").read_text()
    conversation = json.loads((output / "real-code" / "conversation.json").read_text())
    assert len(conversation) >= 2


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
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
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
        )
    assert result == 0
    # The scenario's own settings file carries the approved budgets.
    assert captured["turn_timeout"] == 1200
    # Worker waits happen inside a turn, so min(1200, remaining 1800s) applies.
    assert captured["send_timeouts"] == [1200]
    effective = json.loads(
        (output / "escalation-resumption" / "effective" / "config.json").read_text()
    )
    assert effective["timeouts"] == {
        "scenario": "escalation-resumption",
        "turn_timeout_seconds": 1200,
        "execution_timeout_seconds": 1800,
        "sources": {
            "turn_timeout": "limits",
            "execution_timeout": "limits",
        },
    }
    manifest = json.loads((output / "manifest.json").read_text())
    escalation_settings = manifest["settings_by_scenario"]["escalation-resumption"]
    assert escalation_settings["settings"]["limits"] == {
        **escalation_settings["settings"]["limits"],
        "turn_timeout_seconds": 1200,
        "execution_timeout_seconds": 1800,
    }
    summary = json.loads(
        (output / "escalation-resumption" / "scenario-summary.json").read_text()
    )
    assert summary["execution"]["applied_timeouts"] == {
        "turn_timeout_seconds": 1200,
        "execution_timeout_seconds": 1800,
        "sources": {
            "turn_timeout": "limits",
            "execution_timeout": "limits",
        },
    }
    assert summary["execution"]["deadline_exceeded"] is False


def test_builtin_metric_registry_resolves_scenario_metrics() -> None:
    assert "escalation_resumption" in METRIC_ANALYZERS
    assert METRIC_ANALYZERS["escalation_resumption"] is not None
    assert {"tool_health", "coordination"} <= set(METRIC_ANALYZERS)


def fake_blind_harness(captured: dict | None = None):
    """Reuse the host fake against the staged public workspace tree."""
    inner = fake_harness(captured=captured)

    def launch(**kwargs: Any):
        kwargs["workspace"] = kwargs["staging"].workspace
        return inner(**kwargs)

    return launch


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
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
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
    hooks_json = json.loads(
        (output / "real-code" / "effective" / "hooks.json").read_text()
    )
    assert all(
        record["blind_command"] == [proxy, record["plugin"]]
        for record in hooks_json["commands"]
    )
    effective = json.loads(
        (output / "real-code" / "effective" / "config.json").read_text()
    )
    isolation = effective["filesystem_isolation"]
    assert isolation["mode"] == "blind"
    assert isolation["runtime"] == "bwrap"
    assert isolation["staging_root"] == str(staging.root)
    assert isolation["hook_transport"] == "host-broker"
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["runtime_isolation"] == "blind"
    # Acceptance and diffs followed the staged public workspace during the
    # run (the fake wrote NEW there); the fixture copy stays pristine.
    assert (fixture / "target.txt").read_bytes() == b"OLD\n"
    scenario_output = output / "real-code"
    assert "+NEW" in (scenario_output / "changes.patch").read_text()


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


def test_runtime_isolation_defaults_are_scenario_scoped() -> None:
    """Only the escalation-resumption scenario defaults to the blind bwrap
    boundary; every other scenario keeps the host default, and the explicit
    override is a validated test/internal knob that wins for any scenario."""
    with (
        patch(
            "jcode_harness_client.runner.DEFAULT_RUNTIME_ISOLATION",
            DEFAULT_RUNTIME_ISOLATION,
        ),
        patch(
            "jcode_harness_client.runner.BLIND_DEFAULT_SCENARIOS",
            BLIND_DEFAULT_SCENARIOS,
        ),
    ):
        assert DEFAULT_RUNTIME_ISOLATION == "host"
        assert escalation_controller.SCENARIO_ID in BLIND_DEFAULT_SCENARIOS
        assert (
            resolve_runtime_isolation(None, escalation_controller.SCENARIO_ID)
            == "blind"
        )
        assert resolve_runtime_isolation(None, "real-code") == "host"
        assert resolve_runtime_isolation(None) == "host"
        assert (
            resolve_runtime_isolation("host", escalation_controller.SCENARIO_ID)
            == "host"
        )
        assert resolve_runtime_isolation("blind", "real-code") == "blind"
        with pytest.raises(ValueError, match="runtime_isolation"):
            resolve_runtime_isolation("sandbox", "real-code")


def test_non_escalation_scenario_never_uses_bwrap(tmp_path: Path) -> None:
    """A non-escalation scenario stays on the host runtime by default: the
    runner never preflights or launches the blind sandbox, even on machines
    where bwrap is installed."""
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    plugins = make_plugins(tmp_path)
    output = tmp_path / "output"
    captured: dict = {}
    with (
        patch("jcode_harness_client.runner.launch_harness", fake_harness(captured)),
        patch("jcode_harness_client.runner.blind_preflight") as preflight,
        patch("jcode_harness_client.runner.launch_blind_harness") as blind_launch,
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
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
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["runtime_isolation"] is None
    assert manifest["runtime_isolation_by_scenario"] == {"real-code": "host"}
    assert manifest["results"][0]["runtime_isolation"] == "host"


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
            "jcode_harness_client.runner.BLIND_DEFAULT_SCENARIOS",
            BLIND_DEFAULT_SCENARIOS,
        ),
        patch(
            "jcode_harness_client.runner.launch_blind_harness",
            fake_blind_harness(captured),
        ),
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
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
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["runtime_isolation"] is None
    assert manifest["runtime_isolation_by_scenario"] == {
        "escalation-resumption": "blind"
    }
    effective = json.loads(
        (output / "escalation-resumption" / "effective" / "config.json").read_text()
    )
    assert effective["filesystem_isolation"]["mode"] == "blind"
    provenance = effective["escalation_controller"]
    assert provenance["mode"] == "blind"
    assert provenance["workspace"] == str(staging.workspace)
    assert "+NEW" in ((output / "escalation-resumption" / "changes.patch").read_text())


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
            "jcode_harness_client.runner.BLIND_DEFAULT_SCENARIOS",
            BLIND_DEFAULT_SCENARIOS,
        ),
        patch("jcode_harness_client.runner.launch_harness", fake_harness(captured)),
        patch("jcode_harness_client.runner.blind_preflight") as preflight,
        patch("jcode_harness_client.runner.launch_blind_harness") as blind_launch,
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
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
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["runtime_isolation"] == "host"
    assert manifest["runtime_isolation_by_scenario"] == {
        "escalation-resumption": "host"
    }


def test_escalation_blind_default_is_fail_closed(tmp_path: Path) -> None:
    """A blind preflight failure aborts the default escalation-resumption
    run instead of degrading to a host launch."""
    scenario_dir, blueprints, binary, bridge, _ = make_inputs(tmp_path)
    write_escalation_scenario(scenario_dir)
    with (
        patch(
            "jcode_harness_client.runner.BLIND_DEFAULT_SCENARIOS",
            BLIND_DEFAULT_SCENARIOS,
        ),
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
        'metrics = ["tool_health"]\n'
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
        'worker_blueprints = ["expert"]\n'
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
    with (
        patch("jcode_harness_client.runner.launch_harness", fake_harness()),
        patch("jcode_harness_client.runner.review_evidence", side_effect=clean_review),
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
    manifest = json.loads((output / "manifest.json").read_text())
    assert set(manifest["settings_by_scenario"]) == {"real-code", "second-code"}
    assert (
        manifest["settings_by_scenario"]["real-code"]["effective_sha256"]
        != manifest["settings_by_scenario"]["second-code"]["effective_sha256"]
    )
    first_effective = json.loads(
        (output / "real-code" / "effective" / "config.json").read_text()
    )
    second_effective = json.loads(
        (output / "second-code" / "effective" / "config.json").read_text()
    )
    # Efforts route into the native [agents] section per scenario.
    assert first_effective["agents"]["swarm_root_effort"] == "high"
    assert first_effective["agents"]["swarm_effort"] == "low"
    assert (
        first_effective["agents"]["worker_blueprints"][WORKER_NAME]["effort"]
        == "medium"
    )
    assert first_effective["agents"]["worker_blueprints"][UNUSED_WORKER_NAME].get(
        "effort"
    ) in (None, "low")
    assert "swarm_root_effort" not in second_effective["agents"]
    assert "swarm_effort" not in second_effective["agents"]
    # Models, limits, guard, and timeouts stay per scenario.
    assert first_effective["timeouts"]["turn_timeout_seconds"] == 42
    assert first_effective["timeouts"]["execution_timeout_seconds"] == 500
    assert second_effective["timeouts"]["turn_timeout_seconds"] == 99
    assert second_effective["timeouts"]["execution_timeout_seconds"] is None
    assert (
        first_effective["providers"]["worker_acceptance"]["default_model"] == "gpt-4o"
    )
    assert manifest["settings_by_scenario"]["real-code"]["settings"][
        "delegation_guard"
    ]["worker_blueprints"] == ["expert"]
    assert (
        manifest["settings_by_scenario"]["second-code"]["settings"]["delegation_guard"][
            "worker_blueprints"
        ]
        == []
    )
    for scenario_id in ("real-code", "second-code"):
        summary = json.loads(
            (output / scenario_id / "scenario-summary.json").read_text()
        )
        assert summary["acceptance"]["status"] == "pass"


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
        'metrics = ["tool_health"]\n'
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
