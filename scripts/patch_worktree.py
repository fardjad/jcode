"""Create, reuse, and clean detached personalization worktrees."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from patch_catalog import CatalogError, fail, git, repository_root, worktree_path


def create(root: Path, name: str, revision: str, prefix: str = "patch-worktree-", destination: Path | None = None) -> Path:
    """Create or safely reset clean detached worktree at revision."""
    if not name or Path(name).name != name or ".." in name:
        raise CatalogError(f"invalid worktree name: {name}")
    path = destination or worktree_path(root, name, prefix)
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        root = root.resolve()
        if path == root:
            raise CatalogError(f"refusing to reuse catalog checkout: {path}")
        try:
            git("rev-parse", "--is-inside-work-tree", cwd=path, quiet=True)
        except CatalogError as error:
            raise CatalogError(f"worktree path exists and is invalid: {path}") from error
        try:
            top_level = Path(git("rev-parse", "--show-toplevel", cwd=path)).resolve()
            common_dir = Path(git("rev-parse", "--git-common-dir", cwd=path))
            catalog_common_dir = Path(git("rev-parse", "--git-common-dir", cwd=root))
            if not common_dir.is_absolute():
                common_dir = path / common_dir
            if not catalog_common_dir.is_absolute():
                catalog_common_dir = root / catalog_common_dir
            common_dir = common_dir.resolve()
            catalog_common_dir = catalog_common_dir.resolve()
        except CatalogError as error:
            raise CatalogError(f"existing path is not a valid Git worktree: {path}") from error
        if top_level != path:
            raise CatalogError(f"worktree destination is not its top level: {path}")
        if common_dir != catalog_common_dir:
            raise CatalogError(f"worktree has different Git common dir: {path}")
        registered = {
            Path(line.removeprefix("worktree ")).resolve()
            for line in git("worktree", "list", "--porcelain", cwd=root).splitlines()
            if line.startswith("worktree ")
        }
        if path not in registered:
            raise CatalogError(f"path is not a registered Git worktree: {path}")
        if git("status", "--porcelain", cwd=path):
            raise CatalogError(f"existing worktree is dirty: {path}")
        print(f"reusing worktree {path}", file=sys.stderr)
        resolved_revision = git("rev-parse", f"{revision}^{{commit}}", cwd=root)
        git("checkout", "--detach", resolved_revision, cwd=path)
    else:
        git("worktree", "add", "--detach", str(path), revision)
    print(path)
    return path


def cleanup(root: Path, value: str, force: bool, destination: Path | None = None) -> None:
    """Remove named worktree, refusing dirty trees unless forced."""
    prefix = os.environ.get("JCODE_PATCH_WORKTREE_PREFIX", "patch-worktree-")
    if not value or Path(value).name != value or ".." in value:
        raise CatalogError(f"invalid worktree name: {value}")
    path = destination or worktree_path(root, value, prefix)
    if not path.exists():
        raise CatalogError(f"worktree does not exist: {path}")
    if not force and git("status", "--porcelain", cwd=path):
        raise CatalogError(f"worktree is dirty; use --force: {path}")
    args = ["worktree", "remove"] + (["--force"] if force else []) + [str(path)]
    git(*args)
    git("worktree", "prune")


def main() -> None:
    """Parse command line and execute worktree operation."""
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("create", "cleanup"))
    parser.add_argument("name")
    parser.add_argument("revision", nargs="?")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--path", type=Path)
    args = parser.parse_args()
    try:
        root = repository_root()
        if args.command == "create":
            if args.revision is None:
                raise CatalogError("create requires WORKTREE_NAME REVISION")
            create(root, args.name, args.revision,
                   os.environ.get("JCODE_PATCH_WORKTREE_PREFIX", "patch-worktree-"), args.path)
        else:
            cleanup(root, args.name, args.force, args.path)
    except CatalogError as error:
        fail(str(error))


if __name__ == "__main__":
    main()
