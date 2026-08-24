"""Validate personalization patch metadata and synthetic application."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from patch_catalog import CatalogError, Patch, fail, git, patches, repository_root


SECTIONS = ("Patch intent:", "Why it exists:", "Upstream integration points:", "Update guidance:", "Validation:")


def require(condition: bool, message: str) -> None:
    """Raise catalog error when condition is false."""
    if not condition:
        raise CatalogError(message)


def validate_file(root: Path, patch: Patch) -> None:
    """Validate one discovered patch's body sections and metadata."""
    path = patch.path
    relative = str(path.relative_to(root))
    require(git("ls-files", "--error-unmatch", relative, cwd=root, quiet=True) == relative, f"untracked {relative}")
    text = path.read_text()
    header_body, separator, _ = text.partition("\n---\n")
    require(bool(separator), f"missing patch body section: {relative}")
    _, body_separator, _ = header_body.partition("\n\n")
    require(bool(body_separator), f"missing patch body section: {relative}")
    positions = [header_body.find(section) for section in SECTIONS]
    require(all(header_body.count(section) == 1 for section in SECTIONS),
            f"duplicate or missing patch body section: {relative}")
    for header in ("X-Jcode-Patch-Intent:", "X-Jcode-Patch-Kind:", "X-Jcode-Patch-Depends-On:"):
        require(header_body.count(header) == 1, f"duplicate or missing {header}: {relative}")
    require(all(position >= 0 for position in positions) and positions == sorted(positions),
            f"missing or out-of-order patch body section: {relative}")
    for index, (start, end) in enumerate(zip(positions, positions[1:] + [len(header_body)])):
        content = header_body[start + len(SECTIONS[index]):end]
        require(any(line.strip() for line in content.splitlines()), f"empty patch body section: {relative}")
    validation = header_body[positions[-1] + len("Validation:"):]
    commands = [line for line in validation.splitlines() if line.strip()]
    require(bool(commands), f"missing validation command: {relative}")
    require(not any(line != line.lstrip() or line.startswith(("-", "*", "`")) for line in commands),
            f"invalid validation command: {relative}")


def main() -> None:
    """Run discovered catalog validation and apply every patch to master."""
    argparse.ArgumentParser().parse_args()
    root = repository_root()
    temporary: Path | None = None
    applied: Path | None = None
    try:
        require(git("branch", "--show-current", cwd=root) == "personalized", "current branch must be personalized")
        try:
            git("show-ref", "--verify", "refs/heads/master", cwd=root, quiet=True)
        except CatalogError as error:
            raise CatalogError("local master required") from error
        catalog = patches(root)
        ordered = [catalog[name] for name in sorted(catalog)]
        for patch in ordered:
            validate_file(root, patch)
        tracked = set(git("ls-files", "patches/*", cwd=root).splitlines())
        expected_files = {str(patch.path.relative_to(root)) for patch in ordered}
        require(tracked == expected_files, f"unexpected patch files: {' '.join(sorted(tracked - expected_files))}")
        temporary = Path(tempfile.mkdtemp(prefix="jcode-patch-validation."))
        applied = temporary / "applied"
        git("worktree", "add", "--detach", str(applied), "master")
        for patch in ordered:
            git("am", str(patch.path), cwd=applied, quiet=True)
        applied_tree = git("rev-parse", "HEAD^{tree}", cwd=applied)
    except CatalogError:
        raise
    finally:
        if temporary is not None:
            for worktree in (applied,):
                if worktree is not None:
                    subprocess.run(["git", "worktree", "remove", "--force", str(worktree)], cwd=root,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "worktree", "prune"], cwd=root,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            shutil.rmtree(temporary, ignore_errors=True)
    print(f"patch catalog valid: tree {applied_tree}")


if __name__ == "__main__":
    try:
        main()
    except CatalogError as error:
        fail(str(error))
