from __future__ import annotations

import json
import os
from pathlib import Path

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
    (source / "coordinator.md").write_text(blueprint_text(None), encoding="utf-8")
    (source / "first-arbitrary.md").write_text(
        blueprint_text("first-arbitrary"), encoding="utf-8"
    )
    first, issues = snapshot_blueprints(source, tmp_path / "one", Redactor())
    assert not issues
    assert {item["name"] for item in first} == {"coordinator", "first-arbitrary"}
    (source / "second.md").write_text(blueprint_text("second"), encoding="utf-8")
    (source / "first-arbitrary.md").unlink()
    second, issues = snapshot_blueprints(source, tmp_path / "two", Redactor())
    assert not issues
    assert {item["name"] for item in second} == {"coordinator", "second"}
    (source / "second.md").rename(source / "renamed.md")
    (source / "renamed.md").write_text(blueprint_text("renamed"), encoding="utf-8")
    third, _ = snapshot_blueprints(source, tmp_path / "three", Redactor())
    config = blueprint_config(tmp_path / "three", third, "inherit", 5)
    assert '[agents.worker_blueprints."renamed"]' in config
    assert "first-arbitrary" not in config
    assert "second" not in config


def test_invalid_blueprint_registration_is_explicit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "coordinator.md").write_text(blueprint_text(None), encoding="utf-8")
    (source / "filename.md").write_text(blueprint_text("different"), encoding="utf-8")
    snapshots, issues = snapshot_blueprints(source, tmp_path / "output", Redactor())
    assert snapshots[-1]["name"] == "different"
    assert any("mismatch" in issue for issue in issues)


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
