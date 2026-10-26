from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from jcode_harness_client.scenarios import (
    JudgeScenario,
    Scenario,
    SimulatedUserScenario,
)
from jcode_harness_client.settings import parse_settings
from jcode_harness_client.user_actor import next_user_reply


def scenario(tmp_path: Path) -> Scenario:
    source = tmp_path / "scenario.yaml"
    source.write_text("scenario", encoding="utf-8")
    return Scenario(
        id="scenario-1",
        fixture=tmp_path,
        prompt="Implement the outcome.",
        accept=("python3", "accept.py"),
        metrics=("tool_health",),
        judge=JudgeScenario(("Good workers make bounded progress.",)),
        simulated_user=SimulatedUserScenario(
            max_turns=8,
            instructions="Approve reasonable plans and encourage agent decisions.",
        ),
        source=source,
        source_sha256="abc123",
    )


def settings():
    return parse_settings(
        """[provider]
type = "openai-compatible"
url = "https://endpoint.invalid/v1"
model = "fallback"
api_key = "secret-token"
[models]
simulator = "user-model"
"""
    )


def test_scenario_instructions_drive_llm_user_actor(tmp_path: Path) -> None:
    conversation = [
        {"role": "user", "content": "Implement the outcome."},
        {"role": "assistant", "content": "Here is my plan. Shall I proceed?"},
    ]
    with patch(
        "jcode_harness_client.user_actor.run_structured",
        return_value={"done": False, "message": "Yes, proceed with your plan."},
    ) as structured:
        reply = next_user_reply(
            settings=settings(),
            scenario=scenario(tmp_path),
            conversation=conversation,
            output=tmp_path / "turn-0",
        )
    assert reply.done is False
    assert reply.message == "Yes, proceed with your plan."
    kwargs = structured.call_args.kwargs
    assert kwargs["role"] == "simulator"
    assert "Approve reasonable plans and encourage agent decisions." in kwargs["system"]
    assert "explicitly announces" in kwargs["system"]
    assert "Implement the outcome." in str(kwargs["user_prompt"])
    assert kwargs["cache_key"].startswith("jcode-eval:scenario-1:simulator:")
    assert kwargs["max_tokens"] == 512


def test_simulator_cache_key_is_stable_across_turns_and_uses_cache_point(
    tmp_path: Path,
) -> None:
    scen = scenario(tmp_path)
    cfg = settings()
    first_conversation = [
        {"role": "user", "content": "Implement the outcome."},
        {"role": "assistant", "content": "Plan ready."},
    ]
    second_conversation = [
        *first_conversation,
        {"role": "user", "content": "Proceed."},
        {"role": "assistant", "content": "Done."},
    ]
    with patch(
        "jcode_harness_client.user_actor.run_structured",
        side_effect=[
            {"done": False, "message": "Proceed."},
            {"done": True, "message": ""},
        ],
    ) as structured:
        first = next_user_reply(
            settings=cfg,
            scenario=scen,
            conversation=first_conversation,
            output=tmp_path / "turn-0",
        )
        second = next_user_reply(
            settings=cfg,
            scenario=scen,
            conversation=second_conversation,
            output=tmp_path / "turn-1",
        )
    assert first.done is False
    assert second.done is True
    first_call = structured.call_args_list[0].kwargs
    second_call = structured.call_args_list[1].kwargs
    assert first_call["cache_key"] == second_call["cache_key"]
    assert isinstance(second_call["user_prompt"], list)
    assert len(second_call["user_prompt"]) == 3


@pytest.mark.parametrize(
    "value",
    [
        {"done": False, "message": ""},
        {"done": "yes", "message": "continue"},
        {"done": False, "message": "continue", "extra": True},
        {"done": False},
    ],
)
def test_invalid_simulated_user_replies_are_rejected(
    tmp_path: Path, value: dict
) -> None:
    with patch("jcode_harness_client.user_actor.run_structured", return_value=value):
        with pytest.raises(RuntimeError, match="invalid simulated-user"):
            next_user_reply(
                settings=settings(),
                scenario=scenario(tmp_path),
                conversation=[],
                output=tmp_path / "turn-0",
            )
