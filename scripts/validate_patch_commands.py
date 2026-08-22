"""Run catalog Validation commands in an existing worktree."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from patch_catalog import CatalogError, dependency_order, fail, patches, repository_root, resolve_patch, validation_commands


def validate(root: Path, worktree: Path, target: str | None) -> None:
    """Run all or target closure validations; never apply or create worktrees."""
    if not worktree.is_dir() or not (worktree / ".git").exists():
        raise CatalogError(f"existing Git worktree required: {worktree}")
    catalog = patches(root)
    ordered = ([catalog[name] for name in sorted(catalog)]
               if target is None else dependency_order(root, resolve_patch(root, target)))
    for patch in ordered:
        for command in validation_commands(patch.path):
            print(f"validating {patch.path.name}: {command}")
            try:
                subprocess.run(["bash", "-c", command], cwd=worktree, check=True)
            except subprocess.CalledProcessError as error:
                raise CatalogError(f"validation failed in {patch.path.name}: {command}") from error


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("worktree", type=Path)
    parser.add_argument("patch", nargs="?")
    args = parser.parse_args()
    try:
        validate(repository_root(), args.worktree, args.patch)
    except CatalogError as error:
        fail(str(error))


if __name__ == "__main__":
    main()
