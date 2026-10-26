"""Validate personalization patch metadata and synthetic application."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .patch_catalog import (
    PATCH_ENCODING,
    CatalogError,
    Patch,
    fail,
    git,
    patches,
    repository_root,
)
from .metadata import SECTIONS, parse
from .version_base import resolve_base


HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def require(condition: bool, message: str) -> None:
    """Raise catalog error when condition is false."""
    if not condition:
        raise CatalogError(message)


def _target_path(header: str) -> str:
    """Return a readable target path from a unified-diff +++ header."""
    target = header[4:].split("\t", 1)[0]
    if target.startswith('"') and target.endswith('"'):
        target = target[1:-1]
    if target.startswith("b/"):
        target = target[2:]
    return target


def check_patch_whitespace(root: Path, patch: Patch, text: str) -> None:
    """Check only added source payload inside unified diff hunks."""
    relative = patch.path.relative_to(root).as_posix()
    target = "<unknown>"
    binary = False
    in_diff = False
    hunk: tuple[int, int, int, int] | None = None

    for patch_line, line in enumerate(text.splitlines(), start=1):
        if hunk is not None and hunk[2] == 0 and hunk[3] == 0:
            hunk = None

        if hunk is None and line == "-- ":
            break

        if line.startswith("diff --git "):
            in_diff = True
            target = "<unknown>"
            binary = False
            hunk = None
            continue

        if binary:
            continue

        if not in_diff:
            continue

        if hunk is not None:
            old_line, new_line, old_remaining, new_remaining = hunk
            if line == r"\ No newline at end of file":
                continue
            elif line.startswith("+"):
                payload = line[1:]
                issues: list[str] = []
                if payload.endswith((" ", "\t")):
                    issues.append("trailing whitespace")
                indentation = payload[:len(payload) - len(payload.lstrip(" \t"))]
                if re.search(r" \t", indentation):
                    issues.append("space before tab in leading indentation")
                if issues:
                    advice = "amend the owning source commit in .patched-jcode, then run just snapshot-patches"
                    raise CatalogError(
                        f"whitespace error in {relative}:{patch_line} "
                        f"({target}:{new_line}): {' and '.join(issues)}; {advice}"
                    )
                hunk = (old_line, new_line + 1, old_remaining, new_remaining - 1)
                continue
            elif line.startswith("-"):
                hunk = (old_line + 1, new_line, old_remaining - 1, new_remaining)
                continue
            elif line.startswith(" "):
                hunk = (old_line + 1, new_line + 1, old_remaining - 1, new_remaining - 1)
                continue
            elif line == "":
                hunk = (old_line + 1, new_line + 1, old_remaining - 1, new_remaining - 1)
                continue
            else:
                hunk = None

        if line.startswith("GIT binary patch") or line.startswith("Binary files "):
            binary = True
            continue
        if line.startswith("+++ "):
            target = _target_path(line)
            continue
        match = HUNK_HEADER.match(line)
        if match is not None:
            old_start, old_count, new_start, new_count = match.groups()
            hunk = (
                int(old_start), int(new_start),
                int(old_count) if old_count is not None else 1,
                int(new_count) if new_count is not None else 1,
            )


def validate_file(root: Path, patch: Patch, *, require_file_header: bool = False) -> None:
    """Validate one discovered patch's body sections and metadata."""
    path = patch.path
    relative = path.relative_to(root).as_posix()
    require(git("ls-files", "--error-unmatch", relative, cwd=root, quiet=True) == relative, f"untracked {relative}")
    text = path.read_text(encoding=PATCH_ENCODING)
    header_body, separator, _ = text.partition("\n---\n")
    require(bool(separator), f"missing patch body section: {relative}")
    _, body_separator, _ = header_body.partition("\n\n")
    require(bool(body_separator), f"missing patch body section: {relative}")
    try:
        metadata = parse(text, require_filename=require_file_header)
    except ValueError as error:
        raise CatalogError(f"{relative}: {error}") from error
    if metadata.filename:
        require(metadata.filename == path.name,
                f"X-Jcode-Patch-File does not match filename: {relative}")
    check_patch_whitespace(root, patch, text)


def validate_catalog(root: Path) -> None:
    """Validate metadata and synthetic application against the pinned release."""
    temporary: Path | None = None
    applied: Path | None = None
    try:
        require(git("branch", "--show-current", cwd=root) == "personalized", "current branch must be personalized")
        base = resolve_base(root)
        catalog = patches(root)
        ordered = [catalog[name] for name in sorted(catalog)]
        for patch in ordered:
            validate_file(root, patch, require_file_header=True)
        tracked = set(git("ls-files", "patches/*", cwd=root).splitlines())
        expected_files = {patch.path.relative_to(root).as_posix() for patch in ordered}
        require(tracked == expected_files, f"unexpected patch files: {' '.join(sorted(tracked - expected_files))}")
        temporary = Path(tempfile.mkdtemp(prefix="jcode-patch-validation."))
        applied = temporary / "applied"
        git("worktree", "add", "--detach", str(applied), base)
        for patch in ordered:
            git("am", "--3way", str(patch.path), cwd=applied, quiet=True)
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


def main() -> None:
    validate_catalog(repository_root())


if __name__ == "__main__":
    try:
        main()
    except CatalogError as error:
        fail(str(error))
