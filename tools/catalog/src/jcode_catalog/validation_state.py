"""Identities and process-held locks for catalog validation."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

from .patch_catalog import CatalogError, dependency_order, git, resolve_patch


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def blob(root: Path, path: Path) -> str:
    return git("hash-object", str(path), cwd=root)


def closure_identity(root: Path, name: str, base: str) -> dict:
    closure = dependency_order(root, resolve_patch(root, name))
    return {"base": base, "patches": {p.path.name: blob(root, p.path) for p in closure}}


def validation_identity(root: Path, name: str, base: str) -> str:
    # Tooling, baseline exclusions, and toolchain changes invalidate cached passes.
    files = [root / "justfile", root / "nextest.toml"]
    files += sorted((root / "tools/scripts").glob("*.py"))
    files += sorted((root / "tools/catalog/src/jcode_catalog").glob("*.py"))
    files += [root / "tools/catalog/pyproject.toml", root / "tools/catalog/uv.lock"]
    exclusions = root / ".tools/nextest/exclusions" / f"{base}.json"
    inputs = {str(p.relative_to(root)): blob(root, p) for p in files if p.is_file()}
    inputs["exclusions"] = blob(root, exclusions) if exclusions.is_file() else None
    rustc = subprocess.check_output(["rustc", "-Vv"], text=True).strip()
    return digest({"closure": closure_identity(root, name, base), "workflow": inputs,
                   "rustc": rustc, "schema": 1})


def source_cache(root: Path, worktree: Path, purpose: str) -> Path:
    # Clean tree identity, not a mutable shared pathname, owns compiler metadata.
    if git("status", "--porcelain", "--untracked-files=no", cwd=worktree):
        raise CatalogError("validation requires clean tracked source before selecting a cache")
    tree = git("rev-parse", "HEAD^{tree}", cwd=worktree)
    destination = root / ".tools/cargo-target" / purpose / tree
    destination.mkdir(parents=True, exist_ok=True)
    return destination


@contextmanager
def exclusive_lock(path: Path, label: str):
    """Nonblocking OS lock, released even after process death. Never unlink it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(" ")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as error:
            handle.seek(0)
            owner = handle.read().strip()
            raise CatalogError(f"{label} already running: {path}, owner={owner}") from error
        try:
            handle.seek(0)
            handle.truncate()
            json.dump({"pid": os.getpid(), "started": time.time(), "label": label}, handle)
            handle.flush()
            os.fsync(handle.fileno())
            yield handle
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            # On Unix close releases this process's reference. A passed child
            # descriptor keeps the same lock held until that child also exits.
