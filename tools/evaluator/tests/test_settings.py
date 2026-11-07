"""Security and explicit routing tests for private evaluation settings."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from uuid import uuid4

import pytest

from jcode_harness_client.scenarios import load_scenarios
from jcode_harness_client.settings import (
    EFFORTS,
    KEY_ENV,
    initialize,
    load_scenario_settings,
    load_settings,
    parse_settings,
    profile_config,
    validate_scenario_overrides,
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
    legacy_settings = parse_settings(fixture_text())
    assert legacy_settings.worker_models == {}
    for role in ("coordinator", "swarm", "judge", "simulator"):
        assert legacy_settings.model_for(role) == "fixture-model"
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
    worker_name = f"worker-{uuid4().hex}"
    worker_settings = parse_settings(
        fixture_text()
        + f'\n[worker_models]\n"{worker_name}" = "specialized-worker-model"\n'
    )
    unconfigured_worker_name = f"worker-{uuid4().hex}"
    assert worker_settings.model_for_worker(worker_name) == "specialized-worker-model"
    assert worker_settings.model_for_worker(unconfigured_worker_name) == "fixture-model"
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
        f'\n[worker_models]\n"worker-{uuid4().hex}"=""',
        '\n[worker_models]\ncoordinator="reserved-model"',
        f'\n[worker_models]\n"worker-{uuid4().hex}"=" model-with-padding "',
        "\n[limits]\njudge_max_calls=0",
        "\n[limits]\nsimulator_max_turns=true",
        "\n[limits]\nacceptance_timeout_seconds=0",
    ):
        with pytest.raises(ValueError):
            parse_settings(fixture_text() + extra)


def test_delegation_guard_selection_defaults_empty_and_validation() -> None:
    default_settings = parse_settings(fixture_text())
    assert default_settings.delegation_guard.worker_blueprints == (
        "advanced",
        "expert",
    )
    assert default_settings.delegation_guard.threshold == 8192
    assert default_settings.delegation_guard_environment() == {
        "JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS": "advanced,expert",
        "JCODE_DELEGATION_GUARD_THRESHOLD": "8192",
    }
    explicit = parse_settings(
        fixture_text()
        + '\n[delegation_guard]\nworker_blueprints = ["expert", "advanced"]\n'
        + "threshold = 4096"
    )
    assert explicit.delegation_guard.worker_blueprints == ("expert", "advanced")
    assert explicit.delegation_guard.threshold == 4096
    assert explicit.delegation_guard_environment() == {
        "JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS": "expert,advanced",
        "JCODE_DELEGATION_GUARD_THRESHOLD": "4096",
    }
    disabled = parse_settings(
        fixture_text() + "\n[delegation_guard]\nworker_blueprints = []"
    )
    assert disabled.delegation_guard.worker_blueprints == ()
    assert (
        disabled.delegation_guard_environment()[
            "JCODE_DELEGATION_GUARD_WORKER_BLUEPRINTS"
        ]
        == ""
    )
    assert disabled.delegation_guard.threshold == 8192
    zero = parse_settings(fixture_text() + "\n[delegation_guard]\nthreshold = 0")
    assert zero.delegation_guard.threshold == 0
    for extra in (
        '\n[delegation_guard]\nworker_blueprints = ["routine"]',
        '\n[delegation_guard]\nworker_blueprints = ["advanced", "custom"]',
        '\n[delegation_guard]\nworker_blueprints = ["advanced", "unknown"]',
        '\n[delegation_guard]\nworker_blueprints = "advanced"',
        "\n[delegation_guard]\nworker_blueprints = [1]",
        '\n[delegation_guard]\nworker_blueprints = [" advanced"]',
        "\n[delegation_guard]\nthreshold = -1",
        '\n[delegation_guard]\nthreshold = "8192"',
        "\n[delegation_guard]\nthreshold = true",
        "\n[delegation_guard]\nunknown_key = 1",
        '\n[delegation_guard]\nworker_blueprints = ["advanced"]\nthreshold = 2.5',
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


def test_timeouts_come_from_each_settings_limits_table() -> None:
    defaults = parse_settings(fixture_text())
    plain = defaults.timeouts()
    assert plain.turn_timeout_seconds == 300
    assert plain.execution_timeout_seconds is None
    assert plain.turn_timeout_source == "limits"
    assert plain.execution_timeout_source == "unbounded"
    bounded = parse_settings(
        fixture_text()
        + "\n[limits]\nturn_timeout_seconds = 1200\n"
        + "execution_timeout_seconds = 1800\n"
    )
    escalated = bounded.timeouts()
    assert escalated.turn_timeout_seconds == 1200
    assert escalated.execution_timeout_seconds == 1800
    assert escalated.turn_timeout_source == "limits"
    assert escalated.execution_timeout_source == "limits"
    for extra in (
        "\n[limits]\nexecution_timeout_seconds = true",
        "\n[limits]\nexecution_timeout_seconds = -1",
        "\n[limits]\nexecution_timeout_seconds = 0",
        '\n[limits]\nexecution_timeout_seconds = "300"',
        "\n[limits]\nexecution_timeout_seconds = 2.5",
        "\n[limits]\nunknown_limit = 5",
    ):
        with pytest.raises(ValueError):
            parse_settings(fixture_text() + extra)


def test_effort_tables_roles_workers_and_validation() -> None:
    default_settings = parse_settings(fixture_text())
    for role in ("coordinator", "swarm", "judge", "simulator"):
        assert default_settings.effort_for(role) is None
    assert default_settings.effort_for_worker("worker-any") is None
    explicit = parse_settings(
        fixture_text()
        + '\n[effort]\ncoordinator = "high"\nswarm = "low"\n'
        + 'judge = "medium"\nsimulator = "low"\n'
        + '\n[worker_effort]\n"worker-special" = "high"\n'
    )
    assert explicit.effort_for("coordinator") == "high"
    assert explicit.effort_for("swarm") == "low"
    assert explicit.effort_for("judge") == "medium"
    assert explicit.effort_for("simulator") == "low"
    # Worker override first, then the swarm fallback.
    assert explicit.effort_for_worker("worker-special") == "high"
    assert explicit.effort_for_worker("worker-other") == "low"
    assert set(EFFORTS) == {"low", "medium", "high"}
    for extra in (
        '\n[effort]\ncoordinator = "xhigh"',
        '\n[effort]\ncoordinator = "swarm"',
        '\n[effort]\ncoordinator = "LOW"',
        '\n[effort]\nunknown_role = "low"',
        "\n[effort]\ncoordinator = 2",
        '\n[worker_effort]\ncoordinator = "low"',
        '\n[worker_effort]\n"worker-x" = "none"',
        '\n[worker_effort]\n" padded " = "low"',
        '\n[worker_effort]\n"worker-x" = "low"\ncoordinator = "low"',
        "\n[effort]\ncoordinator = null",
    ):
        with pytest.raises(ValueError):
            parse_settings(fixture_text() + extra)


def test_effort_model_settings_are_sanitized_in_manifest() -> None:
    explicit = parse_settings(
        fixture_text()
        + '\n[effort]\ncoordinator = "high"\nswarm = "low"\n'
        + '\n[worker_effort]\n"worker-special" = "medium"\n'
        + '\n[worker_models]\n"worker-special" = "specialized"\n'
        + "\n[limits]\nexecution_timeout_seconds = 1800\n"
    )
    manifest = explicit.sanitized_manifest()
    assert manifest["effort"] == {
        "coordinator": "high",
        "swarm": "low",
        "judge": None,
        "simulator": None,
    }
    assert manifest["worker_effort"] == {"worker-special": "medium"}
    assert manifest["limits"]["execution_timeout_seconds"] == 1800
    assert "api_key" not in json.dumps(manifest)
    assert "test-secret-never-real" not in json.dumps(manifest)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX permissions")
def test_load_scenario_settings_preflight_unknown_and_traversal(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    # Shared config is required; a scenario without a settings table
    # inherits everything from it.
    config.write_text(fixture_text(), encoding="utf-8")
    config.chmod(0o600)
    loaded = load_scenario_settings(
        config,
        ["alpha", "beta"],
        ["alpha", "beta"],
        {"alpha": {}, "beta": {"models": {"coordinator": "beta-coordinator"}}},
    )
    assert set(loaded) == {"alpha", "beta"}
    assert loaded["alpha"].settings.model_for("coordinator") == "fixture-model"
    assert loaded["beta"].settings.model_for("coordinator") == "beta-coordinator"
    for entry in loaded.values():
        manifest = entry.manifest()
        assert manifest["defaults"]["source"] == "config.toml"
        assert len(manifest["defaults"]["sha256"]) == 64
        assert len(manifest["effective_sha256"]) == 64
        assert "api_key" not in json.dumps(manifest)
        assert "test-secret-never-real" not in json.dumps(manifest)
    # Each scenario loads independently: one selected scenario works while
    # the other stays unloaded, with or without a settings table.
    single = load_scenario_settings(
        config, ["alpha"], ["alpha", "beta"], {"alpha": None}
    )
    assert set(single) == {"alpha"}
    assert single["alpha"].settings.model == "fixture-model"
    with pytest.raises(ValueError, match="non-selected scenario"):
        load_scenario_settings(config, ["alpha"], ["alpha", "beta"], {"gamma": {}})
    with pytest.raises(ValueError, match="must be discovered"):
        load_scenario_settings(config, ["alpha", "gamma"], ["alpha", "beta"], {})
    # Only the exact config file is read: a missing or symlinked config is
    # a clear preflight error with no directory glob fallback.
    with pytest.raises(ValueError, match="missing required settings config"):
        load_scenario_settings(
            tmp_path / "missing.toml", ["alpha"], ["alpha"], {"alpha": {}}
        )
    with pytest.raises(ValueError, match="path separators or traversal"):
        load_scenario_settings(config, ["../escape"], ["alpha", "beta"], {})
    with pytest.raises(ValueError, match="path separators or traversal"):
        load_scenario_settings(config, [".hidden"], ["alpha", "beta"], {})


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
    config = tmp_path / "config.toml"
    example = tmp_path / "config.toml.example"
    example.write_text(fixture_text())
    path = initialize(config)
    assert path == config
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_settings(path).model == "fixture-model"
    with pytest.raises(ValueError, match="refusing to overwrite"):
        initialize(config)
    with pytest.raises(ValueError, match="template"):
        initialize(tmp_path / "other.toml")
    path.chmod(0o644)
    with pytest.raises(ValueError):
        load_settings(path)
    path.chmod(0o600)
    link = tmp_path / "linked.toml"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        load_settings(link)


def test_examples_are_credential_free_and_show_defaults_inheritance() -> None:
    workers = Path(__file__).resolve().parents[3] / "customizations" / "workers"
    evals = workers / "evals"
    example = evals / "config.toml.example"
    scenario_ids = {
        path.parent.name for path in (evals / "scenarios").glob("*/config.toml")
    }
    assert scenario_ids
    # Only the shared config example ships, and live overrides live in the
    # public scenario config.toml settings tables.
    defaults = tomllib.loads(example.read_text(encoding="utf-8"))
    assert defaults["provider"]["api_key"] == "REPLACE_WITH_YOUR_API_KEY"
    assert "--judge" not in example.read_text(encoding="utf-8")
    # Usual routing: Sol coordinates, judges, and simulates; Luna swarms;
    # GLM and Opus run advanced and expert workers.
    assert defaults["models"] == {
        "coordinator": "sol",
        "swarm": "luna",
        "judge": "sol",
        "simulator": "sol",
    }
    assert defaults["worker_models"] == {
        "routine": "luna",
        "advanced": "glm",
        "expert": "opus",
    }
    assert defaults["limits"]["turn_timeout_seconds"] == 300
    assert "execution_timeout_seconds" not in defaults["limits"]
    assert defaults["limits"]["judge_max_calls"] > 1
    assert defaults["limits"]["judge_page_bytes"] > 0
    guard = defaults["delegation_guard"]
    assert set(guard["worker_blueprints"]) == {"advanced", "expert"}
    assert guard["threshold"] == 8192
    # Advanced and expert workers stay at medium effort in the defaults.
    assert defaults["worker_effort"] == {"advanced": "medium", "expert": "medium"}
    scenarios = {
        scenario.id: scenario
        for scenario in load_scenarios(workers / "evals" / "scenarios")
    }
    assert set(scenarios) == scenario_ids
    escalation = scenarios["escalation-resumption"].settings
    # Approved escalation routing: GLM coordinates, Luna runs every worker,
    # and only the escalation budgets differ from the shared defaults.
    assert escalation == {
        "models": {"coordinator": "z-ai/glm-5.3"},
        "worker_models": {
            "advanced": "openai/gpt-6-luna",
            "expert": "openai/gpt-6-luna",
        },
        "limits": {
            "turn_timeout_seconds": 1200,
            "execution_timeout_seconds": 1800,
        },
    }
    # The usual scenarios inherit everything from the shared defaults.
    for scenario_id in scenario_ids - {"escalation-resumption"}:
        assert scenarios[scenario_id].settings == {}
    assert not (workers / ".eval-settings.example.toml").exists()


DEFAULTS_FIXTURE = (
    fixture_text()
    + """
[models]
coordinator = "defaults-coordinator"
swarm = "defaults-swarm"
judge = "defaults-judge"
simulator = "defaults-simulator"
[worker_models]
routine = "defaults-routine"
advanced = "defaults-advanced"
[worker_effort]
advanced = "medium"
expert = "medium"
[delegation_guard]
worker_blueprints = ["advanced", "expert"]
threshold = 8192
"""
)


def _write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX permissions")
def test_defaults_merge_keywise_with_scenario_overrides(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    _write_private(config, DEFAULTS_FIXTURE)
    # Partial provider (model only), per-key model override, worker_models
    # key-wise merge, and an explicit empty guard list that replaces defaults.
    alpha_overrides = {
        "provider": {"model": "alpha-model"},
        "models": {"judge": "alpha-judge"},
        "worker_models": {"routine": "alpha-routine"},
        "delegation_guard": {"worker_blueprints": []},
    }
    # A scenario without a settings mapping inherits all the defaults.
    loaded = load_scenario_settings(
        config,
        ["alpha", "beta"],
        ["alpha", "beta"],
        {"alpha": alpha_overrides, "beta": None},
    )
    alpha = loaded["alpha"].settings
    assert alpha.provider == "openai-compatible"
    assert alpha.url == "https://endpoint.invalid/v1"
    assert alpha.model == "alpha-model"
    assert alpha.api_key == "test-secret-never-real"
    assert alpha.model_for("coordinator") == "defaults-coordinator"
    assert alpha.model_for("judge") == "alpha-judge"
    assert alpha.model_for("simulator") == "defaults-simulator"
    assert alpha.worker_models == {
        "routine": "alpha-routine",
        "advanced": "defaults-advanced",
    }
    # The explicit empty list replaces the defaults list; the threshold
    # inherits.
    assert alpha.delegation_guard.worker_blueprints == ()
    assert alpha.delegation_guard.threshold == 8192
    assert alpha.worker_effort == {"advanced": "medium", "expert": "medium"}
    beta = loaded["beta"].settings
    assert beta.model_for("coordinator") == "defaults-coordinator"
    assert beta.model == "fixture-model"
    assert beta.worker_models == {
        "routine": "defaults-routine",
        "advanced": "defaults-advanced",
    }
    # Provenance covers the shared defaults plus a sanitized effective hash.
    for entry in loaded.values():
        manifest = entry.manifest()
        assert manifest["defaults"]["source"] == "config.toml"
        assert len(manifest["defaults"]["sha256"]) == 64
        assert len(manifest["effective_sha256"]) == 64
        assert "api_key" not in json.dumps(manifest)
        assert "test-secret-never-real" not in json.dumps(manifest)
    assert (
        loaded["alpha"].manifest()["effective_sha256"]
        != loaded["beta"].manifest()["effective_sha256"]
    )


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX permissions")
def test_partial_sources_reject_unknown_keys_and_type_conflicts(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.toml"
    _write_private(config, DEFAULTS_FIXTURE)
    with pytest.raises(ValueError, match="models support"):
        load_scenario_settings(
            config,
            ["alpha"],
            ["alpha"],
            {"alpha": {"models": {"unknown": "x"}}},
        )
    with pytest.raises(ValueError, match="provider model"):
        load_scenario_settings(
            config,
            ["alpha"],
            ["alpha"],
            {"alpha": {"provider": {"model": 5}}},
        )
    # An invalid defaults value is rejected even when the scenario override
    # would mask it with a valid value.
    _write_private(
        config,
        fixture_text() + "\n[limits]\njudge_max_calls = 0\n",
    )
    with pytest.raises(ValueError, match="positive integer"):
        load_scenario_settings(
            config,
            ["alpha"],
            ["alpha"],
            {"alpha": {"limits": {"judge_max_calls": 32}}},
        )


def test_scenario_yaml_overrides_reject_credentials_and_secret_fields() -> None:
    # Non-secret provider routing is allowed in public overrides.
    validate_scenario_overrides(
        {"provider": {"model": "m", "url": "https://endpoint.invalid/v1"}},
        "scenario x settings",
    )
    # Credential fields are rejected at any nesting, including the provider
    # table and list items, and unknown tables and limits fail strictly.
    for bad in (
        {"provider": {"api_key": "k"}},
        {"provider": {"type": "openai-compatible", "password": "p"}},
        {"worker_models": {"nested": {"password": "p"}}},
        {"delegation_guard": {"worker_blueprints": [{"token": "t"}]}},
        {"models": {"coordinator": {"secret": "s"}}},
        {"limits": {"unknown": 1}},
        {"unknown_table": {}},
    ):
        with pytest.raises(ValueError):
            validate_scenario_overrides(bad, "scenario x settings")


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX permissions")
def test_migrated_scenario_settings_preserve_former_effect(
    tmp_path: Path,
) -> None:
    """The YAML settings migration preserves every effective setting.

    The retired per-scenario TOML contents are reconstructed here as plain
    mappings (credential-free) and must produce exactly the same effective
    settings as the shipped scenario config settings merged over the same
    shared config.
    """

    workers = Path(__file__).resolve().parents[3] / "customizations" / "workers"
    scenarios = {
        scenario.id: scenario
        for scenario in load_scenarios(workers / "evals" / "scenarios")
    }
    config = tmp_path / "config.toml"
    _write_private(config, DEFAULTS_FIXTURE)
    former_toml_overrides = {
        "escalation-resumption": {
            "models": {"coordinator": "z-ai/glm-5.3"},
            "worker_models": {
                "advanced": "openai/gpt-6-luna",
                "expert": "openai/gpt-6-luna",
            },
            "limits": {
                "turn_timeout_seconds": 1200,
                "execution_timeout_seconds": 1800,
            },
        },
        **{
            scenario_id: {"limits": {"turn_timeout_seconds": 300}}
            for scenario_id in (
                "polyglot-dot-dsl-rust-fixer",
                "polyglot-rest-api-fixer",
                "refactorbench-flask-debug-flag",
                "stable-task-order",
            )
        },
    }
    loaded = load_scenario_settings(
        config,
        sorted(former_toml_overrides),
        sorted(former_toml_overrides),
        former_toml_overrides,
    )
    migrated = load_scenario_settings(
        config,
        sorted(former_toml_overrides),
        sorted(former_toml_overrides),
        {
            scenario_id: scenarios[scenario_id].settings
            for scenario_id in former_toml_overrides
        },
    )
    for scenario_id in former_toml_overrides:
        assert (
            loaded[scenario_id].settings.sanitized_manifest()
            == migrated[scenario_id].settings.sanitized_manifest()
        )


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX permissions")
def test_missing_or_incomplete_sources_are_clear_errors(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    with pytest.raises(ValueError, match="config.toml"):
        load_scenario_settings(
            config, ["alpha"], ["alpha"], {"alpha": {"provider": {}}}
        )
    # Missing defaults are required even when no scenario overrides exist.
    with pytest.raises(ValueError, match="missing required settings config"):
        load_scenario_settings(config, ["alpha"], ["alpha"], {})
    # With shared defaults present, provenance records the defaults source.
    _write_private(config, DEFAULTS_FIXTURE)
    loaded = load_scenario_settings(config, ["alpha"], ["alpha"], {})
    assert loaded["alpha"].manifest()["defaults"] is not None
    assert loaded["alpha"].defaults_source is not None
    # Overrides merged over incomplete defaults still fail complete
    # validation before any build or paid call.
    _write_private(config, '[provider]\nmodel = "only"\n')
    with pytest.raises(ValueError, match="merged over"):
        load_scenario_settings(
            config,
            ["alpha"],
            ["alpha"],
            {"alpha": {"models": {"judge": "alpha-judge"}}},
        )
