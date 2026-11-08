"""Hold the native build lock across one paid live worker evaluation."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from .isolate_config import isolated_environment
from .patch_catalog import CatalogError, repository_root
from .prepare_worker_eval_build import evaluation_directory, prepare
from .validation_state import exclusive_lock
from .version_base import VersionError


def _evaluator_python(root) -> Path:
    return root / "tools" / "evaluator" / ".venv" / "bin" / "python"


def _evals_dir(root) -> Path:
    """The worker evaluation layout root: config, scenarios, and results."""

    return root / "customizations" / "workers" / "evals"


def _config_file(root) -> Path:
    return _evals_dir(root) / "config.toml"


def _scenarios_dir(root) -> Path:
    return _evals_dir(root) / "scenarios"


def preflight_settings(root, scenario: str | None = None) -> int:
    """Validate selected settings files before any native build cost."""

    python = _evaluator_python(root)
    if not python.is_file():
        raise CatalogError(
            "settings preflight needs: cd tools/evaluator && uv sync --extra eval"
        )
    command = [
        str(python),
        "-m",
        "jcode_harness_client",
        "validate-settings",
        "--config",
        str(_config_file(root)),
        "--scenarios",
        str(_scenarios_dir(root)),
    ]
    if scenario:
        command += ["--scenario", scenario]
    with isolated_environment() as environment:
        return subprocess.run(
            command, cwd=root, env=environment, check=False
        ).returncode


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build/reuse current jcode and run paid worker evaluations."
    )
    parser.add_argument("--scenario")
    args = parser.parse_args()
    root = repository_root()
    run_id = (
        "worker-eval-"
        + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        + f"-{os.getpid()}"
    )
    output = _evals_dir(root) / "results" / run_id
    provenance = evaluation_directory(root) / "provenance" / f"{run_id}.json"
    provenance.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Config errors must surface before any native build cost.
        if preflight_settings(root, args.scenario) != 0:
            print("settings preflight failed; no build was started", file=sys.stderr)
            return 1
        # Keep builders out until native processes and acceptance have finished.
        with exclusive_lock(
            evaluation_directory(root) / "build.lock",
            "worker evaluation build and live run",
        ) as lock:
            build_started = time.monotonic()
            build = prepare(root, held_lock=lock)
            build["build_check_seconds"] = time.monotonic() - build_started
            provenance.write_text(
                json.dumps(build, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            print(
                f"native build {build['action']}: {build['fingerprint']}",
                file=sys.stderr,
            )
            command = [
                str(root / "tools" / "evaluator" / ".venv" / "bin" / "python"),
                "-m",
                "jcode_harness_client",
                "run",
                "--binary",
                build["binary_paths"]["jcode"],
                "--bridge",
                build["binary_paths"]["jcode-harness-api-bridge"],
                "--config",
                str(_config_file(root)),
                "--scenarios",
                str(_scenarios_dir(root)),
                "--blueprints",
                str(root / "customizations" / "workers" / "blueprints"),
                "--plugins",
                str(root / "customizations" / "plugins"),
                "--build-provenance",
                str(provenance),
                "--output",
                str(output),
            ]
            if args.scenario:
                command += ["--scenario", args.scenario]
            with isolated_environment() as environment:
                result = subprocess.run(command, cwd=root, env=environment, check=False)
    except (CatalogError, VersionError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"evaluation evidence: {output}", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
