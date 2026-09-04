"""Regenerate catalog patch files from commits in the patched worktree.

Patches are persistence artifacts derived from commits. This script reads the
ordered commits above ``master`` in the patched worktree, matches each to its
catalog patch file by lexicographic order, and regenerates every ``.patch`` file
from the corresponding commit via ``git format-patch``. The commit message is
the single source of truth for both the diff and the catalog metadata headers
and body sections.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

from patch_catalog import (
    CatalogError,
    fail,
    git,
    patches,
    repository_root,
)


def patched_worktree_path(root: Path) -> Path:
    """Return the conventional patched worktree location."""
    return root / ".patched-jcode"


def commits_above_base(worktree: Path, base: str) -> list[str]:
    """Return commit hashes above base in oldest-first application order."""
    output = git("rev-list", "--reverse", f"{base}..HEAD", cwd=worktree)
    return [line for line in output.splitlines() if line.strip()]


def commit_message(worktree: Path, commit: str) -> str:
    """Return the full commit message for one commit."""
    return git("log", "-1", "--format=%B", commit, cwd=worktree)


def parse_commit_metadata(message: str) -> str:
    """Extract X-Jcode-Patch-Kind from a commit message, validating structure."""
    kind = _header_value(message, "X-Jcode-Patch-Kind")
    _header_value(message, "X-Jcode-Patch-Intent")
    _header_value(message, "X-Jcode-Patch-Depends-On")
    return kind


def _header_value(message: str, name: str) -> str:
    """Return one non-empty header value from a commit message."""
    pattern = re.compile(rf"^{re.escape(name)}:\s*(.+)$", re.MULTILINE)
    matches = pattern.findall(message)
    if len(matches) != 1:
        raise CatalogError(
            f"commit message must have exactly one {name}: header; found {len(matches)}"
        )
    value = matches[0].strip()
    if not value:
        raise CatalogError(f"empty {name}: header in commit message")
    return value


def format_patch(worktree: Path, commit: str, kind: str) -> str:
    """Generate a patch from one commit, sanitizing personal patches."""
    output = subprocess.run(
        ["git", "format-patch", "--no-signature", "--stdout", "-1", commit],
        cwd=worktree,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout
    if kind != "upstream-candidate":
        output = _zero_from_hash(output)
    return output


def _zero_from_hash(patch_text: str) -> str:
    """Replace the real From commit hash with zeros for personal patches."""
    return re.sub(
        r"^From [0-9a-f]{40} ",
        "From 0000000000000000000000000000000000000000 ",
        patch_text,
        count=1,
    )


def derive_patch_name(kind: str, subject: str, existing: set[str], index: int) -> str:
    """Generate a new patch filename from kind and subject."""
    prefix_range = 0 if kind == "upstream-candidate" else 1000
    number = _next_number(existing, prefix_range)
    slug = _slugify(subject)
    name = f"{number:04d}-{kind}-{slug}.patch"
    if name in existing:
        raise CatalogError(f"generated patch name collides: {name}")
    return name


def _next_number(existing: set[str], prefix_range: int) -> int:
    """Find the next available patch number in a range."""
    numbers = []
    for name in existing:
        match = re.match(r"^(\d{4})-", name)
        if match:
            num = int(match.group(1))
            if prefix_range == 0 and num < 1000:
                numbers.append(num)
            elif prefix_range == 1000 and num >= 1000:
                numbers.append(num)
    return (max(numbers) + 1) if numbers else prefix_range


def _slugify(subject: str) -> str:
    """Turn a commit subject into a short kebab-case slug."""
    subject = subject.removeprefix("feat: ").removeprefix("fix: ")
    subject = subject.removeprefix("feat:").removeprefix("fix:")
    subject = subject.strip().lower()
    subject = re.sub(r"[^a-z0-9]+", "-", subject)
    subject = subject.strip("-")
    if not subject:
        raise CatalogError("cannot derive patch slug from commit subject")
    if len(subject) > 50:
        subject = subject[:50].rstrip("-")
    return subject


def validate_commit_message_sections(message: str, patch_name: str) -> None:
    """Ensure a commit message has all required body sections in order."""
    sections = ("Patch intent:", "Why it exists:", "Upstream integration points:",
                "Update guidance:", "Validation:")
    positions = [message.find(section) for section in sections]
    for section in sections:
        count = message.count(section)
        if count != 1:
            raise CatalogError(f"{patch_name}: commit message must have exactly one {section} (found {count})")
    if not all(p >= 0 for p in positions) or positions != sorted(positions):
        raise CatalogError(f"{patch_name}: commit message body sections out of order or missing")
    validation = message[positions[-1] + len("Validation:"):]
    commands = [line for line in validation.splitlines() if line.strip()]
    if not commands:
        raise CatalogError(f"{patch_name}: commit message Validation section is empty")
    if any(line != line.lstrip() or line.startswith(("-", "*", "`")) for line in commands):
        raise CatalogError(f"{patch_name}: commit message has invalid validation command")


def snapshot(root: Path) -> list[str]:
    """Regenerate all patch files from the patched worktree commits.

    Returns the list of regenerated patch filenames.
    """
    worktree = patched_worktree_path(root)
    if not worktree.is_dir() or not (worktree / ".git").exists():
        raise CatalogError(
            f"patched worktree not found at {worktree}; run 'just create-patched-copy' first"
        )
    base = git("rev-parse", "master^{commit}", cwd=root)
    commit_hashes = commits_above_base(worktree, base)
    if not commit_hashes:
        raise CatalogError("no commits above master in patched worktree")
    catalog = patches(root)
    existing_names = set(catalog)
    ordered_patches = [catalog[name] for name in sorted(catalog)]

    if len(commit_hashes) < len(ordered_patches):
        raise CatalogError(
            f"fewer commits ({len(commit_hashes)}) than patches ({len(ordered_patches)}); "
            "did a patch commit get dropped?"
        )

    regenerated: list[str] = []
    for index, commit in enumerate(commit_hashes):
        message = commit_message(worktree, commit)
        kind = parse_commit_metadata(message)
        validate_commit_message_sections(message, f"commit {commit[:8]}")

        if index < len(ordered_patches):
            patch = ordered_patches[index]
            patch_name = patch.path.name
        else:
            subject = message.splitlines()[0]
            patch_name = derive_patch_name(kind, subject, existing_names, index)
            existing_names.add(patch_name)
            print(f"new patch: {patch_name}")

        patch_text = format_patch(worktree, commit, kind)
        destination = root / "patches" / patch_name
        if destination.exists() and destination.read_text() == patch_text:
            print(f"unchanged: {patch_name}")
        else:
            destination.write_text(patch_text)
            print(f"regenerated: {patch_name}")
        regenerated.append(patch_name)

    stale = existing_names - set(regenerated)
    if stale:
        raise CatalogError(
            f"patches without a matching commit (remove or rebase): {' '.join(sorted(stale))}"
        )
    return regenerated


def list_mapping(root: Path) -> None:
    """Print the commit-to-patch mapping for the patched worktree."""
    worktree = patched_worktree_path(root)
    if not worktree.is_dir() or not (worktree / ".git").exists():
        raise CatalogError(
            f"patched worktree not found at {worktree}; run 'just create-patched-copy' first"
        )
    base = git("rev-parse", "master^{commit}", cwd=root)
    commit_hashes = commits_above_base(worktree, base)
    catalog = patches(root)
    ordered_patches = [catalog[name] for name in sorted(catalog)]
    if not commit_hashes:
        raise CatalogError("no commits above master in patched worktree")
    print(f"{'COMMIT':<12} {'PATCH':<55} SUBJECT")
    for index, commit in enumerate(commit_hashes):
        short = commit[:8]
        subject = commit_message(worktree, commit).splitlines()[0]
        if index < len(ordered_patches):
            name = ordered_patches[index].path.name
        else:
            name = "(new — no patch file yet)"
        print(f"{short:<12} {name:<55} {subject}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print commit-to-patch mapping")
    args = parser.parse_args()
    try:
        root = repository_root()
        if args.list:
            list_mapping(root)
        else:
            snapshot(root)
            print("snapshot complete")
    except CatalogError as error:
        fail(str(error))


if __name__ == "__main__":
    main()
