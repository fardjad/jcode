"""Shared orchestration for catalog patch worktrees and validation."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import os
from pathlib import Path
import shlex
import subprocess
import sys

from . import apply_patches, patch_worktree, run_nextest, validate_patch_commands
from .isolate_config import isolated_environment
from .patch_catalog import CatalogError, fail, git, repository_root
from .validate_patches import validate_catalog
from .version_base import resolve_base


def ensure_branch(root: Path, worktree: Path, branch: str) -> None:
    """Create a candidate branch, never replace a divergent existing one."""
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


@contextmanager
def prepared(root: Path, name: str, base: str, target: str | None = None,
             destination: Path | None = None, kind: str | None = None,
             preflight: bool = True):
    """Prepare once and retain the worktree with recovery guidance on failure."""
    if preflight:
        validate_catalog(root)
    worktree = patch_worktree.create(
        root, name, base,
        os.environ.get("JCODE_PATCH_WORKTREE_PREFIX", "patch-worktree-"), destination,
    )
    try:
        apply_patches.apply(root, worktree, target, kind)
        yield worktree
    except BaseException:
        print(f"workflow failed; worktree retained: {worktree}\n"
              f"cleanup: git worktree remove --force {shlex.quote(str(worktree))}",
              file=sys.stderr)
        raise


def compatibility(root: Path, worktree: Path, base: str, mode: str = "test") -> None:
    if run_nextest.execute(root, mode, worktree, base):
        raise CatalogError(f"compatibility {mode} failed in {worktree}")


def execute(root: Path, operation: str, patch: str | None = None, *, preflight: bool = True) -> None:
    """Keep each public workflow's validation, retention and cleanup behavior."""
    base = resolve_base(root)
    if operation in {"create", "validate"}:
        with prepared(root, "patched-jcode", base, destination=root / ".patched-jcode") as worktree:
            if operation == "validate":
                with isolated_environment() as environment:
                    subprocess.run(["cargo", "check", "--workspace"], cwd=worktree,
                                   env=environment, check=True)
                compatibility(root, worktree, base)
                print(f"patched workspace validated: {worktree}")
            else:
                print(f"patches applied; worktree retained: {worktree}")
        return
    if operation == "learn":
        # Learning never applies catalog patches or hides patched failures.
        worktree = patch_worktree.create(root, "clean-upstream", base)
        try:
            compatibility(root, worktree, base, "learn")
            validate_patch_commands.learn(root, worktree, base)
            patch_worktree.cleanup(root, "clean-upstream", False, worktree)
        except BaseException:
            print(f"clean-upstream learning failed; worktree retained: {worktree}\n"
                  f"cleanup: git worktree remove --force {shlex.quote(str(worktree))}",
                  file=sys.stderr)
            raise
        print(f"learned clean-upstream exclusions for: {base}")
        return
    if not patch:
        raise CatalogError(f"{operation} requires a patch")
    patch_name = Path(patch).name
    stem = Path(patch_name).stem
    candidate = operation == "candidate"
    name = f"{'candidate' if candidate else 'test'}-{stem}"
    with prepared(root, name, base, patch_name,
                  kind="upstream-candidate" if candidate else None, preflight=preflight) as worktree:
        validate_patch_commands.validate(root, worktree, patch_name, base)
        if candidate:
            slug = stem.split("-", 1)[-1].removeprefix("candidate-")
            branch = f"upstream-candidate/{slug}"
            ensure_branch(root, worktree, branch)
            patch_worktree.cleanup(root, name, False, worktree)
            print(f"candidate ready: {branch}")
        else:
            compatibility(root, worktree, base)
            print(f"patch passed; worktree retained: {worktree}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("create", "validate", "test", "candidate", "learn"))
    parser.add_argument("patch", nargs="?")
    args = parser.parse_args()
    if args.operation in {"test", "candidate"} and not args.patch:
        parser.error("this workflow requires a patch")
    try:
        execute(repository_root(), args.operation, args.patch)
    except (CatalogError, OSError, subprocess.CalledProcessError) as error:
        fail(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
