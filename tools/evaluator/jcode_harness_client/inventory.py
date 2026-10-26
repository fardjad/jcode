"""Run-specific blueprint and production-plugin snapshots."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hooks import make_hook_wrapper, warm_hook_wrapper
from .redaction import Redactor, write_bytes

_JSON = dict[str, Any]
_HOOKS = {
    "rtk": ("rtk/rtk-transform", "pre_tool_transform"),
    "delegation-guard": (
        "delegation-guard/delegation-guard-transform",
        "post_tool_transform",
    ),
}


@dataclass(frozen=True, slots=True)
class PluginSnapshot:
    id: str
    source_executable: Path
    executable: Path
    hook: str
    paths: tuple[str, ...]
    hashes: dict[str, str]

    def manifest(self) -> _JSON:
        return {
            "id": self.id,
            "source_command": [str(self.source_executable)],
            "production_command": [str(self.executable)],
            "executable": str(self.executable),
            "hook": self.hook,
            "paths": list(self.paths),
            "sha256": self.hashes,
        }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot_blueprints(
    source: Path,
    destination: Path,
    redactor: Redactor,
) -> tuple[list[_JSON], list[str]]:
    if source.is_symlink() or not source.is_dir():
        raise ValueError("blueprints must be a real directory")
    paths = sorted(source.glob("*.md"))
    if not paths or not (source / "coordinator.md").is_file():
        raise ValueError("blueprint directory requires coordinator.md and worker docs")
    snapshots: list[_JSON] = []
    issues: list[str] = []
    names: set[str] = set()
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"blueprint must be a regular non-symlink file: {path}")
        raw = path.read_bytes()
        text = raw.decode("utf-8")
        declared_name: str | None = None
        if text.startswith("---\n") and text.count("---") >= 2:
            front_matter = text.split("---", 2)[1]
            for line in front_matter.splitlines():
                if line.startswith("name:"):
                    declared_name = line.split(":", 1)[1].strip()
                    break
        else:
            issues.append(f"blueprint has invalid front matter: {path.name}")
        name = declared_name or (
            "coordinator" if path.stem == "coordinator" else path.stem
        )
        if name in names:
            raise ValueError(f"duplicate blueprint name in snapshot: {name}")
        names.add(name)
        if path.stem != "coordinator" and declared_name != path.stem:
            issues.append(
                f"blueprint name/config key mismatch: {path.name} "
                f"declares {declared_name!r}"
            )
        write_bytes(destination / path.name, raw, redactor)
        snapshots.append(
            {
                "name": name,
                "config_key": path.stem,
                "path": path.name,
                "citation": f"blueprint:{name}",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "content": redactor.text(text),
            }
        )
    return snapshots, issues


def _copy_plugin_tree(
    source: Path, destination: Path, redactor: Redactor
) -> tuple[list[str], dict[str, str]]:
    if source.is_symlink() or not source.is_dir():
        raise ValueError(f"plugin directory must be real: {source}")
    paths: list[str] = []
    hashes: dict[str, str] = {}
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source).as_posix()
        if path.is_symlink():
            raise ValueError(f"plugin snapshot rejects symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError(f"plugin snapshot rejects non-regular file: {path}")
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        write_bytes(target, path.read_bytes(), redactor)
        target.chmod(0o700 if path.stat().st_mode & 0o111 else 0o600)
        paths.append(relative)
        hashes[relative] = file_sha256(path)
    return paths, hashes


def snapshot_plugins(
    source: Path | None, destination: Path, redactor: Redactor
) -> list[PluginSnapshot]:
    if source is None:
        return []
    if source.is_symlink() or not source.is_dir():
        raise ValueError("plugins must be a real directory")
    snapshots: list[PluginSnapshot] = []
    for plugin_id, (relative, hook) in _HOOKS.items():
        executable = source / relative
        if executable.is_symlink() or not executable.is_file():
            raise ValueError(f"required production hook is missing: {executable}")
        plugin_root = source / plugin_id
        paths, hashes = _copy_plugin_tree(
            plugin_root, destination / plugin_id, redactor
        )
        effective = destination / relative
        effective.chmod(0o700)
        snapshots.append(
            PluginSnapshot(
                id=plugin_id,
                source_executable=executable.resolve(),
                executable=effective,
                hook=hook,
                paths=tuple(f"{plugin_id}/{path}" for path in paths),
                hashes={
                    f"{plugin_id}/{path}": digest for path, digest in hashes.items()
                },
            )
        )
    return snapshots


def blueprint_config(
    snapshot_dir: Path,
    blueprints: list[_JSON],
    swarm_model: str,
    timeout_minutes: int,
) -> str:
    text = (
        "\n[agents]\nworker_blueprints_dir = "
        + json.dumps(str(snapshot_dir.resolve()))
        + f"\nswarm_timeout_minutes = {timeout_minutes}\n"
    )
    for blueprint in blueprints:
        if blueprint["name"] != "coordinator":
            text += (
                "\n[agents.worker_blueprints."
                + json.dumps(blueprint["name"])
                + "]\nmodel = "
                + json.dumps(swarm_model)
                + "\n"
            )
    return text


def traced_plugin_config(
    plugins: list[PluginSnapshot],
    wrapper_dir: Path,
    trace_dir: Path,
) -> tuple[str, list[_JSON]]:
    if not plugins:
        return "", []
    lines = ["\n[hooks]"]
    commands: list[_JSON] = []
    for plugin in plugins:
        production = [str(plugin.executable)]
        wrapper = make_hook_wrapper(
            command=production,
            plugin=plugin.id,
            trace_dir=trace_dir / plugin.id,
            destination=wrapper_dir / f"{plugin.id}-trace",
        )
        warm_hook_wrapper(wrapper)
        traced = [str(wrapper)]
        lines.append(f"{plugin.hook} = [{json.dumps(str(wrapper))}]")
        if plugin.hook == "pre_tool_transform":
            lines.append("pre_tool_transform_timeout_ms = 2000")
        elif plugin.hook == "post_tool_transform":
            lines.append("post_tool_transform_timeout_ms = 2000")
        commands.append(
            {
                "hook": plugin.hook,
                "plugin": plugin.id,
                "production_command": production,
                "traced_command": traced,
                "trace_dir": str(trace_dir / plugin.id),
            }
        )
    return "\n".join(lines) + "\n", commands
