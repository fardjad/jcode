"""Shared helpers for personalization patch catalog workflows."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class CatalogError(RuntimeError):
    """Raised for an invalid catalog or failed workflow operation."""


@dataclass(frozen=True)
class Patch:
    """Patch file and its catalog metadata."""

    path: Path
    dependencies: tuple[str, ...]
    intent: str
    kind: str


def git(*args: str, cwd: Path | None = None, quiet: bool = False) -> str:
    """Run Git and return stdout, converting failures to readable errors."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, check=True, text=True,
            stdout=subprocess.PIPE, stderr=None if not quiet else subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError as error:
        raise CatalogError(f"git {' '.join(args)} failed") from error
    return result.stdout.strip()


def repository_root() -> Path:
    """Return current worktree root."""
    try:
        return Path(git("rev-parse", "--show-toplevel"))
    except CatalogError as error:
        raise CatalogError("not in git worktree") from error


def catalog_directory(root: Path) -> Path:
    """Return patch catalog directory."""
    return root / "patches"


def resolve_patch(root: Path, value: str) -> Path:
    """Resolve patch filename while preventing paths outside catalog."""
    name = Path(value).name
    if name != value and value not in (f"patches/{name}", str(catalog_directory(root) / name)):
        raise CatalogError(f"patch must be under patches: {value}")
    if not name.endswith(".patch") or ".." in name:
        raise CatalogError(f"invalid patch filename: {value}")
    path = catalog_directory(root) / name
    if not path.is_file():
        raise CatalogError(f"patch not in patches: {name}")
    if path.resolve() != path:
        raise CatalogError(f"patch escapes patches: {value}")
    return path


def _header_values(path: Path, prefix: str) -> list[str]:
    """Read RFC-style catalog header values."""
    return [line[len(prefix):] for line in path.read_text().splitlines() if line.startswith(prefix)]


def _metadata(path: Path, name: str) -> str:
    """Read one non-empty catalog header."""
    values = [line[len(name) + 1:].strip() for line in path.read_text().splitlines()
              if line.startswith(f"{name}:")]
    if len(values) != 1 or not values[0].strip():
        raise CatalogError(f"invalid {name.lower()} header in {path.name}")
    return values[0]


def patches(root: Path) -> dict[str, Patch]:
    """Read and validate dependency metadata for all catalog patches."""
    directory = catalog_directory(root)
    paths = sorted(directory.glob("*.patch"))
    if not paths:
        raise CatalogError("patch catalog is empty")
    names = {path.name for path in paths}
    result: dict[str, Patch] = {}
    for path in paths:
        dependency_header = _metadata(path, "X-Jcode-Patch-Depends-On")
        deps = () if dependency_header == "none" else tuple(dependency_header.split(", "))
        if (len(set(deps)) != len(deps) or
                any(not dep or dep not in names or dep >= path.name for dep in deps)):
            raise CatalogError(f"invalid dependencies: {path.name}")
        result[path.name] = Patch(
            path,
            deps,
            _metadata(path, "X-Jcode-Patch-Intent"),
            _metadata(path, "X-Jcode-Patch-Kind"),
        )
    return result


def dependency_order(root: Path, target: Path) -> list[Patch]:
    """Return target dependency closure in lexicographic application order."""
    catalog = patches(root)
    if target.name not in catalog:
        raise CatalogError(f"unknown patch: {target.name}")
    found: set[str] = set()

    def visit(name: str) -> None:
        if name in found:
            return
        for dependency in catalog[name].dependencies:
            visit(dependency)
        found.add(name)

    visit(target.name)
    return [catalog[name] for name in sorted(found)]


def validation_commands(path: Path) -> list[str]:
    """Return non-empty commands from patch Validation section."""
    text = path.read_text()
    section = text.split("\nValidation:\n", 1)
    if len(section) != 2:
        raise CatalogError(f"missing Validation section: {path.name}")
    return [line for line in section[1].split("\n---\n", 1)[0].splitlines() if line.strip()]


def worktree_path(root: Path, name: str, prefix: str = "patch-worktree-") -> Path:
    """Return stable worktree path under common Git directory."""
    common = Path(git("rev-parse", "--git-common-dir", cwd=root))
    if not common.is_absolute():
        common = (root / common).resolve()
    return common / "patch-worktrees" / f"{prefix}{name.removesuffix('.patch')}"


def fail(message: str) -> None:
    """Print workflow error and exit."""
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)
