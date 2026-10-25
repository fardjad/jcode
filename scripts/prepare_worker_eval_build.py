"""Build or reuse binaries for the exact current worker-evaluation catalog.

The JSON result contains binary paths and complete build provenance.  Catalog
materialization always uses .tools/worker-eval/worktree; .patched-jcode is never
an input to this workflow.
"""

from __future__ import annotations

from contextlib import contextmanager, nullcontext, redirect_stdout
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

import apply_patches
import patch_worktree
from isolate_config import isolated_environment
from patch_catalog import CatalogError, git, patches, repository_root
from validation_state import digest, exclusive_lock, source_cache
from version_base import VersionError, resolve_base


FINGERPRINT_SCHEMA = 1
STAMP_SCHEMA = 1
BUILD_HELPER_CONTRACT_VERSION = 1
BUILD_HELPER = "scripts/cargo_exec.sh"
BUILD_PROFILE = "debug"
CARGO_PROFILE = "dev"
BUILD_TARGETS = (
    ("jcode", "jcode"),
    ("jcode-harness-api-server", "jcode-harness-api-bridge"),
)


def evaluation_directory(root: Path) -> Path:
    return root / ".tools" / "worker-eval"


def evaluation_worktree(root: Path) -> Path:
    return evaluation_directory(root) / "worktree"


def attempt_directory(root: Path, fingerprint: str) -> Path:
    return evaluation_directory(root) / "builds" / fingerprint


def success_stamp_path(root: Path, fingerprint: str) -> Path:
    return attempt_directory(root, fingerprint) / "success.json"


def build_log_path(root: Path, fingerprint: str) -> Path:
    return attempt_directory(root, fingerprint) / "build.log"


def build_status_path(root: Path, fingerprint: str) -> Path:
    return attempt_directory(root, fingerprint) / "build-status.json"


def hash_file(path: Path) -> str:
    """Hash actual file bytes, including uncommitted catalog edits."""
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def build_description() -> dict:
    """Describe every native input not already owned by source or rustc."""
    cargo_args = ["build", "--profile", CARGO_PROFILE]
    for package, binary in BUILD_TARGETS:
        cargo_args += ["--package", package, "--bin", binary]
    return {
        "profile": BUILD_PROFILE,
        "cargo_profile": CARGO_PROFILE,
        "targets": [
            {"package": package, "binary": binary}
            for package, binary in BUILD_TARGETS
        ],
        "helper": {
            "path": BUILD_HELPER,
            "contract_version": BUILD_HELPER_CONTRACT_VERSION,
        },
        "cargo_args": cargo_args,
    }


def read_toolchain() -> dict:
    """Capture the full rustc version and its explicit host target."""
    with isolated_environment() as environment:
        result = subprocess.run(
            ["rustc", "-Vv"],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    if result.returncode:
        details = result.stderr.strip() or result.stdout.strip()
        raise CatalogError(f"rustc -Vv failed with exit status {result.returncode}: {details}")
    rustc_vv = result.stdout.strip()
    host = next(
        (line.split(":", 1)[1].strip() for line in rustc_vv.splitlines()
         if line.startswith("host:")),
        None,
    )
    if not rustc_vv or not host:
        raise CatalogError("rustc -Vv did not report a host target")
    return {"rustc_vv": rustc_vv, "host": host}


def current_inputs(root: Path) -> dict:
    """Resolve all source/build inputs, in full catalog application order."""
    catalog = patches(root)
    ordered = [catalog[name] for name in sorted(catalog)]
    return {
        "schema": FINGERPRINT_SCHEMA,
        "base": resolve_base(root),
        "patches": [
            {"name": patch.path.name, "sha256": hash_file(patch.path)}
            for patch in ordered
        ],
        "build": build_description(),
        "toolchain": read_toolchain(),
    }


def fingerprint_inputs(inputs: dict) -> str:
    return digest(inputs)


def current_fingerprint(root: Path) -> str:
    return fingerprint_inputs(current_inputs(root))


def source_state(worktree: Path) -> dict:
    """Return source identity only for a clean, non-ignored worktree."""
    worktree = worktree.resolve()
    status = git("status", "--porcelain", cwd=worktree)
    if status:
        raise CatalogError(f"worker evaluation source is dirty: {worktree}\n{status}")
    return {
        "worktree": str(worktree),
        "head": git("rev-parse", "HEAD", cwd=worktree),
        "tree": git("rev-parse", "HEAD^{tree}", cwd=worktree),
    }


def materialize(root: Path, base: str) -> Path:
    """Create/reset only a clean dedicated worktree and apply every patch."""
    destination = evaluation_worktree(root)
    # Existing helpers refuse dirty worktrees.  Keep their chatter off JSON stdout.
    with redirect_stdout(sys.stderr):
        worktree = patch_worktree.create(
            root,
            "worker-evaluation",
            base,
            destination=destination,
        )
        apply_patches.apply(root, worktree, None)
    return worktree.resolve()


def binary_paths(cargo_target: Path) -> dict[str, Path]:
    suffix = ".exe" if os.name == "nt" else ""
    return {
        binary: cargo_target / BUILD_PROFILE / f"{binary}{suffix}"
        for _, binary in BUILD_TARGETS
    }


def expected_binary_paths(root: Path, tree: str) -> dict[str, Path]:
    return binary_paths(
        root / ".tools" / "cargo-target" / "worker-evaluation" / tree
    )


def atomic_json(path: Path, payload: dict) -> None:
    """Durably replace a JSON record without exposing partial content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def read_valid_stamp(root: Path, fingerprint: str, inputs: dict) -> dict | None:
    """Return a fully verified reusable stamp, or request a rebuild."""
    path = success_stamp_path(root, fingerprint)
    try:
        stamp = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(stamp, dict):
            return None
        expected = {
            "schema": STAMP_SCHEMA,
            "input_schema": FINGERPRINT_SCHEMA,
            "status": "success",
            "fingerprint": fingerprint,
            "base": inputs["base"],
            "patches": inputs["patches"],
            "build": inputs["build"],
            "toolchain": inputs["toolchain"],
        }
        if any(stamp.get(name) != value for name, value in expected.items()):
            return None

        source = stamp["source"]
        dedicated = evaluation_worktree(root).resolve()
        if source["worktree"] != str(dedicated):
            return None
        if source_state(dedicated) != source:
            return None

        binaries = stamp["binaries"]
        expected_paths = expected_binary_paths(root, source["tree"])
        if set(binaries) != {binary for _, binary in BUILD_TARGETS}:
            return None
        for name, expected_path in expected_paths.items():
            record = binaries[name]
            if record["path"] != str(expected_path):
                return None
            if not expected_path.is_file() or hash_file(expected_path) != record["sha256"]:
                return None

        log_path = build_log_path(root, fingerprint)
        status_path = build_status_path(root, fingerprint)
        if stamp["build_log"] != str(log_path) or not log_path.is_file():
            return None
        if stamp["build_status"] != str(status_path):
            return None
        attempt = json.loads(status_path.read_text(encoding="utf-8"))
        if (attempt.get("fingerprint") != fingerprint or
                attempt.get("status") != "success" or
                attempt.get("exit_code") != 0):
            return None
        return stamp
    except (OSError, ValueError, TypeError, KeyError, AttributeError, CatalogError):
        return None


def write_attempt(attempt: dict) -> None:
    atomic_json(Path(attempt["status_path"]), attempt)


def finish_log(log, status: str, exit_code: int | None) -> None:
    log.write(f"worker-eval-build-status: {status}\n")
    log.write(f"worker-eval-exit-status: {exit_code}\n")
    log.flush()
    os.fsync(log.fileno())


def run_build(
    root: Path,
    worktree: Path,
    cargo_target: Path,
    fingerprint: str,
    inputs: dict,
    lock,
) -> dict:
    """Run the isolated helper while retaining a durable attempt and log."""
    directory = attempt_directory(root, fingerprint)
    directory.mkdir(parents=True, exist_ok=True)
    log_path = build_log_path(root, fingerprint)
    status_path = build_status_path(root, fingerprint)
    command = ["bash", str(worktree / BUILD_HELPER), *inputs["build"]["cargo_args"]]
    attempt = {
        "schema": 1,
        "fingerprint": fingerprint,
        "status": "running",
        "started": time.time(),
        "finished": None,
        "pid": None,
        "runner_pid": os.getpid(),
        "exit_code": None,
        "command": command,
        "cwd": str(worktree),
        "log": str(log_path),
        "status_path": str(status_path),
    }
    write_attempt(attempt)

    with isolated_environment() as environment:
        environment["CARGO_TARGET_DIR"] = str(cargo_target)
        environment["JCODE_CATALOG_ROOT"] = str(root)
        with log_path.open("w", encoding="utf-8") as log:
            log.write(json.dumps({
                "fingerprint": fingerprint,
                "command": command,
                "cwd": str(worktree),
                "cargo_target_dir": str(cargo_target),
            }, sort_keys=True) + "\n")
            log.flush()
            child = None
            try:
                options = {"pass_fds": (lock.fileno(),)} if os.name != "nt" else {}
                child = subprocess.Popen(
                    command,
                    cwd=worktree,
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=os.name != "nt",
                    **options,
                )
                attempt["pid"] = child.pid
                write_attempt(attempt)
                exit_code = child.wait()
            except KeyboardInterrupt:
                exit_code = 130
                if child is not None:
                    if os.name != "nt":
                        try:
                            os.killpg(child.pid, signal.SIGTERM)
                        except ProcessLookupError:
                            pass
                    else:
                        child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        if os.name != "nt":
                            os.killpg(child.pid, signal.SIGKILL)
                        else:
                            child.kill()
                        child.wait()
                    exit_code = child.returncode
                attempt.update(
                    status="interrupted",
                    exit_code=exit_code,
                    finished=time.time(),
                )
                finish_log(log, "interrupted", exit_code)
                write_attempt(attempt)
                raise
            except OSError as error:
                log.write(f"build launch failed: {error}\n")
                exit_code = 127

            attempt.update(
                status="built" if exit_code == 0 else "failed",
                exit_code=exit_code,
                finished=time.time(),
            )
            finish_log(log, attempt["status"], exit_code)
            write_attempt(attempt)
            return attempt


def reject_attempt(attempt: dict, reason: str) -> None:
    attempt.update(status="rejected", rejection=reason, finished=time.time())
    write_attempt(attempt)
    with Path(attempt["log"]).open("a", encoding="utf-8") as log:
        log.write(f"worker-eval-build-rejected: {reason}\n")
        log.flush()
        os.fsync(log.fileno())


def provenance(stamp: dict, stamp_path: Path, action: str) -> dict:
    result = deepcopy(stamp)
    result.update({
        "stamp": str(stamp_path),
        "action": action,
        "built": action == "built",
        "reused": action == "reused",
        "binary_paths": {
            name: record["path"] for name, record in stamp["binaries"].items()
        },
    })
    return result


def prepare(root: Path, held_lock=None) -> dict:
    """Build or reuse the exact current catalog while holding the build lock.

    The recipe may hold the same lock across its live run to prevent another
    builder from replacing source/binaries during evaluation.
    """
    root = root.resolve()
    lock_path = evaluation_directory(root) / "build.lock"
    context = (
        exclusive_lock(lock_path, "worker evaluation build")
        if held_lock is None
        else nullcontext(held_lock)
    )
    with context as lock:
        inputs = current_inputs(root)
        fingerprint = fingerprint_inputs(inputs)
        stamp_path = success_stamp_path(root, fingerprint)
        stamp = read_valid_stamp(root, fingerprint, inputs)
        if stamp is not None:
            return provenance(stamp, stamp_path, "reused")

        # An invalid/stale success claim must not survive a failed retry.
        stamp_path.unlink(missing_ok=True)
        worktree = materialize(root, inputs["base"])
        initial_source = source_state(worktree)
        if current_inputs(root) != inputs:
            raise CatalogError("catalog or toolchain changed during source materialization")

        cargo_target = source_cache(root, worktree, "worker-evaluation")
        attempt = run_build(root, worktree, cargo_target, fingerprint, inputs, lock)
        if attempt["exit_code"] != 0:
            raise CatalogError(
                f"worker evaluation build failed with exit status {attempt['exit_code']}; "
                f"log={attempt['log']}; status={attempt['status_path']}"
            )

        try:
            if current_inputs(root) != inputs:
                raise CatalogError("catalog or toolchain changed during worker evaluation build")
            final_source = source_state(worktree)
            if final_source != initial_source:
                raise CatalogError("worker evaluation source HEAD/tree changed during build")
            actual_paths = binary_paths(cargo_target)
            if actual_paths != expected_binary_paths(root, final_source["tree"]):
                raise CatalogError("worker evaluation cache is not keyed by the clean source tree")
            binaries = {
                name: {"path": str(path), "sha256": hash_file(path)}
                for name, path in actual_paths.items()
            }
        except (CatalogError, VersionError, OSError) as error:
            reject_attempt(attempt, str(error))
            if isinstance(error, CatalogError):
                raise
            raise CatalogError(f"cannot verify worker evaluation build: {error}") from error

        attempt.update(status="success", source=final_source, binaries=binaries)
        write_attempt(attempt)
        stamp = {
            "schema": STAMP_SCHEMA,
            "input_schema": FINGERPRINT_SCHEMA,
            "status": "success",
            "created": time.time(),
            "fingerprint": fingerprint,
            "base": inputs["base"],
            "patches": inputs["patches"],
            "build": inputs["build"],
            "toolchain": inputs["toolchain"],
            "source": final_source,
            "binaries": binaries,
            "command": attempt["command"],
            "build_log": attempt["log"],
            "build_status": attempt["status_path"],
        }
        atomic_json(stamp_path, stamp)
        return provenance(stamp, stamp_path, "built")


@contextmanager
def diagnostics_to_stderr():
    """Redirect Python and inherited subprocess stdout away from the JSON stream."""
    sys.stdout.flush()
    saved_stdout = os.dup(1)
    try:
        os.dup2(2, 1)
        with redirect_stdout(sys.stderr):
            yield
    finally:
        sys.stdout.flush()
        os.dup2(saved_stdout, 1)
        os.close(saved_stdout)


def main() -> int:
    if len(sys.argv) != 1:
        print(f"usage: {Path(sys.argv[0]).name}", file=sys.stderr)
        return 2
    try:
        # Helper diagnostics belong on stderr; stdout is one machine-readable result.
        with diagnostics_to_stderr():
            result = prepare(repository_root())
    except KeyboardInterrupt:
        print("ERROR: worker evaluation build interrupted; no success stamp written", file=sys.stderr)
        return 130
    except (CatalogError, VersionError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
