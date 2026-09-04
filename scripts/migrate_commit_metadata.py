"""One-time migration: copy patch metadata into .patched-jcode commit messages.

This bootstraps the commit-first workflow for an existing catalog where the
patch files contain metadata (X-Jcode-Patch-* headers and body sections) that
the corresponding .patched-jcode commits lack. It reads the metadata from each
existing patch file and rewrites the matching commit message via git
filter-branch, preserving the original subject line.

After running this once, the commit messages become the single source of truth
and `just snapshot-patches` can regenerate the patch files from them.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from patch_catalog import CatalogError, fail, git, patches, repository_root


def patched_worktree_path(root: Path) -> Path:
    """Return the conventional patched worktree location."""
    return root / ".patched-jcode"


def commits_above_base(worktree: Path, base: str) -> list[str]:
    """Return commit hashes above base in oldest-first application order."""
    output = git("rev-list", "--reverse", f"{base}..HEAD", cwd=worktree)
    return [line for line in output.splitlines() if line.strip()]


def extract_metadata_from_patch(patch_path: Path) -> str:
    """Return the metadata body (after Subject, before ---) from a patch file."""
    text = patch_path.read_text()
    header_body, sep, _ = text.partition("\n---\n")
    if not sep:
        raise CatalogError(f"missing --- separator in {patch_path.name}")
    lines = header_body.splitlines()
    subject_idx = None
    for i, line in enumerate(lines):
        if line.startswith("Subject:"):
            subject_idx = i
            break
    if subject_idx is None:
        raise CatalogError(f"missing Subject in {patch_path.name}")
    after_subject = lines[subject_idx + 1:]
    while after_subject and after_subject[0] == "":
        after_subject.pop(0)
    if not after_subject:
        raise CatalogError(f"no metadata after Subject in {patch_path.name}")
    return "\n".join(after_subject)


def migrate(root: Path) -> None:
    """Rewrite .patched-jcode commit messages with metadata from patch files."""
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
    ordered_patches = [catalog[name] for name in sorted(catalog)]
    if len(commit_hashes) != len(ordered_patches):
        raise CatalogError(
            f"commit count ({len(commit_hashes)}) != patch count ({len(ordered_patches)})"
        )

    mapping: dict[str, str] = {}
    for commit, patch in zip(commit_hashes, ordered_patches):
        message = git("log", "-1", "--format=%B", commit, cwd=worktree)
        if "X-Jcode-Patch-Intent:" in message:
            print(f"already has metadata: {patch.path.name}")
            continue
        subject = message.splitlines()[0]
        metadata_body = extract_metadata_from_patch(patch.path)
        new_message = f"{subject}\n\n{metadata_body}\n"
        mapping[subject] = new_message

    if not mapping:
        print("all commits already have metadata; nothing to migrate")
        return

    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", dir="/tmp", delete=False
    ) as tmp:
        filter_script = Path(tmp.name)
        tmp.write(
            "#!/usr/bin/env python3\n"
            "import sys, json\n\n"
            f"mapping = {json.dumps(mapping)}\n\n"
            "msg = sys.stdin.read()\n"
            "subject = msg.splitlines()[0]\n"
            "new_msg = mapping.get(subject)\n"
            "if new_msg:\n"
            "    sys.stdout.write(new_msg)\n"
            "else:\n"
            "    sys.stdout.write(msg)\n"
        )

    env = os.environ.copy()
    env["FILTER_BRANCH_SQUELCH_WARNING"] = "1"
    try:
        result = subprocess.run(
            ["git", "filter-branch", "-f", "--msg-filter",
             f"python3 {filter_script}", f"{base}..HEAD"],
            cwd=worktree, capture_output=True, text=True, env=env,
        )
        if result.returncode != 0:
            raise CatalogError(f"git filter-branch failed:\n{result.stderr}")
        subprocess.run(
            ["git", "update-ref", "-d", "refs/original/HEAD"],
            cwd=worktree, capture_output=True,
        )
        print(f"migrated {len(mapping)} commit messages")
    finally:
        filter_script.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args()
    try:
        migrate(repository_root())
    except CatalogError as error:
        fail(str(error))


if __name__ == "__main__":
    main()
