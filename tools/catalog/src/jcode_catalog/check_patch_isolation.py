"""Check that each patch applies on its own declared dependency closure."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from .isolate_config import isolated_environment
from .patch_catalog import CatalogError, dependency_order, fail, git, patches, repository_root
from .version_base import resolve_base


def check(root: Path, base: str) -> list[str]:
    """Apply every patch's declared dependency closure to one temporary worktree."""
    catalog = patches(root)
    failures: list[str] = []
    temporary = Path(tempfile.mkdtemp(prefix="jcode-patch-isolation."))
    worktree = temporary / "worktree"
    try:
        git("worktree", "add", "--detach", str(worktree), base, cwd=root)
        for patch in (catalog[name] for name in sorted(catalog)):
            git("-c", "core.autocrlf=false", "-c", "core.eol=lf", "reset", "--hard", base, cwd=worktree)
            ordered = dependency_order(root, patch.path)
            try:
                with isolated_environment() as environment:
                    subprocess.run(
                        [
                            "git", "-c", "core.autocrlf=false", "-c", "core.eol=lf",
                            "am", "--3way", *(str(item.path) for item in ordered),
                        ],
                        cwd=worktree,
                        env=environment,
                        check=True,
                    )
            except subprocess.CalledProcessError:
                subprocess.run(
                    ["git", "am", "--abort"], cwd=worktree,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                failures.append(patch.path.name)
                print(f"FAIL {patch.path.name}")
            else:
                print(f"ok {patch.path.name}")
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)], cwd=root,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "worktree", "prune"], cwd=root,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        shutil.rmtree(temporary, ignore_errors=True)
    if failures:
        raise CatalogError(f"patch isolation failures: {' '.join(failures)}")
    return failures


def main() -> None:
    root = repository_root()
    try:
        check(root, resolve_base(root))
    except CatalogError as error:
        fail(str(error))


if __name__ == "__main__":
    main()
