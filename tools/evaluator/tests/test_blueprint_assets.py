"""Structural metadata tests for the shipped worker blueprint assets.

These check only structural front-matter metadata, parsed with a real YAML
parser: tool denies, child-blueprint allowlists, and catalog-path hygiene.
Behavioral wording is owned by the blueprint prompts themselves, arbitrary
worker discovery, add/remove, and rename are covered natively by the
evaluator inventory tests, and no minimum body length or prose phrase is
pinned here.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
BLUEPRINTS = REPO_ROOT / "customizations" / "workers" / "blueprints"

# Runtime blueprints stay portable: catalog procedures and paths never appear
# inside them.
CATALOG_PROCEDURE_MARKERS = (
    ".patched-jcode",
    "sync-scratch/",
    "VERSION.txt",
    "tools/scripts/setup_assets.py",
    "just snapshot-patches",
    "X-Jcode-Patch-",
)

# The reserved coordinator grants only a purpose-based allowlist for planning
# and coordination (plan-file read/write, todo, skill access, and every
# generated tier spawn tool via the swarm_* pattern), plus an explicit deny
# for the generic swarm tool as defense in depth since it would not match
# that allowlist anyway.
COORDINATOR_ENABLED_TOOLS = (
    "read",
    "write",
    "todo",
    "skill_manage",
    "swarm_*",
)
COORDINATOR_DENIED_TOOLS = ("swarm",)
# A newly added execution or research tool must stay outside the allowlist
# unless the catalog explicitly widens it; this list guards the specific
# tools the coordinator previously had to deny by name one by one.
COORDINATOR_EXCLUDED_EXECUTION_AND_RESEARCH_TOOLS = (
    "agentgrep",
    "apply_patch",
    "bash",
    "batch",
    "bg",
    "browser",
    "calendar",
    "conversation_search",
    "edit",
    "gmail",
    "jcode_docs",
    "mcp",
    "memory",
    "open",
    "schedule",
    "session_search",
    "webfetch",
    "websearch",
)


def _blueprint(name: str) -> tuple[dict, str]:
    path = BLUEPRINTS / f"{name}.md"
    assert not path.is_symlink() and path.is_file(), path
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n") and text.count("---") >= 2, path
    _, front_matter, body = text.split("---", 2)
    metadata = yaml.safe_load(front_matter)
    assert isinstance(metadata, dict), path
    assert body.strip(), path
    return metadata, body


def test_shipped_blueprints_parse_with_a_real_front_matter_parser() -> None:
    for name in ("coordinator", "routine", "advanced", "expert"):
        metadata, body = _blueprint(name)
        if name != "coordinator":
            assert metadata["name"] == name
        assert body, name


def test_coordinator_grants_purpose_based_allowlist_and_denies_generic_swarm() -> None:
    metadata, _ = _blueprint("coordinator")
    assert metadata["enabled-tools"] == list(COORDINATOR_ENABLED_TOOLS)
    assert metadata["disabled-tools"] == list(COORDINATOR_DENIED_TOOLS)
    enabled = set(metadata["enabled-tools"])
    assert "swarm" not in enabled
    for tool in COORDINATOR_EXCLUDED_EXECUTION_AND_RESEARCH_TOOLS:
        assert tool not in enabled, tool


def test_tier_workers_deny_spawn_tools_and_pin_child_allowlists() -> None:
    expected = {
        "routine": (("swarm", "swarm_routine", "swarm_advanced", "swarm_expert"), []),
        "advanced": (("swarm", "swarm_advanced", "swarm_expert"), ["routine"]),
        "expert": (("swarm", "swarm_advanced", "swarm_expert"), ["routine"]),
    }
    for name, (denied, children) in expected.items():
        metadata, _ = _blueprint(name)
        disabled = set(metadata["disabled-tools"])
        for tool in denied:
            assert tool in disabled, (name, tool)
        assert metadata["allowed-child-blueprints"] == children, name
        assert metadata["communication-policy"] == "report-to-parent", name
    # Advanced and Expert expose the Routine spawn tool the allowlist admits.
    for name in ("advanced", "expert"):
        metadata, _ = _blueprint(name)
        assert "swarm_routine" not in metadata["disabled-tools"], name


def test_blueprints_do_not_leak_catalog_procedure_paths() -> None:
    paths = sorted(BLUEPRINTS.glob("*.md"))
    assert paths
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for marker in CATALOG_PROCEDURE_MARKERS:
            assert marker not in text, (path.name, marker)
