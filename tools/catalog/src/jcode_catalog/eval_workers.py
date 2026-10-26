"""Hold the native build lock across one paid live worker evaluation."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import os
import subprocess
import sys

from .isolate_config import isolated_environment
from .patch_catalog import CatalogError, repository_root
from .prepare_worker_eval_build import evaluation_directory, prepare
from .validation_state import exclusive_lock
from .version_base import VersionError


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
    output = root / "workers" / "results" / run_id
    provenance = (
        evaluation_directory(root) / "provenance" / f"{run_id}.json"
    )
    provenance.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Keep builders out until native processes and acceptance have finished.
        with exclusive_lock(
            evaluation_directory(root) / "build.lock",
            "worker evaluation build and live run",
        ) as lock:
            build = prepare(root, held_lock=lock)
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
                "--settings",
                str(root / "workers" / ".eval-settings.toml"),
                "--scenarios",
                str(root / "workers" / "scenarios"),
                "--blueprints",
                str(root / "workers" / "blueprints"),
                "--plugins",
                str(root / "plugins"),
                "--build-provenance",
                str(provenance),
                "--output",
                str(output),
            ]
            if args.scenario:
                command += ["--scenario", args.scenario]
            with isolated_environment() as environment:
                result = subprocess.run(command, cwd=root, env=environment)
    except (CatalogError, VersionError, OSError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"evaluation evidence: {output}", file=sys.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
