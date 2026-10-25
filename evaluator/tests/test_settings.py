"""Security and explicit routing tests for private evaluation settings."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

import pytest

from jcode_harness_client.settings import (
    EXAMPLE_NAME,
    KEY_ENV,
    initialize,
    load_settings,
    parse_settings,
    profile_config,
)


def fixture_text(
    provider: str = "openai-compatible", url: str = "https://endpoint.invalid/v1"
) -> str:
    return (
        "[provider]\ntype="
        + json.dumps(provider)
        + "\nurl="
        + json.dumps(url)
        + '\nmodel="fixture-model"\napi_key="test-secret-never-real"\n'
    )


def test_four_role_models_limits_and_fallback() -> None:
    legacy = parse_settings(fixture_text())
    for role in ("coordinator", "swarm", "judge", "simulator"):
        assert legacy.model_for(role) == "fixture-model"
    explicit = parse_settings(
        fixture_text()
        + """\n[models]
judge="judge"
simulator="user-actor"
coordinator="coord"
swarm="worker"
[limits]
turn_timeout_seconds=42
swarm_timeout_minutes=2
acceptance_timeout_seconds=84
judge_max_calls=32
judge_page_bytes=123456
"""
    )
    assert explicit.model_for("coordinator") == "coord"
    assert explicit.model_for("swarm") == "worker"
    assert explicit.model_for("judge") == "judge"
    assert explicit.model_for("simulator") == "user-actor"
    assert explicit.limits.turn_timeout_seconds == 42
    assert explicit.limits.acceptance_timeout_seconds == 84
    assert explicit.limits.judge_max_calls == 32
    assert explicit.limits.judge_page_bytes == 123456
    assert (
        tomllib.loads(profile_config(explicit))["providers"]["worker_acceptance"][
            "default_model"
        ]
        == "coord"
    )
    for extra in (
        '\n[models]\nunknown="x"',
        '\n[models]\njudge=""',
        "\n[limits]\njudge_max_calls=0",
        "\n[limits]\nsimulator_max_turns=true",
        "\n[limits]\nacceptance_timeout_seconds=0",
    ):
        with pytest.raises(ValueError):
            parse_settings(fixture_text() + extra)


def test_protocol_url_and_secret_free_profile() -> None:
    for provider in ("openai-compatible", "anthropic-compatible", "openrouter"):
        settings = parse_settings(fixture_text(provider))
        config = tomllib.loads(profile_config(settings))["providers"][
            "worker_acceptance"
        ]
        expected = "open-router" if provider == "openrouter" else provider
        assert config["type"] == expected
        assert config["base_url"] == settings.url
        assert config["api_key_env"] == KEY_ENV
        assert settings.api_key not in repr(settings)
        assert settings.api_key not in profile_config(settings)


def test_unsafe_url_and_secret_quoting_errors_are_sanitized() -> None:
    for url in (
        "http://remote.invalid",
        "https://user:secret@endpoint.invalid/v1",
        "https://endpoint.invalid/v1?key=secret",
        "https://endpoint.invalid/#secret",
    ):
        with pytest.raises(ValueError) as error:
            parse_settings(fixture_text(url=url))
        assert "test-secret-never-real" not in str(error.value)
        assert "user:secret" not in str(error.value)
    with pytest.raises(ValueError) as error:
        parse_settings('[provider]\napi_key="test-secret-never-real')
    assert "test-secret-never-real" not in str(error.value)
    assert (
        parse_settings(fixture_text(url="http://127.0.0.1:1234/v1")).url
        == "http://127.0.0.1:1234/v1"
    )


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX permissions")
def test_private_file_permissions_symlink_and_no_overwrite(tmp_path: Path) -> None:
    (tmp_path / EXAMPLE_NAME).write_text(fixture_text())
    path = initialize(tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_settings(path).model == "fixture-model"
    with pytest.raises(ValueError):
        initialize(tmp_path)
    path.chmod(0o644)
    with pytest.raises(ValueError):
        load_settings(path)
    path.chmod(0o600)
    link = tmp_path / "linked.toml"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        load_settings(link)


def test_example_has_scenario_configured_user_actor_and_no_opt_in_flags() -> None:
    example = (
        Path(__file__).resolve().parents[2] / "workers" / ".eval-settings.example.toml"
    ).read_text(encoding="utf-8")
    assert "scenario YAML" in example
    assert "--judge" not in example
    parsed = tomllib.loads(example)
    assert set(parsed["models"]) == {"coordinator", "swarm", "judge", "simulator"}
    assert parsed["limits"]["judge_max_calls"] > 1
    assert parsed["limits"]["judge_page_bytes"] > 0
