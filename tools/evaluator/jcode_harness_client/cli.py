"""Command-line interface for explicitly authorized live evaluations."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .runner import run
from .settings import initialize, load_settings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jcode-eval",
        description=(
            "Run real-code evaluations with live coordinator/worker and judge calls. "
            "Provider use may incur cost."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser(
        "init-settings",
        help="Create an owner-only .eval-settings.toml file in target directory.",
    )
    init_parser.add_argument(
        "directory",
        nargs="?",
        type=Path,
        default=Path.cwd(),
        help="Target directory (defaults to current working directory).",
    )
    run_parser = subparsers.add_parser(
        "run",
        help="Run correctness, evidence, tool-health, and diagnostic-judge stages.",
    )
    run_parser.add_argument("--binary", type=Path, required=True)
    run_parser.add_argument("--bridge", type=Path, required=True)
    run_parser.add_argument("--settings", type=Path, required=True)
    run_parser.add_argument("--scenarios", type=Path, required=True)
    run_parser.add_argument("--blueprints", type=Path, required=True)
    run_parser.add_argument("--plugins", type=Path, required=True)
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--scenario")
    run_parser.add_argument("--build-provenance", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "init-settings":
            target = (
                args.directory if args.directory.is_dir() else args.directory.parent
            )
            path = initialize(target)
            print(f"Created {path} with owner-only permissions.")
            return 0
        provenance = None
        if args.build_provenance:
            provenance = json.loads(args.build_provenance.read_text(encoding="utf-8"))
            if not isinstance(provenance, dict):
                raise ValueError("build provenance must be a JSON object")
        return run(
            binary=args.binary.resolve(strict=True),
            bridge=args.bridge.resolve(strict=True),
            settings=load_settings(args.settings),
            output=args.output.resolve(),
            scenario_path=args.scenarios,
            scenario_id=args.scenario,
            blueprints_dir=args.blueprints,
            plugins_dir=args.plugins,
            build_provenance=provenance,
        )
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
