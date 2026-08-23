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

NEXTEST_VERSION = "0.9.143"


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
        return names
    except (AttributeError, OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise RuntimeError(f"invalid nextest exclusions {path}: {error}") from error


def run(binary: str, root: Path, worktree: Path, names: list[str]) -> int:
    environment = os.environ.copy()
    environment["JCODE_DEV_FEATURE_PROFILE"] = "minimal"
    command = [binary, "nextest", "run", "--manifest-path", str(worktree / "Cargo.toml"), "--config-file", str(root / "nextest.toml"), "--profile", "compat", "--lib", "--bin", "jcode"]
    if names:
        command += ["--filterset", " and ".join(f"not test(={name})" for name in names)]
    return subprocess.run(command, env=environment).returncode


def main() -> int:
    if len(sys.argv) != 5 or sys.argv[1] not in {"learn", "test"} or sys.argv[3] != "--base":
        print(f"usage: {Path(sys.argv[0]).name} (learn|test) WORKTREE --base COMMIT", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parent.parent
    mode, worktree, base = sys.argv[1], Path(sys.argv[2]).resolve(), sys.argv[4]
    # Keep argument parsing explicit while accepting only the documented shape.
    if not base:
        print("missing base commit", file=sys.stderr)
        return 2
    report = worktree / "target" / "nextest" / "compat" / "junit.xml"
    try:
        binary = subprocess.check_output([sys.executable, str(root / "scripts" / "bootstrap_nextest.py")], text=True).strip()
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
