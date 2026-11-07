"""Command-line interface for explicitly authorized live evaluations."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .comparison import compare_runs
from .runner import run
from .scenarios import Scenario, load_scenarios
from .settings import initialize, load_scenario_settings


def _selected_scenarios(
    scenarios_dir: Path, scenario_id: str | None
) -> tuple[list[Scenario], list[str]]:
    """Load every discovered scenario, then narrow to the selected one."""

    discovered = load_scenarios(scenarios_dir)
    if scenario_id is None:
        return discovered, [scenario.id for scenario in discovered]
    selected = [scenario for scenario in discovered if scenario.id == scenario_id]
    if not selected:
        raise ValueError(f"unknown scenario ID: {scenario_id}")
    return selected, [scenario.id for scenario in discovered]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jcode-eval",
        description=(
            "Run real-code evaluations with live coordinator/worker and judge calls. "
            "Provider use may incur cost."
        ),
        # No abbreviation: a stale --settings flag must not alias --config.
        allow_abbrev=False,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    init_parser = subparsers.add_parser(
        "init-config",
        help=(
            "Create the owner-only shared config file at CONFIG from the "
            "adjacent config.toml.example; per-scenario overrides live in "
            "the scenario config.toml settings table."
        ),
    )
    init_parser.add_argument("config", type=Path)
    run_parser = subparsers.add_parser(
        "run",
        help="Run correctness, evidence, tool-health, and diagnostic-judge stages.",
    )
    run_parser.add_argument("--binary", type=Path, required=True)
    run_parser.add_argument("--bridge", type=Path, required=True)
    run_parser.add_argument("--config", type=Path, required=True)
    run_parser.add_argument("--scenarios", type=Path, required=True)
    run_parser.add_argument("--blueprints", type=Path, required=True)
    run_parser.add_argument("--plugins", type=Path, required=True)
    run_parser.add_argument("--output", type=Path, required=True)
    run_parser.add_argument("--scenario")
    run_parser.add_argument("--build-provenance", type=Path)
    compare_parser = subparsers.add_parser(
        "compare", help="Compare two saved run summaries offline, without paid calls."
    )
    compare_parser.add_argument("baseline", type=Path)
    compare_parser.add_argument("candidate", type=Path)
    validate_parser = subparsers.add_parser(
        "validate-settings",
        help=(
            "Validate the shared config plus every selected scenario's "
            "TOML settings overrides without any build or paid call."
        ),
    )
    validate_parser.add_argument("--config", type=Path, required=True)
    validate_parser.add_argument("--scenarios", type=Path, required=True)
    validate_parser.add_argument("--scenario")
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            before = json.loads(args.baseline.read_text(encoding="utf-8"))
            after = json.loads(args.candidate.read_text(encoding="utf-8"))
            if not isinstance(before, dict) or not isinstance(after, dict):
                raise ValueError("run summaries must be JSON objects")
            print(json.dumps(compare_runs(before, after), indent=2))
            return 0
        if args.command == "init-config":
            path = initialize(args.config)
            print(f"Created {path} with owner-only permissions.")
            return 0
        if args.command == "validate-settings":
            selected, discovered_ids = _selected_scenarios(
                args.scenarios, args.scenario
            )
            load_scenario_settings(
                args.config,
                [scenario.id for scenario in selected],
                discovered_ids,
                {scenario.id: scenario.settings for scenario in selected},
            )
            return 0
        provenance = None
        if args.build_provenance:
            provenance = json.loads(args.build_provenance.read_text(encoding="utf-8"))
            if not isinstance(provenance, dict):
                raise ValueError("build provenance must be a JSON object")
        scenarios, discovered_ids = _selected_scenarios(args.scenarios, args.scenario)
        settings_by_scenario = load_scenario_settings(
            args.config,
            [scenario.id for scenario in scenarios],
            discovered_ids,
            {scenario.id: scenario.settings for scenario in scenarios},
        )
        return run(
            binary=args.binary.resolve(strict=True),
            bridge=args.bridge.resolve(strict=True),
            settings_by_scenario=settings_by_scenario,
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
