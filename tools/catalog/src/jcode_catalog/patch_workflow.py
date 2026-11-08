"""Shared orchestration for catalog patch worktrees and validation."""
from __future__ import annotations

import argparse
from contextlib import contextmanager, nullcontext
import os
from pathlib import Path
import shlex
import subprocess
import sys

from . import apply_patches, patch_worktree, run_nextest, validate_patch_commands
from .isolate_config import isolated_environment
from .patch_catalog import CatalogError, fail, git, repository_root
from .validate_patches import validate_catalog
from .validation_state import exclusive_lock, source_cache
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
    """Prepare once and report recovery guidance on failure."""
    if preflight:
        validate_catalog(root)
    worktree = patch_worktree.create(
        root, name, base,
        os.environ.get("JCODE_PATCH_WORKTREE_PREFIX", "patch-worktree-"), destination,
        disposable=name == "patch-validation",
    )
    try:
        apply_patches.apply(root, worktree, target, kind)
        yield worktree
    except BaseException:
        recovery = ("rerunning patch validation resets this tool-owned worktree"
                    if name == "patch-validation" else
                    f"cleanup: git worktree remove --force {shlex.quote(str(worktree))}")
        print(f"workflow failed in: {worktree}\n{recovery}",
              file=sys.stderr)
        raise


def compatibility(root: Path, worktree: Path, base: str, mode: str = "test", *, held_lock=None) -> None:
    if run_nextest.execute(root, mode, worktree, base, held_lock=held_lock):
        raise CatalogError(f"compatibility {mode} failed in {worktree}")


def execute(root: Path, operation: str, patch: str | None = None, *, preflight: bool = True,
            compat_each: bool = False) -> None:
    """Own the purpose cache and source from reset through the last check."""
    purpose = "upstream-learning" if operation == "learn" else "patch-validation"
    with (nullcontext() if operation == "create" else exclusive_lock(
            root / ".tools/cargo-target" / purpose / "catalog.lock", purpose)) as lock:
        _execute(root, operation, patch, preflight, compat_each, lock)


def check_workspace(root: Path, worktree: Path, lock) -> None:
    with isolated_environment() as environment:
        environment["CARGO_TARGET_DIR"] = str(source_cache(root, worktree, "patch-validation"))
        environment["JCODE_CATALOG_ROOT"] = str(root)
        subprocess.run(["cargo", "check", "--workspace"], cwd=worktree, env=environment, check=True,
                       **({"pass_fds": (lock.fileno(),)} if os.name != "nt" else {}))


def _execute(root: Path, operation: str, patch: str | None, preflight: bool, compat_each: bool, lock) -> None:
    base = resolve_base(root)
    if operation in {"create", "validate", "gate"}:
        name = "patch-validation" if operation == "gate" else "patched-jcode"
        destination = None if operation == "gate" else root / ".patched-jcode"
        with prepared(root, name, base, destination=destination, preflight=preflight) as worktree:
            if operation != "create":
                check_workspace(root, worktree, lock)
                validate_patch_commands.validate(root, worktree, None, base, held_lock=lock)
                compatibility(root, worktree, base, held_lock=lock)
                print(f"patched workspace validated: {worktree}")
            else:
                print(f"patches applied; worktree retained: {worktree}")
        return
    if operation == "learn":
        # Learning never applies catalog patches or hides patched failures.
        worktree = patch_worktree.create(root, "clean-upstream", base)
        try:
            compatibility(root, worktree, base, "learn", held_lock=lock)
            validate_patch_commands.learn(root, worktree, base, held_lock=lock)
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
    name = f"candidate-{stem}" if candidate else "patch-validation"
    with prepared(root, name, base, patch_name,
                  kind="upstream-candidate" if candidate else None, preflight=preflight) as worktree:
        if not candidate:
            check_workspace(root, worktree, lock)
        validate_patch_commands.validate(root, worktree, patch_name, base, held_lock=lock)
        if candidate:
            slug = stem.split("-", 1)[-1].removeprefix("candidate-")
            branch = f"upstream-candidate/{slug}"
            ensure_branch(root, worktree, branch)
            patch_worktree.cleanup(root, name, False, worktree)
            print(f"candidate ready: {branch}")
        else:
            if compat_each:
                compatibility(root, worktree, base, held_lock=lock)
            print(f"patch passed: {worktree}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog=
        "Patch tests reuse a tool-owned patch-validation worktree; rerunning resets failed source.")
    parser.add_argument("operation", choices=("create", "validate", "test", "candidate", "learn"))
    parser.add_argument("patch", nargs="?")
    parser.add_argument("--compat-each", action="store_true", help="Also run compatibility for the patch closure.")
    args = parser.parse_args()
    if args.operation in {"test", "candidate"} and not args.patch:
        parser.error("this workflow requires a patch")
    try:
        execute(repository_root(), args.operation, args.patch, compat_each=args.compat_each)
    except (CatalogError, OSError, subprocess.CalledProcessError) as error:
        fail(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
