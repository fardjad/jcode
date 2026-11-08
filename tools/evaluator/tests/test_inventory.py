from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from uuid import uuid4

import pytest

from jcode_harness_client.inventory import (
    blueprint_config,
    snapshot_blueprints,
    snapshot_plugins,
    traced_plugin_config,
)
from jcode_harness_client.redaction import Redactor


def blueprint_text(name: str | None) -> str:
    declared = f"name: {name}\n" if name else ""
    contract = name or "coordinator"
    return f"---\n{declared}enabled-tools: []\n---\nContract for {contract}.\n"


def test_worker_discovery_supports_add_remove_and_rename(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    first_name = f"worker-{uuid4().hex}"
    second_name = f"worker-{uuid4().hex}"
    renamed_name = f"worker-{uuid4().hex}"
    additional_name = f"worker-{uuid4().hex}"
    (source / "coordinator.md").write_text(blueprint_text(None), encoding="utf-8")
    (source / f"{first_name}.md").write_text(
        blueprint_text(first_name), encoding="utf-8"
    )
    first, issues = snapshot_blueprints(source, tmp_path / "one", Redactor())
    assert not issues
    assert {item["name"] for item in first} == {"coordinator", first_name}
    (source / f"{second_name}.md").write_text(
        blueprint_text(second_name), encoding="utf-8"
    )
    (source / f"{first_name}.md").unlink()
    second, issues = snapshot_blueprints(source, tmp_path / "two", Redactor())
    assert not issues
    assert {item["name"] for item in second} == {"coordinator", second_name}
    (source / f"{second_name}.md").rename(source / f"{renamed_name}.md")
    (source / f"{renamed_name}.md").write_text(
        blueprint_text(renamed_name), encoding="utf-8"
    )
    (source / f"{additional_name}.md").write_text(
        blueprint_text(additional_name), encoding="utf-8"
    )
    third, _ = snapshot_blueprints(source, tmp_path / "three", Redactor())
    config = blueprint_config(
        tmp_path / "three",
        third,
        "inherit",
        5,
        {renamed_name: "specialized-model"},
    )
    assert f'[agents.worker_blueprints."{renamed_name}"]' in config
    assert 'model = "specialized-model"' in config
    assert first_name not in config
    assert second_name not in config
    assert f'[agents.worker_blueprints."{additional_name}"]' in config


def test_invalid_blueprint_registration_is_explicit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    worker_id = f"worker-{uuid4().hex}"
    (source / "coordinator.md").write_text(blueprint_text(None), encoding="utf-8")
    (source / "source-name.md").write_text(blueprint_text(worker_id), encoding="utf-8")
    snapshots, issues = snapshot_blueprints(source, tmp_path / "output", Redactor())
    assert snapshots[-1]["name"] == worker_id
    assert any("mismatch" in issue for issue in issues)


def test_staging_never_writes_or_rewrites_a_configured_secret(tmp_path: Path) -> None:
    source = tmp_path / "blueprints"
    source.mkdir()
    (source / "coordinator.md").write_text(blueprint_text(None) + "secret-token")
    with pytest.raises(ValueError, match="configured secret"):
        snapshot_blueprints(source, tmp_path / "stage", Redactor(("secret-token",)))
    assert not list((tmp_path / "stage").glob("*.md"))


def test_plugin_snapshot_and_trace_commands_retain_production_identity(
    tmp_path: Path,
) -> None:
    plugins = tmp_path / "plugins"
    for plugin, executable in (
        ("rtk", "rtk-transform"),
        ("delegation-guard", "delegation-guard-transform"),
    ):
        directory = plugins / plugin
        directory.mkdir(parents=True)
        path = directory / executable
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        (directory / "metadata.json").write_text(json.dumps({"plugin": plugin}))
    snapshots = snapshot_plugins(plugins, tmp_path / "effective", Redactor())
    assert {plugin.id for plugin in snapshots} == {"rtk", "delegation-guard"}
    config, commands = traced_plugin_config(
        snapshots, tmp_path / "wrappers", tmp_path / "hook-traces"
    )
    assert "pre_tool_transform" in config
    assert "post_tool_transform" in config
    assert "pre_tool_transform_timeout_ms = 2000" in config
    assert "post_tool_transform_timeout_ms = 2000" in config
    assert not list((tmp_path / "hook-traces").rglob("trace.json"))
    assert len(commands) == 2
    for command in commands:
        assert Path(command["production_command"][0]).is_file()
        assert Path(command["traced_command"][0]).is_file()
        assert command["production_command"] != command["traced_command"]
        if os.name == "posix":
            assert Path(command["traced_command"][0]).stat().st_mode & 0o111


def test_composite_controller_replaces_only_the_guard_entry(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    for plugin, executable in (
        ("rtk", "rtk-transform"),
        ("delegation-guard", "delegation-guard-transform"),
    ):
        directory = plugins / plugin
        directory.mkdir(parents=True)
        path = directory / executable
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    snapshots = snapshot_plugins(plugins, tmp_path / "effective", Redactor())
    controller = tmp_path / "escalation_controller.py"
    controller.write_text("# composite controller\n", encoding="utf-8")
    config, commands = traced_plugin_config(
        snapshots,
        tmp_path / "wrappers",
        tmp_path / "hook-traces",
        composite_controller=controller,
        python_executable="/usr/bin/python3",
    )
    records = {command["plugin"]: command for command in commands}
    guard = records["delegation-guard"]
    rtk = records["rtk"]
    guard_executable = next(
        snapshot.executable
        for snapshot in snapshots
        if snapshot.id == "delegation-guard"
    )
    rtk_executable = next(
        snapshot.executable for snapshot in snapshots if snapshot.id == "rtk"
    )
    assert guard["composite"] is True
    assert guard["production_command"] == ["/usr/bin/python3", str(controller)]
    assert guard["controller_module"] == str(controller)
    assert guard["guard_executable"] == str(guard_executable)
    assert guard["guard_production_command"] == [str(guard_executable)]
    assert "composite" not in rtk
    assert rtk["production_command"] == [str(rtk_executable)]
    # The hook config keeps exactly one entry per hook; no duplicate guard.
    hooks = tomllib.loads(config)["hooks"]
    assert len(hooks["post_tool_transform"]) == 1
    assert len(hooks["pre_tool_transform"]) == 1
    guard_wrapper = Path(guard["traced_command"][0])
    sidecar = json.loads(guard_wrapper.with_suffix(".json").read_text())
    assert sidecar["command"] == guard["production_command"]
    assert sidecar["plugin"] == "delegation-guard"
