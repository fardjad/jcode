"""Validated real-code scenarios and disposable fixture workspaces."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
_METRIC_ID = re.compile(r"[a-z][a-z0-9_-]{0,79}")
_FIELDS = {
    "id",
    "fixture",
    "prompt",
    "accept",
    "metrics",
    "judge",
    "simulated_user",
}


@dataclass(frozen=True, slots=True)
class JudgeScenario:
    criteria: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SimulatedUserScenario:
    max_turns: int
    instructions: str


@dataclass(frozen=True, slots=True)
class Scenario:
    """One outcome-only task, harness criteria, and independent acceptance command."""

    id: str
    fixture: Path
    prompt: str
    accept: tuple[str, ...]
    metrics: tuple[str, ...]
    judge: JudgeScenario
    simulated_user: SimulatedUserScenario
    source: Path
    source_sha256: str

    def manifest(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": str(self.source),
            "source_sha256": self.source_sha256,
            "fixture": str(self.fixture),
            "fixture_sha256": tree_digest(self.fixture),
            "prompt": self.prompt,
            "accept": list(self.accept),
            "metrics": list(self.metrics),
            "judge": {"criteria": list(self.judge.criteria)},
            "simulated_user": {
                "max_turns": self.simulated_user.max_turns,
                "instructions": self.simulated_user.instructions,
            },
        }


@dataclass(frozen=True, slots=True)
class FileState:
    sha256: str
    mode: int
    binary: bool


def _yaml_load(raw: bytes) -> Any:
    import yaml  # type: ignore[import-untyped]

    class UniqueLoader(yaml.SafeLoader):  # type: ignore[misc]
        def construct_mapping(self, node: Any, deep: bool = False) -> Any:
            result: dict[str, Any] = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str) or key in result:
                    raise ValueError("scenario keys must be unique strings")
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    if any(
        isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
        for token in yaml.scan(raw)
    ):
        raise ValueError("YAML anchors and aliases are unsupported")
    return yaml.load(raw, Loader=UniqueLoader)


def _load_file(path: Path) -> Scenario:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"scenario must be a regular non-symlink file: {path}")
    raw = path.read_bytes()
    if len(raw) > 256 * 1024:
        raise ValueError("scenario file exceeds 256 KiB")
    data = _yaml_load(raw)
    if not isinstance(data, dict) or set(data) != _FIELDS:
        raise ValueError(
            "scenario requires exactly id, fixture, prompt, accept, metrics, judge, "
            "and simulated_user; version is not supported"
        )
    scenario_id = data.get("id")
    if not isinstance(scenario_id, str) or not _ID.fullmatch(scenario_id):
        raise ValueError("scenario ID must be a lowercase slug")
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("scenario prompt must be nonempty text")
    fixture_value = data.get("fixture")
    if not isinstance(fixture_value, str) or not fixture_value:
        raise ValueError("scenario fixture must be a relative directory path")
    fixture_relative = Path(fixture_value)
    if fixture_relative.is_absolute():
        raise ValueError("scenario fixture must be a relative directory path")
    fixture = (path.parent / fixture_relative).resolve()
    if not fixture.is_dir() or fixture.is_symlink():
        raise ValueError("scenario fixture must be an existing non-symlink directory")
    for child in fixture.rglob("*"):
        if child.is_symlink():
            raise ValueError(
                f"fixture contains a symlink: {child.relative_to(fixture)}"
            )
    accept = data.get("accept")
    if (
        not isinstance(accept, list)
        or not accept
        or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in accept)
    ):
        raise ValueError("scenario accept must be a nonempty argv string list")
    metrics = data.get("metrics")
    if (
        not isinstance(metrics, list)
        or any(
            not isinstance(name, str) or not _METRIC_ID.fullmatch(name)
            for name in metrics
        )
        or len(set(metrics)) != len(metrics)
    ):
        raise ValueError("scenario metrics must be unique lowercase metric IDs")
    judge = data.get("judge")
    if not isinstance(judge, dict) or set(judge) != {"criteria"}:
        raise ValueError("scenario judge requires exactly criteria")
    criteria = judge["criteria"]
    if (
        not isinstance(criteria, list)
        or not criteria
        or any(not isinstance(item, str) or not item.strip() for item in criteria)
    ):
        raise ValueError("judge criteria must be a nonempty text list")
    simulated_user = data.get("simulated_user")
    if not isinstance(simulated_user, dict) or set(simulated_user) != {
        "max_turns",
        "instructions",
    }:
        raise ValueError("simulated_user requires exactly max_turns and instructions")
    max_turns = simulated_user["max_turns"]
    instructions = simulated_user["instructions"]
    if type(max_turns) is not int or max_turns < 1:
        raise ValueError("simulated_user.max_turns must be a positive integer")
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("simulated_user.instructions must be nonempty text")
    return Scenario(
        id=scenario_id,
        fixture=fixture,
        prompt=prompt.strip(),
        accept=tuple(accept),
        metrics=tuple(metrics),
        judge=JudgeScenario(tuple(item.strip() for item in criteria)),
        simulated_user=SimulatedUserScenario(max_turns, instructions.strip()),
        source=path.resolve(),
        source_sha256=hashlib.sha256(raw).hexdigest(),
    )


def load_scenarios(path: Path, scenario_id: str | None = None) -> list[Scenario]:
    """Load one scenario file or every top-level YAML scenario in a directory."""

    if path.is_symlink():
        raise ValueError("scenario path must not be a symlink")
    paths = sorted(path.glob("*.yaml")) if path.is_dir() else [path]
    if path.is_dir() and not paths:
        raise ValueError("scenario directory must contain at least one YAML file")
    scenarios = [_load_file(child) for child in paths]
    if len({scenario.id for scenario in scenarios}) != len(scenarios):
        raise ValueError("duplicate scenario IDs across files")
    if scenario_id is not None:
        scenarios = [scenario for scenario in scenarios if scenario.id == scenario_id]
        if not scenarios:
            raise ValueError(f"unknown scenario ID: {scenario_id}")
    return scenarios


def snapshot_tree(root: Path) -> dict[str, FileState]:
    """Hash every regular file, including files generated by tools or tests."""

    result: dict[str, FileState] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"workspace contains a symlink: {path.relative_to(root)}")
        if not path.is_file():
            continue
        raw = path.read_bytes()
        try:
            raw.decode("utf-8")
            binary = False
        except UnicodeDecodeError:
            binary = True
        result[path.relative_to(root).as_posix()] = FileState(
            sha256=hashlib.sha256(raw).hexdigest(),
            mode=stat.S_IMODE(path.stat().st_mode),
            binary=binary or b"\x00" in raw,
        )
    return result


def tree_digest(root: Path) -> str:
    files = snapshot_tree(root)
    serializable = {
        name: {"sha256": state.sha256, "mode": state.mode}
        for name, state in files.items()
    }
    return hashlib.sha256(
        json.dumps(serializable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def materialize_scenario(scenario: Scenario, workspace: Path) -> dict[str, FileState]:
    """Copy a fixture without following links and return its pristine snapshot."""

    if workspace.exists() and any(workspace.iterdir()):
        raise ValueError("scenario workspace must be empty")
    workspace.mkdir(parents=True, exist_ok=True)
    shutil.copytree(scenario.fixture, workspace, dirs_exist_ok=True, symlinks=False)
    pristine = snapshot_tree(workspace)
    if pristine != snapshot_tree(scenario.fixture):
        raise RuntimeError("materialized fixture differs from its source")
    return pristine


def source_diff(
    workspace: Path,
    pristine_root: Path,
    pristine: dict[str, FileState] | None = None,
    final: dict[str, FileState] | None = None,
) -> str:
    """Return a complete no-index diff, with explicit binary and mode records."""

    pristine = snapshot_tree(pristine_root) if pristine is None else pristine
    final = snapshot_tree(workspace) if final is None else final
    chunks: list[str] = []
    for name in sorted(set(pristine) | set(final)):
        before = pristine.get(name)
        after = final.get(name)
        if before == after:
            continue
        chunks.append(f"diff --git a/{name} b/{name}\n")
        if before is None:
            chunks.append("new file\n")
        elif after is None:
            chunks.append("deleted file\n")
        if before is not None and after is not None and before.mode != after.mode:
            chunks.append(f"old mode {before.mode:04o}\nnew mode {after.mode:04o}\n")
        if (before is not None and before.binary) or (
            after is not None and after.binary
        ):
            old_hash = before.sha256 if before else "absent"
            new_hash = after.sha256 if after else "absent"
            chunks.append(
                f"Binary files differ (before {old_hash}, after {new_hash})\n"
            )
            continue
        old_lines = (
            (pristine_root / name).read_text(encoding="utf-8").splitlines(keepends=True)
            if before is not None
            else []
        )
        new_lines = (
            (workspace / name).read_text(encoding="utf-8").splitlines(keepends=True)
            if after is not None
            else []
        )
        chunks.extend(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=f"a/{name}" if before else "/dev/null",
                tofile=f"b/{name}" if after else "/dev/null",
            )
        )
    return "".join(chunks)


def verify_fixture_unchanged(scenario: Scenario, expected_sha256: str) -> None:
    if tree_digest(scenario.fixture) != expected_sha256:
        raise RuntimeError("evaluation modified the original fixture")


def write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    if os.name == "posix":
        path.chmod(0o600)
