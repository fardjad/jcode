"""Create or verify an upstream-candidate branch at prepared worktree HEAD."""

from __future__ import annotations

import argparse
from pathlib import Path

from patch_catalog import CatalogError, fail, git, repository_root


def ensure_branch(root: Path, worktree: Path, branch: str) -> None:
    """Create branch at HEAD, or refuse any divergent existing branch."""
    expected = git("rev-parse", "HEAD", cwd=worktree)
    try:
        actual = git("rev-parse", branch, cwd=root, quiet=True)
    except CatalogError:
        git("branch", branch, expected, cwd=root)
        print(f"candidate branch created: {branch}")
    else:
        if actual != expected:
            raise CatalogError(f"candidate branch differs; refusing to change {branch}")
        print(f"candidate branch unchanged: {branch}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("worktree", type=Path)
    parser.add_argument("branch")
    args = parser.parse_args()
    try:
        ensure_branch(repository_root(), args.worktree, args.branch)
    except CatalogError as error:
        fail(str(error))


if __name__ == "__main__":
    main()
