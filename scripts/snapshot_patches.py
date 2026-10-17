"""Regenerate catalog patches from commits using explicit filename metadata.

Each commit's ``X-Jcode-Patch-File`` header names its patch. The commit message
remains the source of truth for the patch body and catalog metadata.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from patch_catalog import CatalogError, PATCH_ENCODING, fail, git, repository_root


PATCH_NAME = re.compile(r"^\d{4}-[a-z0-9][a-z0-9-]*\.patch$")


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
        [
            "git", "-c", "diff.suppressBlankEmpty=true", "format-patch",
            "--no-signature", "--stdout", "-1", commit,
        ],
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
    if kind == "upstream-candidate":
        lower, upper = 0, 999
    elif kind.startswith("personal-"):
        lower, upper = 1000, 9999
    else:
        raise CatalogError(f"unsupported patch kind: {kind}")
    number = _next_number(existing, lower)
    if number > upper:
        raise CatalogError(f"no available patch number in {lower:04d}-{upper:04d} range")
    slug = _slugify(subject)
    name = f"{number:04d}-{kind}-{slug}.patch"
    if name in existing:
        raise CatalogError(f"generated patch name collides: {name}")
    return name


def _next_number(existing: set[str], lower: int) -> int:
    """Find the next available number after the existing numbers in a range."""
    upper = 999 if lower == 0 else 9999
    numbers = []
    for name in existing:
        match = re.match(r"^(\d{4})-", name)
        if match:
            number = int(match.group(1))
            if lower <= number <= upper:
                numbers.append(number)
    return (max(numbers) + 1) if numbers else (1 if lower == 0 else lower + 1)


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


def _entry_fields(entry: Any) -> tuple[str, str, str]:
    """Read commit hash, full message, and generated patch text from an entry."""
    if isinstance(entry, Mapping):
        commit = entry.get("commit_hash", entry.get("commit", entry.get("hash", "")))
        message = entry.get("message", "")
        text = entry.get("patch_text", entry.get("text", ""))
    elif isinstance(entry, (tuple, list)) and len(entry) == 3:
        commit, message, text = entry
    else:
        commit = getattr(entry, "commit_hash", getattr(entry, "commit", getattr(entry, "hash", "")))
        message = getattr(entry, "message", "")
        text = getattr(entry, "patch_text", getattr(entry, "text", ""))
    return str(commit), str(message), str(text)


def _kind_from_message(message: str) -> str | None:
    matches = re.findall(r"^X-Jcode-Patch-Kind:\s*(.*?)\s*$", message, re.MULTILINE)
    return matches[0].strip() if len(matches) == 1 and matches[0].strip() else None


def _file_values(message: str) -> list[str]:
    return re.findall(r"^X-Jcode-Patch-File:\s*(.*?)\s*$", message, re.MULTILINE)


def _validate_name_and_kind(name: str, kind: str) -> str | None:
    if "/" in name or not PATCH_NAME.fullmatch(name):
        return f"invalid X-Jcode-Patch-File filename: {name!r}"
    number = int(name[:4])
    if kind == "upstream-candidate":
        if not 0 <= number <= 999:
            return f"{name}: upstream-candidate patch number must be in 0000-0999"
    elif kind.startswith("personal-"):
        if not 1000 <= number <= 9999:
            return f"{name}: personal patch number must be in 1000-9999"
    else:
        return f"unsupported patch kind: {kind}"
    return None


def plan_snapshot(
    entries: list[Any], existing_names: set[str] | Mapping[str, str], prune: bool,
    existing_texts: Mapping[str, str] | None = None,
) -> tuple[list[tuple[str, str, str]], list[str]]:
    """Validate every commit and return (name, text, status) writes and deletions.

    ``existing_names`` may be a set of filenames or a filename-to-current-text
    mapping. The latter lets this pure planner identify unchanged patch content.
    """
    if isinstance(existing_names, Mapping):
        names_on_disk = set(existing_names)
        contents = existing_names
    else:
        names_on_disk = set(existing_names)
        contents = existing_texts or {}

    errors: list[str] = []
    commits: list[dict[str, str]] = []
    planned_names: set[str] = set()
    suggestion_names = set(names_on_disk)

    for entry in entries:
        commit, message, text = _entry_fields(entry)
        short = commit[:8] or "unknown"
        subject = message.splitlines()[0] if message.splitlines() else "(no subject)"
        kind: str | None = None
        try:
            kind = parse_commit_metadata(message)
        except CatalogError as error:
            errors.append(f"commit {short} ({subject}): {error}")
        try:
            validate_commit_message_sections(message, f"commit {short}")
        except CatalogError as error:
            errors.append(f"commit {short} ({subject}): {error}")

        values = _file_values(message)
        name: str | None = None
        if len(values) != 1 or not values[0].strip():
            if not values and kind is not None:
                try:
                    suggestion = derive_patch_name(kind, subject, suggestion_names, len(commits))
                    suggestion_names.add(suggestion)
                    errors.append(
                        f'commit {short} ({subject}) is missing X-Jcode-Patch-File; '
                        f'add "X-Jcode-Patch-File: {suggestion}" to commit {short} ({subject})'
                    )
                except CatalogError as error:
                    errors.append(f"commit {short} ({subject}): {error}")
            else:
                errors.append(
                    f"commit {short} ({subject}): must have exactly one non-empty "
                    f"X-Jcode-Patch-File: header; found {len(values)}"
                )
        else:
            name = values[0].strip()

        if kind is None:
            kind = _kind_from_message(message)
        if name is not None and kind is not None:
            issue = _validate_name_and_kind(name, kind)
            if issue:
                errors.append(f"commit {short} ({subject}): {issue}")
            if name in planned_names:
                errors.append(f"commit {short} ({subject}): duplicate X-Jcode-Patch-File name {name}")
            planned_names.add(name)
            suggestion_names.add(name)

        dep_values = re.findall(
            r"^X-Jcode-Patch-Depends-On:\s*(.*?)\s*$", message, re.MULTILINE
        )
        if len(dep_values) == 1:
            dependency_value = dep_values[0].strip()
            if dependency_value != "none":
                dependencies = [item.strip() for item in dependency_value.split(",")]
                seen_dependencies: set[str] = set()
                earlier_names = {item["name"] for item in commits if item.get("name")}
                for dependency in dependencies:
                    if not dependency or dependency in seen_dependencies:
                        errors.append(
                            f"commit {short} ({subject}): invalid or duplicate dependency {dependency!r}"
                        )
                    elif dependency not in earlier_names:
                        errors.append(
                            f"commit {short} ({subject}): dependency {dependency} must name an earlier "
                            "commit in this stack (not merely an existing patch file)"
                        )
                    seen_dependencies.add(dependency)

        commits.append({
            "commit": commit, "message": message, "text": text,
            "name": name or "", "subject": subject,
        })

    for previous, current in zip(commits, commits[1:]):
        before = previous["name"]
        after = current["name"]
        if before and after and before >= after:
            errors.append(
                f"commits {previous['commit'][:8]} ({previous['subject']}) and "
                f"{current['commit'][:8]} ({current['subject']}) are out of order: "
                f"{before} must sort before {after}; renumber the file(s) so lexicographic "
                "application order matches commit order"
            )

    stale = sorted(names_on_disk - planned_names)
    if stale and not prune:
        errors.append(
            f"stale patch files: {' '.join(stale)}; rerun with --prune to delete"
        )
    if errors:
        raise CatalogError("snapshot planning failed:\n- " + "\n- ".join(errors))

    planned: list[tuple[str, str, str]] = []
    for item in commits:
        name = item["name"]
        text = item["text"]
        if name not in names_on_disk:
            status = "new"
        elif contents.get(name) == text:
            status = "unchanged"
        else:
            status = "regenerated"
        planned.append((name, text, status))
    return planned, stale if prune else []


def apply_plan(
    patches_dir: Path, plan: tuple[list[tuple[str, str, str]], list[str]]
) -> list[str]:
    """Apply a prevalidated plan, replacing each changed file atomically."""
    writes, deletions = plan
    results: list[str] = []
    patches_dir.mkdir(parents=True, exist_ok=True)
    for name, text, status in writes:
        if status != "unchanged":
            fd, temporary_name = tempfile.mkstemp(prefix=f".{name}.", dir=patches_dir)
            try:
                with os.fdopen(fd, "w", encoding=PATCH_ENCODING) as output:
                    output.write(text)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(temporary_name, patches_dir / name)
            except BaseException:
                Path(temporary_name).unlink(missing_ok=True)
                raise
        print(f"{status}: {name}")
        results.append(name)
    for name in deletions:
        (patches_dir / name).unlink()
        print(f"removed: {name}")
    return results


def snapshot(root: Path, prune: bool = False) -> list[str]:
    """Plan and apply patches from every commit above the pinned release."""
    worktree = patched_worktree_path(root)
    if not worktree.is_dir() or not (worktree / ".git").exists():
        raise CatalogError(
            f"patched worktree not found at {worktree}; run 'just create-patched-copy' first"
        )
    from version_base import resolve_base

    base = resolve_base(root)
    commit_hashes = commits_above_base(worktree, base)
    if not commit_hashes:
        raise CatalogError("no commits above pinned release in patched worktree")
    entries: list[dict[str, str]] = []
    for commit in commit_hashes:
        message = commit_message(worktree, commit)
        kind = _kind_from_message(message) or ""
        entries.append({
            "commit_hash": commit,
            "message": message,
            "patch_text": format_patch(worktree, commit, kind),
        })
    directory = root / "patches"
    existing_texts = {
        path.name: path.read_text(encoding=PATCH_ENCODING)
        for path in directory.glob("*.patch")
    }
    plan = plan_snapshot(entries, existing_texts, prune)
    return apply_plan(directory, plan)


def list_mapping(root: Path) -> None:
    """Print name-based commit-to-patch mapping and unclaimed catalog files."""
    worktree = patched_worktree_path(root)
    if not worktree.is_dir() or not (worktree / ".git").exists():
        raise CatalogError(
            f"patched worktree not found at {worktree}; run 'just create-patched-copy' first"
        )
    from version_base import resolve_base

    base = resolve_base(root)
    commit_hashes = commits_above_base(worktree, base)
    if not commit_hashes:
        raise CatalogError("no commits above pinned release in patched worktree")
    directory = root / "patches"
    disk_names = {path.name for path in directory.glob("*.patch")}
    claimed: set[str] = set()
    print(f"{'COMMIT':<12} {'PATCH':<55} SUBJECT")
    for commit in commit_hashes:
        message = commit_message(worktree, commit)
        values = _file_values(message)
        name = values[0].strip() if len(values) == 1 and values[0].strip() else "(missing X-Jcode-Patch-File)"
        if name in disk_names:
            claimed.add(name)
        print(f"{commit[:8]:<12} {name:<55} {message.splitlines()[0] if message.splitlines() else ''}")
    for name in sorted(disk_names - claimed):
        print(f"{'':<12} {name:<55} (stale, no commit)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="print commit-to-patch mapping")
    parser.add_argument("--prune", action="store_true", help="delete stale patches after validation")
    args = parser.parse_args()
    try:
        root = repository_root()
        if args.list:
            list_mapping(root)
        else:
            snapshot(root, prune=args.prune)
            print("snapshot complete")
    except CatalogError as error:
        fail(str(error))


if __name__ == "__main__":
    main()
