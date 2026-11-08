from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from jcode_harness_client.scenarios import (
    load_scenarios,
    materialize_scenario,
    snapshot_tree,
    source_diff,
    tree_digest,
    verify_fixture_unchanged,
)

BASE_TOML = """id = "real-code"
fixture = "../../fixture"
prompt = "Implement the requested outcome."
accept = ["python3", "-c", "print('ok')"]

[judge]
criteria = ["Coordinator delegates appropriately and workers report verified results."]

[simulated_user]
max_turns = 8
instructions = "Approve reasonable plans and let the agent make decisions."
"""


def make_scenario(
    root: Path, body: str | None = None, scenario_id: str = "real-code"
) -> Path:
    fixture = root / "fixture"
    fixture.mkdir(exist_ok=True)
    (fixture / "old.txt").write_text("old\n", encoding="utf-8")
    (fixture / "delete.txt").write_text("delete\n", encoding="utf-8")
    scenario_dir = root / "scenarios" / scenario_id
    scenario_dir.mkdir(parents=True, exist_ok=True)
    config = scenario_dir / "config.toml"
    config.write_text(body if body is not None else BASE_TOML, encoding="utf-8")
    return config


def test_optional_settings_table_is_validated_and_not_leaked(
    tmp_path: Path,
) -> None:
    path = make_scenario(
        tmp_path, BASE_TOML + '\n[settings.models]\ncoordinator = "z-ai/glm-5.3"\n'
    )
    scenario = load_scenarios(path)[0]
    assert scenario.settings == {"models": {"coordinator": "z-ai/glm-5.3"}}
    # An explicit empty table inherits everything from the shared config.
    path = make_scenario(tmp_path, BASE_TOML + "\n[settings]\n")
    assert load_scenarios(path)[0].settings == {}
    # No settings table at all is also valid.
    assert make_scenario(tmp_path).is_file()
    assert load_scenarios(make_scenario(tmp_path))[0].settings == {}
    for bad in (
        BASE_TOML + '\n[settings.provider]\napi_key = "never-commit-this"\n',
        BASE_TOML + '\n[settings.models.coordinator]\npassword = "p"\n',
        BASE_TOML + "\n[settings.limits]\nunknown = 1\n",
        BASE_TOML + "\n[settings.unknown_table]\n",
        BASE_TOML.replace(
            'prompt = "Implement the requested outcome."',
            'prompt = "Implement the requested outcome."\nsettings = "not-a-mapping"',
        ),
    ):
        path = make_scenario(tmp_path, bad)
        with pytest.raises(ValueError):
            load_scenarios(path)
    # Settings never render into agent-facing prompt, judge criteria, or
    # simulated-user instructions.
    path = make_scenario(
        tmp_path, BASE_TOML + '\n[settings.models]\ncoordinator = "z-ai/glm-5.3"\n'
    )
    scenario = load_scenarios(path)[0]
    visible = "\n".join(
        (
            scenario.prompt,
            *scenario.judge.criteria,
            scenario.simulated_user.instructions,
        )
    )
    assert "z-ai/glm-5.3" not in visible
    assert "settings" not in visible


def test_real_scenario_schema_and_materialized_diff(tmp_path: Path) -> None:
    scenario_path = make_scenario(tmp_path)
    scenario = load_scenarios(scenario_path)[0]
    original_digest = tree_digest(scenario.fixture)
    workspace = tmp_path / "workspace"
    pristine = materialize_scenario(scenario, workspace)

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", *args], cwd=workspace, text=True
        ).strip()

    assert git("rev-list", "--count", "HEAD") == "1"
    assert git("status", "--porcelain") == ""
    assert git("log", "-1", "--format=%s") == "initial"
    assert tree_digest(workspace) == original_digest
    assert source_diff(workspace, scenario.fixture, pristine) == ""
    second = tmp_path / "second"
    materialize_scenario(scenario, second)
    assert subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=second, text=True
    ).strip() == git("rev-parse", "HEAD")
    fixture_with_git = tmp_path / "fixture-with-git"
    shutil.copytree(workspace, fixture_with_git)
    (fixture_with_git / "old.txt").write_text("already modified\n")
    existing = tmp_path / "existing"
    materialize_scenario(replace(scenario, fixture=fixture_with_git), existing)
    assert subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=existing, text=True
    ).strip() == "M old.txt"
    (workspace / "old.txt").write_text("new\n", encoding="utf-8")
    (workspace / "delete.txt").unlink()
    (workspace / "added.txt").write_text("added\n", encoding="utf-8")
    (workspace / "binary.bin").write_bytes(b"\x00\xff\x01")
    diff = source_diff(workspace, scenario.fixture, pristine)
    assert "-old" in diff and "+new" in diff
    assert "deleted file" in diff and "new file" in diff
    assert "Binary files differ" in diff
    assert ".git/" not in diff
    verify_fixture_unchanged(scenario, original_digest)
    assert scenario.accept == ("python3", "-c", "print('ok')")
    assert set(scenario.manifest()) == {
        "id",
        "source",
        "source_sha256",
        "fixture",
        "fixture_sha256",
        "prompt",
        "accept",
        "judge",
        "simulated_user",
        "settings",
    }
    assert scenario.judge.criteria
    assert scenario.simulated_user.max_turns == 8


def test_directory_selection_and_duplicate_ids(tmp_path: Path) -> None:
    first = make_scenario(tmp_path)
    scenarios_dir = tmp_path / "scenarios"
    # A directory claiming another directory's ID is rejected: directory
    # names are the unique identity under directory discovery.
    second = make_scenario(tmp_path, BASE_TOML, scenario_id="second")
    second_dir = second.parent
    with pytest.raises(ValueError, match="must match its scenario directory name"):
        load_scenarios(scenarios_dir)
    with pytest.raises(ValueError, match="unknown scenario ID"):
        load_scenarios(first, "missing")
    (second_dir / "config.toml").write_text(
        BASE_TOML.replace('id = "real-code"', 'id = "second"'), encoding="utf-8"
    )
    mismatched = make_scenario(
        tmp_path, BASE_TOML.replace('id = "real-code"', 'id = "other"'), "mismatch"
    )
    mismatched_dir = mismatched.parent
    scenarios_dir = mismatched_dir.parent
    with pytest.raises(ValueError, match="must match its scenario directory name"):
        load_scenarios(scenarios_dir)
    # Directory discovery loads sorted immediate children only.
    (second_dir / "config.toml").write_text(
        BASE_TOML.replace('id = "real-code"', 'id = "second"'), encoding="utf-8"
    )
    (mismatched_dir / "config.toml").write_text(
        BASE_TOML.replace('id = "real-code"', 'id = "mismatch"'), encoding="utf-8"
    )
    loaded = load_scenarios(scenarios_dir)
    assert [scenario.id for scenario in loaded] == ["mismatch", "real-code", "second"]


def test_directory_defaults_and_single_file_rules(tmp_path: Path) -> None:
    # A config without id inherits the directory name, and without fixture
    # it uses the adjacent fixtures directory.
    scenario_dir = tmp_path / "scenarios" / "fixtureless"
    fixtures = scenario_dir / "fixtures"
    fixtures.mkdir(parents=True)
    (fixtures / "ok.txt").write_text("ok\n", encoding="utf-8")
    config = scenario_dir / "config.toml"
    config.write_text(
        BASE_TOML.replace('id = "real-code"\n', "").replace(
            'fixture = "../../fixture"\n', ""
        ),
        encoding="utf-8",
    )
    scenario = load_scenarios(tmp_path / "scenarios")[0]
    assert scenario.id == "fixtureless"
    assert scenario.fixture == fixtures.resolve()
    # A directly loaded single file requires an explicit id.
    with pytest.raises(ValueError, match="required"):
        load_scenarios(config)
    stray = tmp_path / "scenarios" / "stray.yaml"
    stray.write_text("id: legacy\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a fallback"):
        load_scenarios(tmp_path / "scenarios")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('accept = ["python3", "-c", "print(\'ok\')"]', "accept = []"),
        ('accept = ["python3", "-c", "print(\'ok\')"]', 'accept = ["python3", 3]'),
        ('prompt = "Implement the requested outcome."', 'prompt = ""'),
        (
            'criteria = ["Coordinator delegates appropriately and workers report '
            'verified results."]',
            "criteria = []",
        ),
        ("max_turns = 8", "max_turns = 0"),
        (
            (
                'instructions = "Approve reasonable plans and let the agent '
                'make decisions."'
            ),
            'instructions = ""',
        ),
        (
            'prompt = "Implement the requested outcome."',
            'prompt = "Implement the requested outcome."\n'
            "version = 1\nexpect_delegation = true",
        ),
        (
            'prompt = "Implement the requested outcome."',
            'prompt = "Implement the requested outcome."\nmetrics = ["tool_health"]',
        ),
    ],
)
def test_old_or_malformed_scenario_fields_are_rejected(
    tmp_path: Path, old: str, new: str
) -> None:
    path = make_scenario(tmp_path, BASE_TOML.replace(old, new))
    with pytest.raises(ValueError):
        load_scenarios(path)


def test_invalid_toml_and_symlinks_are_rejected(tmp_path: Path) -> None:
    path = make_scenario(tmp_path)
    # Duplicate TOML keys are rejected by the parser itself.
    path.write_text(
        BASE_TOML + '\nextra_prompt = "duplicate"\ninvalid line here',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="valid UTF-8 TOML"):
        load_scenarios(path)
    path.write_text(BASE_TOML, encoding="utf-8")
    link = tmp_path / "linked.toml"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        load_scenarios(link)


def test_fixture_symlink_is_rejected(tmp_path: Path) -> None:
    path = make_scenario(tmp_path)
    (tmp_path / "fixture" / "escape").symlink_to(path)
    with pytest.raises(ValueError, match="contains a symlink"):
        load_scenarios(path)


def test_all_bundled_scenarios_are_outcome_only_and_harness_focused() -> None:
    scenarios = (
        Path(__file__).resolve().parents[3]
        / "customizations"
        / "workers"
        / "evals"
        / "scenarios"
    )
    loaded = load_scenarios(scenarios)
    assert len(loaded) == 5
    for scenario in loaded:
        raw = scenario.source.read_text(encoding="utf-8")
        assert "expect_delegation" not in raw
        assert "version = " not in raw
        assert "script = " not in raw
        assert "task compliance" not in raw.lower()
        assert "${workspace}" not in scenario.prompt
        assert scenario.accept
        assert any(
            "Worker non-use" in criterion for criterion in scenario.judge.criteria
        )
        assert "approve it" in scenario.simulated_user.instructions
        # Public settings overrides are credential-free and never render
        # into agent-facing text; the escalation scenario carries the
        # approved routing and budgets, and every other scenario inherits
        # the shared config with an empty table.
        visible = "\n".join(
            (
                scenario.prompt,
                *scenario.judge.criteria,
                scenario.simulated_user.instructions,
            )
        )
        assert "api_key" not in json.dumps(scenario.settings, default=str)
        assert "settings" not in visible
        assert not (
            set(scenario.settings)
            - {
                "provider",
                "models",
                "worker_models",
                "limits",
                "effort",
                "worker_effort",
                "delegation_guard",
            }
        )
    escalation = next(
        scenario for scenario in loaded if scenario.id == "escalation-resumption"
    )
    assert escalation.settings == {
        "worker_models": {
            "routine": "openai/gpt-6-luna",
            "advanced": "openai/gpt-6-luna",
            "expert": "openai/gpt-6-luna",
        },
        "limits": {
            "turn_timeout_seconds": 1200,
            "execution_timeout_seconds": 1800,
        },
        "delegation_guard": {"tail_bytes": 0},
    }
    for scenario in loaded:
        if scenario.id != "escalation-resumption":
            assert scenario.settings == {}


def test_snapshot_detects_invalid_utf8_as_binary(tmp_path: Path) -> None:
    (tmp_path / "bad.bin").write_bytes(b"\xff\xfe")
    state = snapshot_tree(tmp_path)["bad.bin"]
    assert state.binary
    assert len(state.sha256) == 64


PRIVATE_TOML = """
[instrumentation]
isolation = "blind"
controller = "controller.py"
journals = ["events.jsonl", "guard-events.jsonl"]
[instrumentation.environment]
JCODE_TEST_CONTROLLER_STATE_DIR = "{state_dir}"
JCODE_TEST_CONTROLLER_WORKSPACE = "{workspace}"
JCODE_TEST_CONTROLLER_GUARD = "{guard}"
JCODE_TEST_CONTROLLER_MODE = "blind"
"""


def test_private_instrumentation_is_not_materialized_or_in_agent_text(tmp_path):
    config = make_scenario(tmp_path, BASE_TOML + PRIVATE_TOML)
    asset = config.parent / "controller.py"
    asset.write_text("# private host hook\n")
    scenario = load_scenarios(config)[0]
    assert scenario.instrumentation.controller == asset.resolve()
    assert scenario.instrumentation.isolation == "blind"
    assert scenario.instrumentation.journals == ("events.jsonl", "guard-events.jsonl")
    workspace = tmp_path / "workspace"
    materialize_scenario(scenario, workspace)
    assert not (workspace / "controller.py").exists()
    assert "CONTROLLER" not in scenario.prompt
    assert "CONTROLLER" not in scenario.simulated_user.instructions
    assert "CONTROLLER" not in "\n".join(scenario.judge.criteria)


@pytest.mark.parametrize(
    "old,new",
    [
        ('controller = "controller.py"', 'controller = "../../fixture/private.py"'),
        ('controller = "controller.py"', 'controller = "/etc/passwd"'),
        ('isolation = "blind"', 'isolation = "host"'),
        (
            'journals = ["events.jsonl", "guard-events.jsonl"]',
            'journals = ["../events.jsonl"]',
        ),
        ('JCODE_TEST_CONTROLLER_MODE = "blind"', 'JCODE_HOME = "{state_dir}"'),
        (
            'JCODE_TEST_CONTROLLER_MODE = "blind"',
            'JCODE_TEST_CONTROLLER_MODE = "{unknown}"',
        ),
        ('isolation = "blind"', 'isolation = "blind"\ncommand = "unapproved"'),
    ],
)
def test_unsafe_private_metadata_is_rejected(tmp_path, old, new):
    config = make_scenario(tmp_path, BASE_TOML + PRIVATE_TOML.replace(old, new))
    (config.parent / "controller.py").write_text("# private\n")
    with pytest.raises(ValueError, match="instrumentation"):
        load_scenarios(config)


def test_private_controller_symlink_and_fixture_location_are_rejected(tmp_path):
    config = make_scenario(tmp_path, BASE_TOML + PRIVATE_TOML)
    target = tmp_path / "fixture/private.py"
    target.write_text("# must not be exposed\n")
    asset = config.parent / "controller.py"
    asset.symlink_to(target)
    with pytest.raises(ValueError, match="non-symlink"):
        load_scenarios(config)
    asset.unlink()
    asset.write_text("# private\n")
    config.write_text(
        (BASE_TOML + PRIVATE_TOML).replace('fixture = "../../fixture"', 'fixture = "."')
    )
    with pytest.raises(ValueError, match="outside the fixture"):
        load_scenarios(config)
