#!/usr/bin/env python3
"""Contract checks for catalog worker blueprints."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXER = ROOT / "workers" / "blueprints" / "fixer.md"
COORDINATOR = ROOT / "workers" / "blueprints" / "coordinator.md"
README = ROOT / "workers" / "README.md"


def require(text: str, fragment: str, label: str) -> None:
    if fragment not in text:
        raise AssertionError(f"{label}: missing {fragment!r}")


def main() -> None:
    fixer = FIXER.read_text()
    coordinator = COORDINATOR.read_text()
    readme = README.read_text()

    require(fixer, "name: fixer", "fixer identity")
    require(fixer, "communication-policy: report-to-parent", "fixer reporting")
    require(fixer, "Exactly one concrete mechanical task", "single-task admission")
    require(fixer, "explicit output contract", "output-contract admission")
    require(fixer, "Do not plan, reason, analyze", "no-reasoning constraint")
    require(fixer, "Do not choose an approach", "coordinator-owned decisions")
    require(fixer, "Run only the exact validation command", "coordinator-owned validation")
    require(fixer, "Return only the coordinator's requested output contract", "bounded report")
    require(fixer, "ESCALATION: need one explicit mechanical task", "ambiguous-task escalation")

    forbidden_tools = ("mcp", "mcp_search", "mcp_call", "skill_manage", "webfetch")
    front_matter = fixer.split("---", 2)[1]
    for tool in forbidden_tools:
        if f"- {tool}" in front_matter:
            raise AssertionError(f"fixer must not receive {tool}")

    require(coordinator, "Fixer requires a literal contract", "coordinator fixer guidance")
    require(coordinator, "swarm_fixer", "coordinator fixer routing")
    require(readme, "`swarm_fixer`", "worker catalog listing")
    require(readme, "Fixer delegation contract", "worker catalog contract")

    print("strict fixer blueprint contract passed")


if __name__ == "__main__":
    main()
