from __future__ import annotations

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


def make_scenario(root: Path, body: str | None = None) -> Path:
    fixture = root / "fixture"
    fixture.mkdir(exist_ok=True)
    (fixture / "old.txt").write_text("old\n", encoding="utf-8")
    (fixture / "delete.txt").write_text("delete\n", encoding="utf-8")
    scenario_dir = root / "scenarios"
    scenario_dir.mkdir(exist_ok=True)
    scenario = scenario_dir / "scenario.yaml"
    scenario.write_text(
        body
        or """id: real-code
fixture: ../fixture
prompt: Implement the requested outcome.
accept: [python3, -c, "print('ok')"]
metrics: [tool_health]
judge:
  criteria: [Coordinator delegates appropriately and workers report verified results.]
simulated_user:
  max_turns: 8
  instructions: Approve reasonable plans and let the agent make decisions.
""",
        encoding="utf-8",
    )
    return scenario


def test_real_scenario_schema_and_materialized_diff(tmp_path: Path) -> None:
    scenario_path = make_scenario(tmp_path)
    scenario = load_scenarios(scenario_path)[0]
    original_digest = tree_digest(scenario.fixture)
    workspace = tmp_path / "workspace"
    pristine = materialize_scenario(scenario, workspace)
    (workspace / "old.txt").write_text("new\n", encoding="utf-8")
    (workspace / "delete.txt").unlink()
    (workspace / "added.txt").write_text("added\n", encoding="utf-8")
    (workspace / "binary.bin").write_bytes(b"\x00\xff\x01")
    diff = source_diff(workspace, scenario.fixture, pristine)
    assert "-old" in diff and "+new" in diff
    assert "deleted file" in diff and "new file" in diff
    assert "Binary files differ" in diff
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
        "metrics",
        "judge",
        "simulated_user",
    }
    assert scenario.metrics == ("tool_health",)
    assert scenario.judge.criteria
    assert scenario.simulated_user.max_turns == 8


def test_directory_selection_and_duplicate_ids(tmp_path: Path) -> None:
    first = make_scenario(tmp_path)
    second = tmp_path / "scenarios" / "second.yaml"
    second.write_text(first.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate scenario IDs"):
        load_scenarios(first.parent)
    with pytest.raises(ValueError, match="unknown scenario ID"):
        load_scenarios(first, "missing")


@pytest.mark.parametrize(
    "replacement",
    [
        "accept: [python3]\nextra: true",
        "expect_delegation: true",
        "worker: fixer",
        "version: 1",
        "accept: []",
        "accept: [python3, 3]",
        "prompt: ''",
    ],
)
def test_old_or_malformed_scenario_fields_are_rejected(
    tmp_path: Path, replacement: str
) -> None:
    path = make_scenario(tmp_path)
    text = path.read_text(encoding="utf-8")
    if replacement.startswith("accept:"):
        text = text.replace("accept: [python3, -c, \"print('ok')\"]", replacement)
    elif replacement.startswith("prompt:"):
        text = text.replace("prompt: Implement the requested outcome.", replacement)
    else:
        text += replacement + "\n"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError):
        load_scenarios(path)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("metrics: [tool_health]", "metrics: [tool_health, tool_health]"),
        ("metrics: [tool_health]", "metrics: tool_health"),
        (
            "criteria: [Coordinator delegates appropriately and workers report "
            "verified results.]",
            "criteria: []",
        ),
        ("max_turns: 8", "max_turns: 0"),
        (
            "instructions: Approve reasonable plans and let the agent make decisions.",
            "instructions: ''",
        ),
    ],
)
def test_metric_judge_and_user_configuration_is_validated(
    tmp_path: Path, old: str, new: str
) -> None:
    path = make_scenario(tmp_path)
    path.write_text(
        path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        load_scenarios(path)


def test_duplicate_yaml_keys_aliases_and_symlinks_are_rejected(tmp_path: Path) -> None:
    path = make_scenario(tmp_path)
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "prompt: duplicate\n", encoding="utf-8")
    with pytest.raises(ValueError, match="unique"):
        load_scenarios(path)
    path.write_text(
        """id: aliased
fixture: &fixture ../fixture
prompt: Implement something.
accept: [python3, -c, "pass"]
other: *fixture
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="anchors"):
        load_scenarios(path)
    link = tmp_path / "linked.yaml"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        load_scenarios(link)


def test_fixture_symlink_is_rejected(tmp_path: Path) -> None:
    path = make_scenario(tmp_path)
    (tmp_path / "fixture" / "escape").symlink_to(
        tmp_path / "scenarios" / "scenario.yaml"
    )
    with pytest.raises(ValueError, match="contains a symlink"):
        load_scenarios(path)


def test_all_bundled_scenarios_are_outcome_only_and_harness_focused() -> None:
    scenarios = Path(__file__).resolve().parents[2] / "workers" / "scenarios"
    loaded = load_scenarios(scenarios)
    assert len(loaded) == 3
    for scenario in loaded:
        raw = scenario.source.read_text(encoding="utf-8")
        assert "expect_delegation" not in raw
        assert "version:" not in raw
        assert "script:" not in raw
        assert "task compliance" not in raw.lower()
        assert "${workspace}" not in scenario.prompt
        assert scenario.accept
        assert scenario.metrics == ("tool_health",)
        assert any(
            "Worker non-use" in criterion for criterion in scenario.judge.criteria
        )
        assert "approve it" in scenario.simulated_user.instructions


def test_snapshot_detects_invalid_utf8_as_binary(tmp_path: Path) -> None:
    (tmp_path / "bad.bin").write_bytes(b"\xff\xfe")
    state = snapshot_tree(tmp_path)["bad.bin"]
    assert state.binary
    assert len(state.sha256) == 64
