"""Run patch validation resumably and retain per-patch logs."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from patch_catalog import CatalogError, fail, patches, repository_root, resolve_patch
from version_base import resolve_base


def should_skip(results: dict[str, dict[str, str]], name: str, blob: str, force: bool) -> bool:
    """Return whether a previously successful unchanged patch can be skipped."""
    result = results.get(name, {})
    return not force and result.get("status") == "pass" and result.get("blob") == blob


def retained_worktree_path(output: str) -> str | None:
    """Extract the path emitted by test-patch-file's retained-worktree line."""
    for line in output.splitlines():
        marker = "worktree retained: "
        if marker in line:
            return line.split(marker, 1)[1].strip() or None
    return None


def patch_blob(root: Path, path: Path) -> str:
    """Return Git's object hash for a patch file."""
    result = subprocess.run(
        ["git", "hash-object", str(path)], cwd=root, check=True,
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def save_results(path: Path, results: dict[str, dict[str, str]]) -> None:
    """Atomically persist current per-patch results."""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
        json.dump(results, temporary, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary.flush()
    temporary_path.replace(path)


def run_all(root: Path, only: list[str] | None = None, force: bool = False) -> int:
    """Run selected patches in sorted order and record resumable outcomes."""
    base = resolve_base(root)
    catalog = patches(root)
    ordered = [catalog[name] for name in sorted(catalog)]
    if only:
        selected = {resolve_patch(root, name).name for name in only}
        ordered = [patch for patch in ordered if patch.path.name in selected]
    directory = root / ".tools" / "patch-tests" / base
    directory.mkdir(parents=True, exist_ok=True)
    results_path = directory / "results.json"
    if results_path.exists():
        try:
            results = json.loads(results_path.read_text(encoding="utf-8"))
            if not isinstance(results, dict):
                raise ValueError("results must be an object")
        except (OSError, json.JSONDecodeError, ValueError) as error:
            raise CatalogError(f"invalid patch test results {results_path}: {error}") from error
    else:
        results = {}

    failures: list[str] = []
    passed = 0
    total = len(ordered)
    for index, patch in enumerate(ordered, start=1):
        name = patch.path.name
        blob = patch_blob(root, patch.path)
        if should_skip(results, name, blob, force):
            status = "pass"
            passed += 1
        else:
            log_path = directory / f"{patch.path.stem}.log"
            try:
                result = subprocess.run(
                    ["just", "test-patch-file", f"patches/{name}"],
                    cwd=root, capture_output=True, text=True,
                )
                output = result.stdout + result.stderr
                log_path.write_text(output, encoding="utf-8")
                status = "pass" if result.returncode == 0 else "fail"
                if status == "pass":
                    worktree = retained_worktree_path(output)
                    if worktree is not None:
                        cleanup = subprocess.run(
                            ["git", "worktree", "remove", "--force", worktree],
                            cwd=root, capture_output=True, text=True,
                        )
                        if cleanup.returncode != 0:
                            status = "fail"
                            with log_path.open("a", encoding="utf-8") as log:
                                log.write(cleanup.stdout + cleanup.stderr)
                if status == "pass":
                    passed += 1
                else:
                    failures.append(name)
            except OSError as error:
                status = "fail"
                failures.append(name)
                log_path.write_text(f"{error}\n", encoding="utf-8")
            results[name] = {"blob": blob, "status": status}
            save_results(results_path, results)
        print(f'JCODE_PROGRESS {{"current":{index},"total":{total},"message":"{name} {status}"}}')
    failed_count = len(failures)
    print(f"patch tests: {passed} passed, {failed_count} failed, {total} total")
    if failures:
        print("failed patches: " + " ".join(failures), file=sys.stderr)
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--only", nargs="+")
    args = parser.parse_args()
    try:
        raise SystemExit(run_all(repository_root(), args.only, args.force))
    except CatalogError as error:
        fail(str(error))


if __name__ == "__main__":
    main()
