"""CLI retirement, strict parsing, and preflight selection tests."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from jcode_harness_client import cli

SECRET = "test-secret-never-real"

BASE_DEFAULTS = (
    '[provider]\ntype="openai-compatible"\n'
    'url="https://endpoint.invalid/v1"\nmodel="m"\n'
    f'api_key="{SECRET}"\n'
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.parametrize("command", ["migrate-settings", "derive-defaults"])
def test_retired_migration_commands_reject_before_filesystem_access(
    command: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_on_path_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("retired command accessed the filesystem")

    monkeypatch.setattr(Path, "exists", fail_on_path_access)
    monkeypatch.setattr(Path, "is_file", fail_on_path_access)
    monkeypatch.setattr(Path, "is_dir", fail_on_path_access)
    monkeypatch.setattr(Path, "read_text", fail_on_path_access)
    monkeypatch.setattr(Path, "write_text", fail_on_path_access)
    with pytest.raises(SystemExit) as error:
        cli.main(
            [
                command,
                "--source",
                str(tmp_path / "legacy.toml"),
                "--settings-dir",
                str(tmp_path / "settings"),
                "--scenarios",
                str(tmp_path / "scenarios"),
            ]
        )
    assert error.value.code == 2


SCENARIO_TOML = """id = "{id}"
prompt = "Do the bounded task."
accept = ["true"]
metrics = ["tool_health"]

[judge]
criteria = ["Good bounded coordination."]

[simulated_user]
max_turns = 4
instructions = "Approve reasonable progress."
"""


def _real_scenarios(tmp_path: Path, *scenario_ids: str) -> Path:
    scenarios_dir = tmp_path / "scenarios"
    scenarios_dir.mkdir()
    for scenario_id in scenario_ids:
        # The default adjacent fixtures directory is used without an
        # explicit fixture field.
        fixtures = scenarios_dir / scenario_id / "fixtures"
        fixtures.mkdir(parents=True)
        (fixtures / "target.txt").write_text("OLD\n", encoding="utf-8")
        (scenarios_dir / scenario_id / "config.toml").write_text(
            SCENARIO_TOML.format(id=scenario_id), encoding="utf-8"
        )
    return scenarios_dir


def test_validate_settings_selects_one_scenario_with_other_valid_files(
    tmp_path: Path,
) -> None:
    scenarios_dir = _real_scenarios(tmp_path, "alpha", "beta")
    # Shared config is required; a scenario without a settings table
    # inherits everything from it.
    config = tmp_path / "config.toml"
    config.write_text(
        BASE_DEFAULTS,
        encoding="utf-8",
    )
    config.chmod(0o600)
    validate = [
        "validate-settings",
        "--config",
        str(config),
        "--scenarios",
        str(scenarios_dir),
        "--scenario",
        "alpha",
    ]
    assert cli.main(validate) == 0
    # Public TOML settings overrides validate against the same config.
    (scenarios_dir / "alpha" / "config.toml").write_text(
        SCENARIO_TOML.format(id="alpha")
        + '\n[settings.models]\ncoordinator = "alpha-coordinator"\n',
        encoding="utf-8",
    )
    assert cli.main(validate) == 0
    # Credential fields in public TOML settings fail preflight.
    (scenarios_dir / "alpha" / "config.toml").write_text(
        SCENARIO_TOML.format(id="alpha")
        + '\n[settings.provider]\napi_key = "never-commit-this"\n',
        encoding="utf-8",
    )
    assert cli.main(validate) == 1
    # An invalid shared config fails preflight instead of being read.
    (scenarios_dir / "alpha" / "config.toml").write_text(
        SCENARIO_TOML.format(id="alpha"), encoding="utf-8"
    )
    config.write_text('[provider]\nmodel = "m"\n', encoding="utf-8")
    assert cli.main(validate) == 1


def test_stale_settings_flag_is_not_an_abbreviation_alias() -> None:
    with pytest.raises(SystemExit) as error:
        cli.main(["run", "--settings", "somewhere", "--binary", "b"])
    assert error.value.code == 2


def test_init_config_creates_shared_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "config.toml"
    (tmp_path / "config.toml.example").write_text(
        '[provider]\ntype="openai-compatible"\n'
        'url="https://endpoint.invalid/v1"\nmodel="m"\n'
        'api_key="REPLACE_WITH_YOUR_API_KEY"\n',
        encoding="utf-8",
    )
    assert cli.main(["init-config", str(config)]) == 0
    assert "config.toml" in capsys.readouterr().out
    assert config.is_file()
    if os.name != "nt":
        assert _mode(config) == 0o600
    assert cli.main(["init-config", str(config)]) == 1
    # The retired per-scenario argument arity is rejected.
    with pytest.raises(SystemExit) as error:
        cli.main(["init-config", str(config), "alpha"])
    assert error.value.code == 2
