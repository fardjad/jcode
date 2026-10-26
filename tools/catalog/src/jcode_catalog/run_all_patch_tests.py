"""Run patch validation with exclusive ownership and durable evidence."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .patch_catalog import CatalogError, fail, patches, repository_root, resolve_patch
from .validation_state import blob as patch_blob, exclusive_lock, validation_identity
from .validate_patches import validate_catalog
from .isolate_config import isolated_environment
from .execution import atomic_json, stop_child
from .version_base import resolve_base


def batch_child(root: Path, name: str, expected: str) -> None:
    """Run a closure only after verifying the parent's exact preflight inputs."""
    base = resolve_base(root)
    if validation_identity(root, name, base) != expected:
        raise CatalogError("batch preflight inputs changed before child launch")
    from .patch_workflow import execute
    with isolated_environment() as environment:
        original = os.environ.copy()
        os.environ.clear()
        os.environ.update(environment)
        try:
            execute(root, "test", name, preflight=False)
        finally:
            os.environ.clear()
            os.environ.update(original)


def should_skip(results, name, blob, force, fingerprint=None):
    result = results.get(name, {})
    return (not force and fingerprint is not None and result.get("status") == "pass"
            and result.get("blob") == blob and result.get("fingerprint") == fingerprint)


def retained_worktree_path(output: str) -> str | None:
    for line in output.splitlines():
        marker = "worktree retained: "
        if marker in line:
            return line.split(marker, 1)[1].strip() or None
    return None


def save_results(path: Path, results: dict) -> None:
    atomic_json(path, results)


def load_results(path):
    if not path.exists():
        return {}
    try:
        results = json.loads(path.read_text())
        if not isinstance(results, dict) or any(not isinstance(v, dict) for v in results.values()):
            raise ValueError("results must map patch names to records")
        return results
    except (OSError, ValueError) as error:
        raise CatalogError(f"invalid results {path}: {error}") from error


def run_all(root: Path, only=None, force=False, audit=False) -> int:
    base = resolve_base(root)
    directory = root / ".tools/patch-tests" / base
    directory.mkdir(parents=True, exist_ok=True)
    # One catalog runner across pins, since worktree names are shared across pins.
    with exclusive_lock(root / ".tools/patch-tests/runner.lock", "patch validation") as lock:
        for ledger in (root / ".tools/patch-tests").glob("*/results.json"):
            for name, record in load_results(ledger).items():
                if record.get("status") == "running" and record.get("pid"):
                    try:
                        os.kill(int(record["pid"]), 0)
                    except ProcessLookupError:
                        pass
                    except (OSError, ValueError) as error:
                        raise CatalogError(f"cannot establish stopped runner child: {name}, {ledger}") from error
                    else:
                        raise CatalogError(f"prior validation child may still be active: {name}, "
                                           f"pid={record['pid']}, log={record.get('log')}")
        catalog = patches(root)
        selected = {resolve_patch(root, name).name for name in only} if only else set(catalog)
        ordered = [catalog[name] for name in sorted(selected)]
        results_path = directory / "results.json"
        results = load_results(results_path)
        fingerprints = {p.path.name: validation_identity(root, p.path.name, base) for p in ordered}
        if not audit and any(not should_skip(results, p.path.name, patch_blob(root, p.path),
                                             force, fingerprints[p.path.name]) for p in ordered):
            validate_catalog(root)
        passed = 0
        for index, patch in enumerate(ordered, 1):
            name = patch.path.name
            blob = patch_blob(root, patch.path)
            fingerprint = fingerprints[name]
            if should_skip(results, name, blob, force, fingerprint):
                passed += 1
                continue
            if audit:
                print(f"not current: {name}")
                continue
            if resolve_base(root) != base or validation_identity(root, name, base) != fingerprint:
                raise CatalogError("validation inputs changed before launch, stop and rerun")
            log_path = directory / f"{patch.path.stem}.log"
            command = [sys.executable, "-c",
                       "from pathlib import Path; import sys; "
                       "from jcode_catalog.run_all_patch_tests import batch_child; "
                       "batch_child(Path(sys.argv[1]), sys.argv[2], sys.argv[3])",
                       str(root), name, fingerprint]
            record = {"blob": blob, "fingerprint": fingerprint, "base": base,
                      "status": "running", "started": time.time(), "command": command,
                      "log": str(log_path), "runner_pid": os.getpid(), "exit_code": None}
            results[name] = record
            save_results(results_path, results)
            with log_path.open("w", encoding="utf-8") as log:
                try:
                    options = {"pass_fds": (lock.fileno(),)} if os.name != "nt" else {}
                    child = subprocess.Popen(command, cwd=root, stdout=log, stderr=subprocess.STDOUT,
                                             start_new_session=os.name != "nt", **options)
                    record["pid"] = child.pid
                    save_results(results_path, results)
                    try:
                        code = child.wait()
                    except KeyboardInterrupt:
                        # Do not release resource ownership while a build survives.
                        stop_child(child)
                        record.update(status="interrupted", exit_code=child.returncode,
                                      finished=time.time())
                        save_results(results_path, results)
                        raise
                except OSError as error:
                    log.write(f"launch failed: {error}\n")
                    code = 127
                record.update(exit_code=code, finished=time.time(), status="pass" if code == 0 else "fail")
                if resolve_base(root) != base or validation_identity(root, name, base) != fingerprint:
                    record["status"] = "stale"
                    log.write("inputs changed during validation, result rejected\n")
                log.flush()
                os.fsync(log.fileno())
            if record["status"] == "pass":
                worktree = retained_worktree_path(log_path.read_text())
                if worktree:
                    with log_path.open("a") as log:
                        cleanup = subprocess.run(["git", "worktree", "remove", "--force", worktree],
                                                 cwd=root, stdout=log, stderr=subprocess.STDOUT)
                    record["cleanup_exit_code"] = cleanup.returncode
                    if cleanup.returncode:
                        record["status"] = "fail"
                if record["status"] == "pass":
                    passed += 1
            save_results(results_path, results)
            print(f'JCODE_PROGRESS {{"current":{index},"total":{len(ordered)},"message":"{name} {record["status"]}"}}', flush=True)
        # Recheck every selected input, including ones skipped earlier.
        current = all(resolve_base(root) == base and validation_identity(root, p.path.name, base)
                      == fingerprints[p.path.name] for p in ordered)
        print(f"patch tests: {passed} current passes, {len(ordered)} total")
        return 0 if passed == len(ordered) and current else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--only", nargs="+")
    parser.add_argument("--audit", action="store_true", help="Check exact cached acceptance without running tests")
    args = parser.parse_args()
    try:
        raise SystemExit(run_all(repository_root(), args.only, args.force, args.audit))
    except CatalogError as error:
        fail(str(error))


if __name__ == "__main__":
    main()
