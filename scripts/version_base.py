"""Resolve the catalog's stable upstream release pin."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


UPSTREAM_URL = "https://github.com/1jehuang/jcode.git"
STABLE_RELEASE = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+\Z")


class VersionError(RuntimeError):
    """Raised when the catalog release pin cannot be resolved safely."""


def git(*args: str, cwd: Path, capture: bool = True) -> str:
    """Run Git and convert failures to a version-specific error."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, check=True, text=True,
            stdout=subprocess.PIPE if capture else None,
        )
    except subprocess.CalledProcessError as error:
        raise VersionError(f"git {' '.join(args)} failed") from error
    return result.stdout.strip() if capture else ""


def require_stable_release(value: str) -> str:
    """Accept only stable vX.Y.Z release names."""
    if not STABLE_RELEASE.fullmatch(value):
        raise VersionError(f"not a stable release tag: {value}")
    return value


def pinned_version(root: Path) -> str:
    """Read and validate VERSION.txt."""
    path = root / "VERSION.txt"
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise VersionError(f"cannot read catalog version: {path}") from error
    return require_stable_release(value)


def resolve_release(root: Path, release: str, fetch: bool = False) -> str:
    """Resolve a stable tag through a private local ref, fetching canonical source when requested."""
    release = require_stable_release(release)
    ref = f"refs/jcode/upstream-tags/{release}"
    if fetch or subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", ref], cwd=root,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ).returncode != 0:
        git(
            "fetch", "--no-tags", UPSTREAM_URL,
            f"+refs/tags/{release}:{ref}", cwd=root, capture=False,
        )
    return git("rev-parse", f"{ref}^{{commit}}", cwd=root)


def resolve_base(root: Path, fetch_release: bool = False) -> str:
    """Resolve the catalog's pinned stable release."""
    return resolve_release(root, pinned_version(root), fetch=fetch_release)


def latest_release(root: Path) -> str:
    """Select the latest stable tag published by canonical upstream."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", UPSTREAM_URL],
            cwd=root, check=True, text=True, stdout=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as error:
        raise VersionError("could not list canonical jcode release tags") from error
    releases = [
        line.split("refs/tags/", 1)[1]
        for line in result.stdout.splitlines()
        if "refs/tags/" in line
    ]
    return latest_stable_release(releases)


def latest_stable_release(releases: list[str]) -> str:
    """Return the highest stable vX.Y.Z from tag names."""
    releases = [release for release in releases if STABLE_RELEASE.fullmatch(release)]
    if not releases:
        raise VersionError("canonical jcode repository has no stable vX.Y.Z tags")
    return max(releases, key=lambda item: tuple(int(part) for part in item[1:].split(".")))


def write_version(root: Path, release: str) -> None:
    """Atomically update the catalog's stable release pin."""
    release = require_stable_release(release)
    destination = root / "VERSION.txt"
    fd, temporary_name = tempfile.mkstemp(prefix=".VERSION.txt.", dir=root, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(f"{release}\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, destination)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("pinned")
    commands.add_parser("latest")
    resolve_parser = commands.add_parser("resolve")
    resolve_parser.add_argument("release", nargs="?")
    resolve_parser.add_argument("--fetch", action="store_true")
    write_parser = commands.add_parser("write")
    write_parser.add_argument("release")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    try:
        if args.command == "pinned":
            result = pinned_version(root)
        elif args.command == "latest":
            result = latest_release(root)
        elif args.command == "resolve":
            result = (
                resolve_base(root, fetch_release=args.fetch)
                if args.release is None
                else resolve_release(root, args.release, fetch=args.fetch)
            )
        else:
            write_version(root, args.release)
            result = args.release
    except VersionError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    print(result)


if __name__ == "__main__":
    main()
