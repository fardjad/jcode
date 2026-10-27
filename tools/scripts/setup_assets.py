"""Install jcode catalog plugins, worker blueprints, and skills into the jcode
home.

Default mode creates directory symlinks from this catalog checkout into the
target jcode home. When
directory symlinks are unsupported or refused by the filesystem, the tool
falls back to copying the catalog payload and records what it installed in a
state file so later runs can refresh pristine copies.

Copies stay in copy mode on subsequent runs. Change modes only by backing up
and moving the existing destinations, then installing again.
Copied assets update only when this tool is rerun. Inside a managed directory any local
edit or extra file blocks the update until the directory is backed up or
moved, so local changes are never overwritten or deleted. This includes
files the repository ignores, such as editor swap files, __pycache__
directories, and caches. Each asset is refreshed as one staged unit that is
rolled back on failure; a failure affecting a later asset leaves assets
refreshed earlier in the same run updated (the whole run is not atomic).
Top-level entries
that are not part of the managed payload (for example a local plugin or
skill directory, or one that upstream no longer ships) are preserved
untouched during updates and reported in the output. Local content ignored
by the repository (for example plugin directories without an allowlist
entry in plugins/.gitignore, such as the local plugins/agent-sudo plugin,
or skill directories without an entry in skills/.gitignore, plus
__pycache__, editor files, and test caches) is never copied. Existing
unmanaged files and unrelated symlinks are never replaced. config.toml is
never read or written. The tool never requests elevation.

Runtime requirements of the installed assets: the plugin adapters are
python3 scripts that use only the Python standard library, so a ``python3``
on PATH is required to run them. The rtk plugin additionally requires the
external ``rtk`` CLI on PATH and fails open when it is absent. Plugins only
take effect when config.toml [hooks] points at the installed executables;
this tool never edits that configuration.

Usage:
    python3 tools/scripts/setup_assets.py [--copy]
"""

from __future__ import annotations

import argparse
import errno
import fnmatch
import hashlib
import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[2]
METADATA_NAME = ".setup-assets-state.json"

ALWAYS_EXCLUDED_NAMES = {
    ".coverage",
    ".DS_Store",
    ".git",
    ".gitignore",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}
ALWAYS_EXCLUDED_PATTERNS = ("*.py[cod]", "*.swp")

# Windows raises errno EPERM (winerror 1314, privilege not held) and some
# filesystems raise EINVAL/ENOSYS/EOPNOTSUPP when symlinks are unavailable.
_SYMLINK_UNSUPPORTED_ERRNOS = {
    value
    for value in (
        getattr(errno, name, None)
        for name in ("EPERM", "EACCES", "EINVAL", "ENOSYS", "ENOTSUP", "EOPNOTSUPP")
    )
    if value is not None
}
_WINDOWS_SYMLINK_PRIVILEGE_WINERRORS = {1314}


@dataclass(frozen=True)
class Asset:
    name: str
    source_rel: Path
    required: tuple[str, ...]


ASSETS = (
    Asset(
        name="plugins",
        source_rel=Path("plugins"),
        required=("rtk/rtk-transform", "delegation-guard/delegation-guard-transform"),
    ),
    Asset(
        name="worker-blueprints",
        source_rel=Path("workers") / "blueprints",
        required=("coordinator.md", "fixer.md", "investigator.md", "research.md"),
    ),
    Asset(
        name="skills",
        source_rel=Path("skills"),
        required=("author-plan/SKILL.md",),
    ),
)


def fail(message: str) -> None:
    print(f"setup-assets: error: {message}", file=sys.stderr)


def note(message: str) -> None:
    print(f"setup-assets: {message}")


def home_dir() -> Path:
    """Resolve the target jcode home (JCODE_HOME or ~/.jcode)."""
    configured = os.environ.get("JCODE_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".jcode"


# ---------------------------------------------------------------------------
# Ignore rules and inventory


class IgnoreRule(NamedTuple):
    """One ordered .gitignore rule in the subset the catalog uses."""

    negated: bool
    anchored: bool
    dir_only: bool
    pattern: str


def parse_gitignore_rules(text: str) -> list[IgnoreRule]:
    """Parse the gitignore subset used by the catalog.

    Supported: blank lines, comments, trailing-/ directory-only patterns,
    leading-/ root anchoring, embedded-/ anchored patterns, plain unanchored
    basename globs, and negation (!) rules. Matching applies the rules in
    order and the last matching rule wins, mirroring git's behavior for this
    subset. Not supported (the catalog does not use them): ``**``,
    per-directory .gitignore files, and escaped bang or hash lines.
    """
    rules: list[IgnoreRule] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        dir_only = line.endswith("/")
        if dir_only:
            line = line.rstrip("/")
        if not line:
            continue
        if line.startswith("/"):
            anchored = True
            line = line.lstrip("/")
        else:
            anchored = "/" in line
        if not line:
            continue
        rules.append(
            IgnoreRule(negated=negated, anchored=anchored, dir_only=dir_only, pattern=line)
        )
    return rules


def load_gitignore_rules(source: Path) -> list[IgnoreRule]:
    gitignore = source / ".gitignore"
    try:
        text = gitignore.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    return parse_gitignore_rules(text)


def always_excluded(name: str) -> bool:
    if name in ALWAYS_EXCLUDED_NAMES:
        return True
    return any(fnmatch.fnmatch(name, pattern) for pattern in ALWAYS_EXCLUDED_PATTERNS)


def rule_matches(rule: IgnoreRule, rel_posix: str, is_dir: bool) -> bool:
    if rule.dir_only and not is_dir:
        return False
    if rule.anchored:
        # Anchored patterns match from the root and `*` never crosses `/`,
        # so compare segment by segment (`/*/` matches top-level dirs only).
        pattern_parts = rule.pattern.split("/")
        rel_parts = rel_posix.split("/")
        if len(pattern_parts) != len(rel_parts):
            return False
        return all(
            fnmatch.fnmatch(rel_part, pattern_part)
            for rel_part, pattern_part in zip(rel_parts, pattern_parts)
        )
    return fnmatch.fnmatch(rel_posix.split("/")[-1], rule.pattern)


def ignored(rel_posix: str, is_dir: bool, rules: list[IgnoreRule]) -> bool:
    """Evaluate rules in order; the last matching rule wins, like git."""
    decision = False
    for rule in rules:
        if rule_matches(rule, rel_posix, is_dir):
            decision = not rule.negated
    return decision


def content_hash(path: Path) -> str:
    """Hash a file's content; symlinks hash as their target string."""
    if path.is_symlink():
        return "symlink:" + os.readlink(path)
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def walk_files(root: Path, rules: list[IgnoreRule]) -> list[Path]:
    """List payload-relative paths of non-ignored entries below root."""
    found: list[Path] = []

    def visit(directory: Path, rel: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise RuntimeError(f"cannot list {directory}: {exc}") from exc
        for entry in entries:
            rel_entry = rel / entry.name
            rel_posix = rel_entry.as_posix()
            if always_excluded(entry.name):
                continue
            if entry.is_symlink():
                if not ignored(rel_posix, False, rules):
                    found.append(rel_entry)
                continue
            if entry.is_dir():
                if ignored(rel_posix, True, rules):
                    continue
                visit(Path(entry.path), rel_entry)
            elif entry.is_file():
                if not ignored(rel_posix, False, rules):
                    found.append(rel_entry)

    visit(root, Path())
    return sorted(found, key=lambda path: path.as_posix())


def inventory(root: Path, rules: list[IgnoreRule]) -> dict[str, str]:
    """Map relative payload path to content hash for a directory tree."""
    return {
        path.as_posix(): content_hash(root / path) for path in walk_files(root, rules)
    }


def walk_all_entries(root: Path) -> list[str]:
    """List every payload-relative path below root, pruning nothing.

    Destination protection scan: unlike the source payload walk, ignored and
    always-excluded names are still listed, so local files the copy never
    tracks (editor swap files, __pycache__ bytecode, caches) are seen before
    an update could delete them together with the backup tree. Symlinks are
    reported as leaves and never followed.
    """
    found: list[Path] = []

    def visit(directory: Path, rel: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise RuntimeError(f"cannot list {directory}: {exc}") from exc
        for entry in entries:
            rel_entry = rel / entry.name
            if entry.is_symlink():
                found.append(rel_entry)
                continue
            if entry.is_dir():
                visit(Path(entry.path), rel_entry)
            elif entry.is_file():
                found.append(rel_entry)

    visit(root, Path())
    return sorted(path.as_posix() for path in found)


# ---------------------------------------------------------------------------
# Installed-copy comparison


def managed_top_names(recorded: dict[str, str], desired: dict[str, str]) -> set[str]:
    return {rel.split("/", 1)[0] for rel in set(recorded) | set(desired)}


def find_problems(
    installed: Path,
    recorded: dict[str, str],
    desired: dict[str, str],
    rules: list[IgnoreRule],
) -> list[str]:
    """Diff an installed copy against the recorded and desired path sets."""
    problems: list[str] = []
    state = inventory(installed, rules)
    for rel in sorted(set(recorded) | set(desired)):
        on_disk = state.get(rel)
        if rel in recorded and rel in desired:
            if on_disk is None:
                problems.append(f"{rel}: recorded file is missing")
            elif on_disk != recorded[rel]:
                problems.append(f"{rel}: file changed since it was copied")
        elif rel in recorded:
            if on_disk is None:
                # The path may be invisible to the scan because its top-level
                # directory is no longer allowlisted (upstream dropped the
                # plugin). Check the file directly so a preserved directory
                # is not reported missing, while a local edit still blocks
                # the update.
                direct = None
                direct_path = installed / rel
                if direct_path.is_file():
                    direct = content_hash(direct_path)
                if direct == recorded[rel]:
                    continue
                if direct is not None:
                    problems.append(
                        f"{rel}: file changed and upstream no longer ships it"
                    )
                else:
                    problems.append(f"{rel}: recorded file is missing")
            elif on_disk != recorded[rel]:
                problems.append(f"{rel}: file changed and upstream no longer ships it")
        else:
            if on_disk is not None and on_disk != desired[rel]:
                problems.append(f"{rel}: unmanaged file blocks the upstream addition")
    # Extra-entry protection scans everything on disk, including names the
    # payload walk excludes (swap files, __pycache__, caches): an update
    # would delete those with the backup tree, so they must block first.
    tops = managed_top_names(recorded, desired)
    for rel in walk_all_entries(installed):
        if rel in recorded or rel in desired:
            continue
        top = rel.split("/", 1)[0]
        if top in tops:
            problems.append(f"{rel}: extra file inside managed directory '{top}'")
    return problems


# ---------------------------------------------------------------------------
# Metadata (stored outside the installed asset trees)


def metadata_path(home: Path) -> Path:
    return home / METADATA_NAME


def read_metadata(home: Path) -> dict | None:
    path = metadata_path(home)
    if not path.exists():
        return {"version": 1, "assets": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail(f"cannot read {path}: {exc}")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("assets"), dict):
        fail(f"{path} does not contain a setup-assets state object")
        return None
    return data


def write_metadata(path: Path, metadata: dict) -> None:
    if not metadata["assets"]:
        if path.exists():
            path.unlink()
        return
    tmp = path.with_name(path.name + f".tmp-{uuid.uuid4().hex[:8]}")
    try:
        tmp.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def recorded_files(metadata: dict, asset_name: str) -> dict[str, str]:
    entry = metadata["assets"].get(asset_name, {})
    files = entry.get("files", {}) if isinstance(entry, dict) else {}
    return files if isinstance(files, dict) else {}


# ---------------------------------------------------------------------------
# Copy installation (staged, with rollback)


def unique_sibling(dest: Path, kind: str) -> Path:
    return dest.with_name(f"{dest.name}.{kind}-{uuid.uuid4().hex[:8]}")


def stage_tree(source: Path, desired: dict[str, str], staging: Path) -> None:
    for rel in sorted(desired):
        target = staging / Path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / rel, target)


def move_unmanaged_back(backup: Path, dest: Path, managed_tops: set[str]) -> list[str]:
    """Preserve backup entries outside the managed top-level names.

    Any top-level entry that the refreshed payload does not ship (a local
    plugin directory, or a plugin upstream no longer includes) is moved back
    untouched so it is never silently deleted.
    """
    moved: list[str] = []
    for entry in sorted(os.listdir(backup)):
        if entry in managed_tops:
            continue
        shutil.move(str(backup / entry), str(dest / entry))
        moved.append(entry)
    return moved


def undo_unmanaged_moves(dest: Path, backup: Path, moved: list[str]) -> None:
    for entry in reversed(moved):
        source = dest / entry
        if source.exists():
            shutil.move(str(source), str(backup / entry))


def install_copy_fresh(dest: Path, staging: Path, meta_path: Path, metadata: dict) -> None:
    os.rename(staging, dest)
    try:
        write_metadata(meta_path, metadata)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        shutil.rmtree(staging, ignore_errors=True)
        raise


def install_copy_update(
    dest: Path,
    staging: Path,
    meta_path: Path,
    metadata: dict,
    managed_tops: set[str],
) -> list[str]:
    """Swap a staged tree in, preserving unmanaged top-level entries."""
    backup = unique_sibling(dest, "backup")
    os.rename(dest, backup)
    moved: list[str] = []
    try:
        os.rename(staging, dest)
        moved = move_unmanaged_back(backup, dest, managed_tops)
        write_metadata(meta_path, metadata)
    except BaseException as exc:
        try:
            undo_unmanaged_moves(dest, backup, moved)
            os.rename(dest, staging)
            os.rename(backup, dest)
            shutil.rmtree(staging, ignore_errors=True)
        except BaseException as undo_exc:
            fail(f"rollback of {dest} failed: {undo_exc}")
        if not isinstance(exc, OSError):
            raise
        raise OSError(f"failed to install updated copy at {dest}: {exc}") from exc
    shutil.rmtree(backup)
    return moved


def install_all_copies(repo: Path, home: Path, metadata: dict) -> int:
    problems: list[str] = []
    plans: list[tuple[Asset, Path, Path, dict[str, str], list]] = []
    for asset in ASSETS:
        source = repo / asset.source_rel
        dest = home / asset.name
        rules = load_gitignore_rules(source)
        desired = inventory(source, rules)
        recorded = recorded_files(metadata, asset.name)
        # A source symlink hashes as its target string in the inventory, but
        # staging copies would follow the link and store the target's
        # content, silently corrupting the recorded state. Refuse instead.
        symlinked = sorted(rel for rel in desired if os.path.islink(source / rel))
        if symlinked:
            problems.append(
                f"{source}: source payload contains symlinks that copying "
                f"would follow instead of preserving: {', '.join(symlinked)}"
            )
        if dest.is_symlink():
            problems.append(
                f"{dest}: refusing to replace an existing symlink; "
                "remove it first or run without --copy"
            )
        elif dest.exists():
            if recorded:
                details = find_problems(dest, recorded, desired, rules)
                if details:
                    problems.append(f"{dest}: installed copy is not pristine:")
                    problems.extend(f"  {detail}" for detail in details)
            else:
                problems.append(f"{dest}: unmanaged path already exists")
        plans.append((asset, source, dest, desired, rules))

    if problems:
        for problem in problems:
            fail(problem)
        print(
            "setup-assets: refusing to change assets because of the problems above.\n"
            "Back up or move the listed paths, then rerun this command.",
            file=sys.stderr,
        )
        return 1

    for asset, source, dest, desired, rules in plans:
        staging = unique_sibling(dest, "staging")
        staging.mkdir()
        meta_path = metadata_path(home)
        recorded = recorded_files(metadata, asset.name)
        had_dest = dest.exists()
        # Only the refreshed payload's top-level names are managed during the
        # swap; every other backup entry is preserved, so a plugin that
        # upstream stops shipping (or an allowlisted local directory) is
        # never silently deleted.
        managed_tops = {rel.split("/", 1)[0] for rel in desired}
        metadata["assets"][asset.name] = {"source": str(source), "files": dict(desired)}
        try:
            stage_tree(source, desired, staging)
            if had_dest:
                moved = install_copy_update(dest, staging, meta_path, metadata, managed_tops)
                removed = len(recorded) - len(set(desired) & set(recorded))
                note(
                    f"refreshed copy at {dest} "
                    f"({len(set(desired) - set(recorded))} added, {removed} removed)"
                )
                if moved:
                    note(
                        f"preserved unmanaged top-level entries at {dest}: "
                        f"{', '.join(moved)}"
                    )
            else:
                install_copy_fresh(dest, staging, meta_path, metadata)
                note(f"copied {len(desired)} files to {dest}")
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            fail(str(exc))
            return 1
    return 0


# ---------------------------------------------------------------------------
# Symlink installation


def symlink_unsupported(exc: OSError) -> bool:
    if getattr(exc, "winerror", None) in _WINDOWS_SYMLINK_PRIVILEGE_WINERRORS:
        return True
    return getattr(exc, "errno", None) in _SYMLINK_UNSUPPORTED_ERRNOS


def symlinks_supported(home: Path) -> tuple[bool | None, str | None]:
    """Probe whether symlink creation works; None means a hard error."""
    probe = home / f".setup-assets-probe-{uuid.uuid4().hex[:8]}"
    try:
        os.symlink(str(probe) + ".missing-target", probe, target_is_directory=False)
    except OSError as exc:
        if symlink_unsupported(exc):
            return False, f"directory symlinks are not supported here ({exc})"
        return None, f"cannot create symlinks in {home}: {exc}"
    finally:
        if probe.is_symlink():
            probe.unlink()
    return True, None


def install_all_symlinks(repo: Path, home: Path, metadata: dict) -> int:
    problems: list[str] = []
    actions: list[tuple[str, Asset, Path, Path]] = []
    for asset in ASSETS:
        source = (repo / asset.source_rel).resolve()
        dest = home / asset.name
        if dest.is_symlink():
            if not dest.exists():
                problems.append(
                    f"{dest}: broken symlink; remove it and rerun this command"
                )
                continue
            if dest.resolve() != source:
                problems.append(
                    f"{dest}: unrelated symlink to {os.readlink(dest)}; refusing to replace"
                )
                continue
            actions.append(("already", asset, source, dest))
        elif dest.exists():
            problems.append(f"{dest}: unmanaged or copied path already exists")
        else:
            actions.append(("create", asset, source, dest))

    if problems:
        for problem in problems:
            fail(problem)
        print(
            "setup-assets: refusing to change assets because of the problems above.\n"
            "Back up or move the listed paths, then rerun this command.",
            file=sys.stderr,
        )
        return 1

    for action, asset, source, dest in actions:
        if action == "already":
            note(f"already linked {dest} -> {source}")
            continue
        if action == "create":
            try:
                os.symlink(str(source), dest, target_is_directory=True)
            except OSError as exc:
                fail(f"cannot create symlink {dest} -> {source}: {exc}")
                return 1
            note(f"linked {dest} -> {source}")
            continue
    return 0


# ---------------------------------------------------------------------------
# Source validation and entry point


def validate_sources(repo: Path) -> list[str]:
    errors: list[str] = []
    for asset in ASSETS:
        source = repo / asset.source_rel
        if not source.is_dir():
            errors.append(f"missing asset source directory: {source}")
            continue
        for rel in asset.required:
            path = source / rel
            if not path.is_file():
                errors.append(f"missing required asset file: {path}")
            elif os.name == "posix" and rel.endswith("-transform"):
                if not os.access(path, os.X_OK):
                    errors.append(f"required asset file is not executable: {path}")
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup_assets.py",
        description=(
            "Link or copy the catalog plugins, worker blueprints, and skills "
            "into the target jcode home (JCODE_HOME or ~/.jcode)."
        ),
        epilog=(
            "Limitations: the default mode symlinks whole directories, so the "
            "installation always reflects this repository exactly. With --copy "
            "the catalog payload is duplicated instead; copies are snapshots, "
            "so upstream updates only appear when this tool is rerun. Inside a "
            "managed directory any local edit or extra file blocks the update "
            "until you back up or move that directory. That includes ignored names such "
            "as swap files and __pycache__. Each asset is refreshed as one "
            "rolled-back unit; if a later asset fails, assets refreshed "
            "earlier in the same run stay updated. Top-level entries that "
            "are not part of the managed payload (a local plugin or skill "
            "directory, or one upstream no longer ships) are preserved "
            "untouched and reported during updates. Content ignored by the "
            "repository (such as plugin directories without an allowlist "
            "entry in plugins/.gitignore, for example the local "
            "plugins/agent-sudo plugin, or skill directories without an "
            "entry in skills/.gitignore, plus __pycache__, editor files, and "
            "test caches) is never "
            "copied. Existing unmanaged files and unrelated symlinks are never "
            "replaced. config.toml is never read or written, and the tool "
            "never requests elevation.\n"
            "\n"
            "Runtime requirements: the plugin adapters are python3 scripts "
            "using only the Python standard library, so a python3 on PATH is "
            "required to run them. The rtk plugin additionally requires the "
            "external rtk CLI on PATH and fails open when it is absent. "
            "Plugins only take effect when config.toml [hooks] points at the "
            "installed executables; this tool never edits that configuration."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="copy the payload instead of creating directory symlinks",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < (3, 14):
        fail("Python 3.14 or newer is required")
        return 1
    args = build_parser().parse_args(argv)
    repo = REPO_ROOT
    errors = validate_sources(repo)
    if errors:
        for error in errors:
            fail(error)
        return 1

    home = home_dir()
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"cannot create target home {home}: {exc}")
        return 1

    metadata = read_metadata(home)
    if metadata is None:
        return 1

    use_copy = args.copy or bool(metadata["assets"])
    if metadata["assets"] and not args.copy:
        note("keeping the existing copy installation mode")
    if not use_copy:
        supported, message = symlinks_supported(home)
        if supported is None:
            fail(message or "symlink support could not be determined")
            return 1
        if not supported:
            note(f"{message}; falling back to copying assets")
            use_copy = True

    if use_copy:
        return install_all_copies(repo, home, metadata)
    return install_all_symlinks(repo, home, metadata)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
