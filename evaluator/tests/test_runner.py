from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from jcode_harness_client.cli import main as cli_main
from jcode_harness_client.client import HarnessTimeoutError
from jcode_harness_client.llm_backend import CallUsageRecord
from jcode_harness_client.runner import METRIC_ANALYZERS, run
from jcode_harness_client.settings import parse_settings
from jcode_harness_client.user_actor import SimulatedUserReply

SECRET = "test-secret-never-real"


def evaluation_settings():
    return parse_settings(
        f"""[provider]
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
    )


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
    (scenario_dir / "real.yaml").write_text(
        "id: real-code\nfixture: ../fixture\n"
        "prompt: Implement the outcome in the current workspace.\n"
        f"accept: {json.dumps(accept)}\n"
        "metrics: [tool_health]\n"
        "judge:\n"
        "  criteria: [Good coordination uses suitable workers and verifies results.]\n"
        "simulated_user:\n"
        "  max_turns: 8\n"
        "  instructions: Approve reasonable plans and let the agent decide.\n",
        encoding="utf-8",
    )
    blueprints = root / "blueprints"
    blueprints.mkdir()
    (blueprints / "coordinator.md").write_text(
        "---\nenabled-tools: []\ndisabled-tools: []\n---\nCoordinate.\n",
        encoding="utf-8",
    )
    for name in ("custom-worker", "unused-worker"):
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
                parent_id = "session_parent"
                worker_id = "session_custom"
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
                        "name": "custom-worker",
                        "enabled_tools": ["read"],
                    },
                    "agent_label": "animal-name-is-not-a-role",
                    "swarm_status": {"state": "completed"},
                    "edit_stats": {"files": 1},
                    "messages": [native_message("worker-user", "user", "assignment")],
                }

                native_turns: list[str] = []

                class Session:
                    session_id = parent_id

                    def send_message(self, prompt: str, **options: Any):
                        turn_index = len(native_turns)
                        native_turns.append(prompt)
                        if captured is not None:
                            captured.setdefault("prompts", []).append(prompt)
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
                                    "worker_blueprint_policy": {
                                        "name": "custom-worker"
                                    },
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
            )
        )
    return (
        {
            "summary": "Clean diagnostic review.",
            "findings": [],
            "review": "clean",
            "review_complete": True,
            "reviewed_sessions": ["session_parent", "session_custom"],
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
            settings=evaluation_settings(),
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
    assert summary["workers"]["exercised"] == ["custom-worker"]
    assert summary["workers"]["not_exercised"] == ["unused-worker"]
    assert summary["review"] == "clean"
    assert "task_compliance" not in summary
    assert summary["cost_usd"] is not None and summary["cost_usd"] > 0.011
    assert run_summary["cost_usd"] == summary["cost_usd"]
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
        "custom-worker",
        "unused-worker",
    }
    assert judge.call_args.kwargs["scenario"]["judge_criteria"] == [
        "Good coordination uses suitable workers and verifies results."
    ]
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["models"]["simulator"] == "gpt-4o-mini"
    assert manifest["build"]["fingerprint"] == "abc"
    assert manifest["scenarios"][0]["id"] == "real-code"
    assert manifest["evaluator"]["files"]["jcode_harness_client/runner.py"]
    assert len(manifest["evaluator"]["sha256"]) == 64
    for path in output.rglob("*"):
        if path.is_file():
            assert SECRET not in path.read_bytes().decode("utf-8", errors="ignore")


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
            settings=evaluation_settings(),
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
            settings=evaluation_settings(),
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
    scenario_file = scenario_dir / "real.yaml"
    scenario_file.write_text(
        scenario_file.read_text(encoding="utf-8").replace(
            "max_turns: 8", "max_turns: 1"
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
            settings=evaluation_settings(),
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
    scenario_file = scenario_dir / "real.yaml"
    scenario_file.write_text(
        scenario_file.read_text(encoding="utf-8").replace(
            "metrics: [tool_health]", "metrics: [custom_metric]"
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
            settings=evaluation_settings(),
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
                settings=evaluation_settings(),
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
                settings=evaluation_settings(),
                output=tmp_path / "other",
                scenario_path=scenario_dir,
                scenario_id="missing",
                blueprints_dir=blueprints,
                plugins_dir=None,
            )
        scenario_file = scenario_dir / "real.yaml"
        scenario_file.write_text(
            scenario_file.read_text(encoding="utf-8").replace(
                "metrics: [tool_health]", "metrics: [future_metric]"
            ),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="unknown evaluation metrics"):
            run(
                binary=binary,
                bridge=bridge,
                settings=evaluation_settings(),
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
                "--settings",
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
