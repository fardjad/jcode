"""Apply catalog patches to an existing worktree."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from patch_catalog import CatalogError, dependency_order, fail, patches, repository_root, resolve_patch


def apply(root: Path, worktree: Path, target: str | None, required_kind: str | None = None) -> None:
    """Apply all patches or target dependency closure; never create a worktree."""
    try:
        if not worktree.is_dir() or not (worktree / ".git").exists():
            raise CatalogError(f"existing Git worktree required: {worktree}")
        catalog = patches(root)
        if required_kind is not None and target is None:
            raise CatalogError("--require-kind requires a target patch")
        if target is None:
            ordered = [catalog[name] for name in sorted(catalog)]
        else:
            target_path = resolve_patch(root, target)
            if required_kind is not None and catalog[target_path.name].kind != required_kind:
                raise CatalogError(
                    f"{target_path.name} requires X-Jcode-Patch-Kind: {required_kind}"
                )
            ordered = dependency_order(root, target_path)
        for patch in ordered:
            print(f"applying {patch.path.name}")
            subprocess.run(["git", "am", str(patch.path)], cwd=worktree, check=True)
    except subprocess.CalledProcessError as error:
        raise CatalogError(
            f"git am conflict in {worktree}\n"
            f"git -C {worktree} status\n"
            f"Resolve conflicts, then run: git -C {worktree} am --continue\n"
            f"Or abandon and retry: git -C {worktree} am --abort"
        ) from error


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("worktree", type=Path)
    parser.add_argument("patch", nargs="?")
    parser.add_argument("--require-kind")
    args = parser.parse_args()
    try:
        root = repository_root()
        apply(root, args.worktree, args.patch, args.require_kind)
    except CatalogError as error:
        fail(str(error))


if __name__ == "__main__":
    main()
