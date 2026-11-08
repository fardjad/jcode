"""Run patch validation with exclusive ownership and durable evidence."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .patch_catalog import CatalogError, fail, patches, repository_root, resolve_patch
from .validation_state import blob as patch_blob, digest, exclusive_lock, validation_identity
from .validate_patches import validate_catalog
from .isolate_config import isolated_environment
from .execution import atomic_json, stop_child
from .version_base import resolve_base
from .patch_workflow import execute


def batch_child(root: Path, name: str, expected: str, compat_each: bool = False) -> None:
    """Run a closure only after verifying the parent's exact preflight inputs."""
    base = resolve_base(root)
    if validation_identity(root, name, base) != expected:
        raise CatalogError("batch preflight inputs changed before child launch")
    with isolated_environment() as environment:
        original = os.environ.copy()
        os.environ.clear()
        os.environ.update(environment)
        try:
            execute(root, "test", name, preflight=False, compat_each=compat_each)
        finally:
            os.environ.clear()
            os.environ.update(original)


def should_skip(results, name, blob, force, fingerprint=None, compat_each=False):
    result = results.get(name, {})
    return (not force and fingerprint is not None and result.get("status") == "pass"
            and result.get("blob") == blob and result.get("fingerprint") == fingerprint
            and (not compat_each or result.get("compat_each") is True))


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


def run_all(root: Path, only=None, force=False, compat_each=False) -> int:
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
        fingerprints = {name: validation_identity(root, name, base) for name in sorted(catalog)}
        def current():
            return (set(patches(root)) == set(catalog) and resolve_base(root) == base
                    and all(validation_identity(root, name, base) == fingerprint
                            for name, fingerprint in fingerprints.items()))

        validate_catalog(root)
        if only:
            print("full-stack gate was not run (--only targeted run)")
        else:
            fingerprint = digest({"names": list(fingerprints), "validation": fingerprints})
            gate = results.get("__full_stack__", {})
            if force or gate.get("status") != "pass" or gate.get("fingerprint") != fingerprint:
                # No success claim survives a failed or interrupted gate attempt.
                gate = results["__full_stack__"] = {"fingerprint": fingerprint, "status": "fail"}
                save_results(results_path, results)
                execute(root, "gate", preflight=False)
                if not current():
                    raise CatalogError("full-stack inputs changed during validation, stop and rerun")
                gate["status"] = "pass"
                save_results(results_path, results)
        passed = 0
        for index, patch in enumerate(ordered, 1):
            name = patch.path.name
            blob = patch_blob(root, patch.path)
            fingerprint = fingerprints[name]
            if should_skip(results, name, blob, force, fingerprint, compat_each):
                passed += 1
                continue
            if resolve_base(root) != base or validation_identity(root, name, base) != fingerprint:
                raise CatalogError("validation inputs changed before launch, stop and rerun")
            log_path = directory / f"{patch.path.stem}.log"
            command = [sys.executable, "-c",
                       "from pathlib import Path; import sys; "
                       "from jcode_catalog.run_all_patch_tests import batch_child; "
                       "batch_child(Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4] == '1')",
                       str(root), name, fingerprint, str(int(compat_each))]
            record = {"blob": blob, "fingerprint": fingerprint, "base": base,
                      "status": "running", "started": time.time(), "command": command,
                       "log": str(log_path), "runner_pid": os.getpid(), "exit_code": None,
                       "compat_each": compat_each}
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
                passed += 1
            save_results(results_path, results)
            print(f'JCODE_PROGRESS {{"current":{index},"total":{len(ordered)},"message":"{name} {record["status"]}"}}', flush=True)
        # The combined gate depends on every patch, including skipped/unselected ones.
        inputs_current = current()
        print(f"patch tests: {passed} current passes, {len(ordered)} total")
        return 0 if passed == len(ordered) and inputs_current else 1


def main():
    parser = argparse.ArgumentParser(description=
        "Gate changed full-stack inputs, then check closures without a current pass. "
        "The tool-owned patch-validation worktree persists; rerunning resets failed source.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--only", nargs="+", help="Run selected closures without the full-stack gate.")
    parser.add_argument("--compat-each", action="store_true", help="Run compatibility for each closure (upstream syncs).")
    args = parser.parse_args()
    try:
        raise SystemExit(run_all(repository_root(), args.only, args.force, args.compat_each))
    except CatalogError as error:
        fail(str(error))


if __name__ == "__main__":
    main()
