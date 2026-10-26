#!/usr/bin/env python3
"""Learn and use pinned compatibility-test exclusions."""

from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .bootstrap_nextest import VERSION as NEXTEST_VERSION
from .validation_state import exclusive_lock, source_cache
from .isolate_config import isolated_environment


def config_path(root: Path, base: str) -> Path:
    return root / ".tools" / "nextest" / "exclusions" / f"{base}.json"


def failed_test_names(report: Path) -> list[str]:
    try:
        document = ET.parse(report)
    except (OSError, ET.ParseError) as error:
        raise RuntimeError(f"cannot read JUnit report {report}: {error}") from error
    names: list[str] = []
    for testcase in document.iter():
        if testcase.tag.rsplit("}", 1)[-1] != "testcase":
            continue
        failed = any(child.tag.rsplit("}", 1)[-1] in {"failure", "error"} for child in testcase)
        if not failed:
            continue
        name = testcase.get("name", "")
        if not name or name != name.strip() or any(ord(char) < 32 for char in name):
            raise RuntimeError(f"JUnit failure has invalid test name: {name!r}")
        if name in names:
            raise RuntimeError(f"JUnit failure has ambiguous duplicate test name: {name!r}")
        names.append(name)
    if not names:
        for suite in document.iter():
            if suite.tag.rsplit("}", 1)[-1] not in {"testsuite", "testsuites"}:
                continue
            try:
                has_failures = int(suite.get("failures", "0")) or int(suite.get("errors", "0"))
            except ValueError as error:
                raise RuntimeError("JUnit has invalid failure counts") from error
            if has_failures:
                raise RuntimeError("JUnit reports failures but no usable failed test names")
    return sorted(names)


def write_config(root: Path, base: str, names: list[str]) -> Path:
    destination = config_path(root, base)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "nextest_version": NEXTEST_VERSION,
        "nextest_pin": NEXTEST_VERSION,
        "base_commit": base,
        "failed_tests": names,
    }
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        json.dump(payload, temporary, indent=2)
        temporary.write("\n")
        temporary.flush()
        os.fsync(temporary.fileno())
    temporary_path.replace(destination)
    return destination


def load_names(root: Path, base: str) -> list[str] | None:
    path = config_path(root, base)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("base_commit") != base or payload.get("nextest_version") != NEXTEST_VERSION:
            return None
        names = payload["failed_tests"]
        if not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names):
            raise ValueError("failed_tests must contain non-empty strings")
        if names != sorted(set(names)):
            raise ValueError("failed_tests must be sorted and unique")
        validation_names = payload.get("failed_validation_tests", [])
        if (not isinstance(validation_names, list) or
                any(not isinstance(name, str) or not name for name in validation_names)):
            raise ValueError("failed_validation_tests must contain non-empty strings")
        if validation_names != sorted(set(validation_names)):
            raise ValueError("failed_validation_tests must be sorted and unique")
        return names
    except (AttributeError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f"invalid nextest exclusions {path}: {error}") from error


def load_validation_names(root: Path, base: str) -> list[str]:
    """Load clean-upstream Validation command exclusions for a base."""
    path = config_path(root, base)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("base_commit") != base or payload.get("nextest_version") != NEXTEST_VERSION:
            return []
        names = payload.get("failed_validation_tests", [])
        if not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names):
            raise ValueError("failed_validation_tests must contain non-empty strings")
        if names != sorted(set(names)):
            raise ValueError("failed_validation_tests must be sorted and unique")
        return names
    except (AttributeError, OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise RuntimeError(f"invalid nextest exclusions {path}: {error}") from error


def run(binary: str, root: Path, worktree: Path, names: list[str]) -> int:
    command = [binary, "nextest", "run", "--manifest-path", str(worktree / "Cargo.toml"), "--config-file", str(root / "nextest.toml"), "--profile", "compat", "--lib", "--bin", "jcode"]
    if names:
        command += ["--filterset", " and ".join(f"not test(={name})" for name in names)]
    with isolated_environment() as environment:
        environment["JCODE_DEV_FEATURE_PROFILE"] = "minimal"
        cargo_target = source_cache(root, worktree, "compatibility")
        environment["CARGO_TARGET_DIR"] = str(cargo_target)
        # Preserve the public report location while separating compiled artifacts.
        with exclusive_lock(cargo_target / "catalog.lock", "compatibility cache"):
            report = cargo_target / "nextest/compat/junit.xml"
            destination = worktree / "target/nextest/compat/junit.xml"
            report.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
            code = subprocess.run(command, env=environment).returncode
            # nextest may keep its store at workspace/target despite Cargo's
            # artifact override. Accept either actual location, never old data.
            if report.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(report.read_bytes())
            return code


def main() -> int:
    if len(sys.argv) != 5 or sys.argv[1] not in {"learn", "test"} or sys.argv[3] != "--base":
        print(f"usage: {Path(sys.argv[0]).name} (learn|test) WORKTREE --base COMMIT", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[4]
    mode, worktree, base = sys.argv[1], Path(sys.argv[2]).resolve(), sys.argv[4]
    # Keep argument parsing explicit while accepting only the documented shape.
    if not base:
        print("missing base commit", file=sys.stderr)
        return 2
    return execute(root, mode, worktree, base)


def execute(root: Path, mode: str, worktree: Path, base: str) -> int:
    """Run compatibility testing or clean-upstream exclusion learning."""
    report = worktree / "target" / "nextest" / "compat" / "junit.xml"
    try:
        with isolated_environment() as environment:
            binary = subprocess.check_output(
                [sys.executable, "-m", "jcode_catalog.bootstrap_nextest"],
                env=environment,
                text=True,
            ).strip()
        report.unlink(missing_ok=True)
        names = [] if mode == "learn" else (load_names(root, base) or [])
        print(f"nextest exclusions: {'loaded' if names else 'none'} ({len(names)} tests)")
        result = run(binary, root, worktree, names)
        if mode == "learn":
            names = failed_test_names(report)
            if result != 0 and not names:
                raise RuntimeError("nextest failed but JUnit contains no failed tests")
            path = write_config(root, base, names)
            print(f"learned exclusions ({len(names)}):", *names, sep="\n")
            print(f"learned exclusions path: {path}")
            return 0
        return result
    except (OSError, subprocess.CalledProcessError, RuntimeError) as error:
        print(f"nextest: {error}", file=sys.stderr)
        return 1
    finally:
        print(f"JUnit report: {report}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
