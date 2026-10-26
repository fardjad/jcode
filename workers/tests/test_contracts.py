"""Generic static invariants for the currently discovered blueprint files."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

BLUEPRINTS = Path(__file__).resolve().parents[1] / "blueprints"
FORBIDDEN_CATALOG_PROCEDURES = (
    ".patched-jcode",
    "sync-scratch/",
    "VERSION.txt",
    "tools/scripts/setup_assets.py",
    "just snapshot-patches",
    "X-Jcode-Patch-",
)


def parse_blueprint(path: Path) -> tuple[str, str]:
    if path.is_symlink() or not path.is_file():
        raise AssertionError(f"blueprint must be a regular non-symlink file: {path}")
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or text.count("---") < 2:
        raise AssertionError(f"blueprint has invalid front-matter fences: {path}")
    _, front_matter, body = text.split("---", 2)
    if not front_matter.strip() or not body.strip():
        raise AssertionError(f"blueprint front matter and body must be nonempty: {path}")
    return front_matter, body


def validate_blueprint_directory(directory: Path) -> None:
    paths = sorted(directory.glob("*.md"))
    if not paths or not (directory / "coordinator.md").is_file():
        raise AssertionError("blueprint directory requires coordinator.md and worker docs")
    if len(paths) < 2:
        raise AssertionError("at least one dynamically named worker blueprint is required")
    for path in paths:
        front_matter, body = parse_blueprint(path)
        for marker in FORBIDDEN_CATALOG_PROCEDURES:
            if marker in body:
                raise AssertionError(f"catalog procedure {marker!r} leaked into {path}")
        if path.stem == "coordinator":
            if "enabled-tools: []" not in front_matter:
                raise AssertionError("coordinator must not declare an enabled tool allowlist")
            if "disabled-tools:" not in front_matter:
                raise AssertionError("coordinator must declare disabled-tools")
            for concept in ("delegat", "acceptance", "worker"):
                if concept not in body.lower():
                    raise AssertionError(f"coordinator contract lacks {concept}: {path}")
        else:
            if "communication-policy: report-to-parent" not in front_matter:
                raise AssertionError(f"worker must report to its parent: {path}")
            if "enabled-tools:" not in front_matter:
                raise AssertionError(f"worker tool policy must be explicit: {path}")
            for concept in ("PARTIAL", "ESCALATION", "evidence", "blocker"):
                if concept not in body:
                    raise AssertionError(f"worker handoff contract lacks {concept}: {path}")
            if len(body.strip()) < 200:
                raise AssertionError(f"worker contract is too short to be useful: {path}")


class WorkerContractTests(unittest.TestCase):
    def test_current_blueprints_satisfy_generic_invariants(self) -> None:
        validate_blueprint_directory(BLUEPRINTS)

    def test_arbitrary_worker_ids_can_be_added_removed_and_renamed(self) -> None:
        coordinator = (BLUEPRINTS / "coordinator.md").read_text(encoding="utf-8")
        worker = (BLUEPRINTS / "fixer.md").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "coordinator.md").write_text(coordinator, encoding="utf-8")
            (root / "arbitrary-a.md").write_text(worker, encoding="utf-8")
            (root / "other-worker.md").write_text(worker, encoding="utf-8")
            validate_blueprint_directory(root)
            (root / "arbitrary-a.md").unlink()
            (root / "other-worker.md").rename(root / "renamed-worker.md")
            validate_blueprint_directory(root)


if __name__ == "__main__":
    unittest.main()
