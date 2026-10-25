"""Run catalog Validation commands in an existing worktree."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from isolate_config import isolated_environment
from patch_catalog import CatalogError, dependency_order, fail, patches, repository_root, resolve_patch, validation_commands
from run_nextest import config_path, load_validation_names
from validation_state import exclusive_lock, source_cache
from version_base import resolve_base


FAILED_TEST = re.compile(r"^test (\S+) \.\.\. FAILED$", re.MULTILINE)


def parse_failed_tests(output: str) -> list[str]:
    """Extract sorted unique Rust test names from cargo test output."""
    return sorted(set(FAILED_TEST.findall(output)))


def append_skips(command: str, names: list[str]) -> str:
    """Append cargo test skip options while preserving an existing delimiter."""
    if not names:
        return command
    separator = " --skip "
    if " -- " in command:
        return command + separator + " --skip ".join(names)
    return command + " --" + separator + " --skip ".join(names)


def write_validation_names(root: Path, base: str, names: list[str]) -> Path:
    """Update only validation exclusions in the existing base-specific file."""
    destination = config_path(root, base)
    try:
        payload = json.loads(destination.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise CatalogError(f"cannot read nextest exclusions {destination}: {error}") from error
    if payload.get("base_commit") != base:
        raise CatalogError(f"nextest exclusions do not match base {base}: {destination}")
    payload["failed_validation_tests"] = sorted(set(names))
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        json.dump(payload, temporary, indent=2)
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
    temporary_path.replace(destination)
    return destination


def learn(root: Path, worktree: Path, base: str) -> list[str]:
    """Run cargo Validation commands on clean upstream and record failing tests."""
    catalog = patches(root)
    cargo_target = source_cache(root, worktree, "patch-validation")
    names: set[str] = set()
    for patch in (catalog[name] for name in sorted(catalog)):
        for command in validation_commands(patch.path):
            if not command.lstrip().startswith("cargo test"):
                continue
            print(f"learning validation exclusions from {patch.path.name}: {command}")
            with isolated_environment() as environment, exclusive_lock(
                    cargo_target / "catalog.lock", "baseline validation cache"):
                environment["CARGO_TARGET_DIR"] = str(cargo_target)
                environment["JCODE_CATALOG_ROOT"] = str(root)
                result = subprocess.run(
                    ["bash", "-c", command], cwd=worktree, env=environment,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
            names.update(parse_failed_tests(result.stdout))
    found = sorted(names)
    path = write_validation_names(root, base, found)
    print(f"learned validation exclusions ({len(found)}): {path}")
    return found


def validate(root: Path, worktree: Path, target: str | None, base: str | None = None) -> None:
    """Run all or target closure validations; never apply or create worktrees."""
    if not worktree.is_dir() or not (worktree / ".git").exists():
        raise CatalogError(f"existing Git worktree required: {worktree}")
    catalog = patches(root)
    ordered = ([catalog[name] for name in sorted(catalog)]
               if target is None else dependency_order(root, resolve_patch(root, target)))
    base = base or resolve_base(root)
    exclusions = load_validation_names(root, base)
    print(f"validation exclusions: {len(exclusions)}")
    cargo_target = source_cache(root, worktree, "patch-validation")
    for patch in ordered:
        for command in validation_commands(patch.path):
            if command.lstrip().startswith("cargo test"):
                command = append_skips(command, exclusions)
            print(f"validating {patch.path.name}: {command}")
            try:
                with isolated_environment() as environment:
                    # Compiler metadata belongs to this clean source tree only.
                    environment["CARGO_TARGET_DIR"] = str(cargo_target)
                    environment["JCODE_CATALOG_ROOT"] = str(root)
                    with exclusive_lock(cargo_target / "catalog.lock", "declared validation cache"):
                        subprocess.run(["bash", "-c", command], cwd=worktree, env=environment, check=True)
            except subprocess.CalledProcessError as error:
                raise CatalogError(f"validation failed in {patch.path.name}: {command}") from error


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "learn":
        parser = argparse.ArgumentParser()
        parser.add_argument("command")
        parser.add_argument("worktree", type=Path)
        parser.add_argument("--base", required=True)
        args = parser.parse_args()
        try:
            learn(repository_root(), args.worktree, args.base)
        except CatalogError as error:
            fail(str(error))
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("worktree", type=Path)
    parser.add_argument("patch", nargs="?")
    parser.add_argument("--base")
    args = parser.parse_args()
    try:
        validate(repository_root(), args.worktree, args.patch, args.base)
    except CatalogError as error:
        fail(str(error))


if __name__ == "__main__":
    main()
