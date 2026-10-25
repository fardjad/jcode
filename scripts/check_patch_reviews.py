"""Create review templates and check evidence freshness, never semantic correctness."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from patch_catalog import CatalogError, fail, git, patches, repository_root
from snapshot_patches import format_patch, _zero_from_hash
from validation_state import closure_identity
from version_base import resolve_base


def source_commits(root):
    base = resolve_base(root)
    source = root / ".patched-jcode"
    result = {}
    for commit in git("rev-list", "--reverse", f"{base}..HEAD", cwd=source).splitlines():
        message = git("show", "-s", "--format=%B", commit, cwd=source)
        names = [line.split(":", 1)[1].strip() for line in message.splitlines()
                 if line.startswith("X-Jcode-Patch-File:")]
        if len(names) != 1 or names[0] in result:
            raise CatalogError(f"invalid source mapping for {commit}")
        result[names[0]] = commit
    catalog = patches(root)
    if set(result) != set(catalog):
        raise CatalogError("source commits and catalog patch names differ")
    for name, commit in result.items():
        generated = format_patch(source, commit, catalog[name].kind)
        persisted = catalog[name].path.read_text()
        # git am recreates candidate commit identities. Ignore only the mail
        # From hash, not message, diff, metadata, or named validation commands.
        if _zero_from_hash(generated) != _zero_from_hash(persisted):
            raise CatalogError(f"source commit and catalog patch content differ: {name}")
    return result


def evidence_errors(record, identity, commit):
    errors = []
    if record.get("identity") != identity or record.get("commit") != commit:
        errors.append("stale patch, dependency, base or source commit identity")
    if record.get("status") != "reviewed":
        errors.append("review incomplete")
    if not isinstance(record.get("reviewer"), str) or not record["reviewer"].strip():
        errors.append("reviewer missing")
    claims = record.get("claims")
    if not isinstance(claims, list) or not claims:
        errors.append("claim-to-code/test evidence missing")
    else:
        for claim in claims:
            if not isinstance(claim, dict) or any(not isinstance(claim.get(key), str)
                    or not claim[key].strip() for key in ("claim", "code", "tests", "finding")):
                errors.append("each claim needs code, tests and finding")
            elif claim.get("outcome") not in ("supported", "accepted-gap"):
                errors.append("unresolved claim outcome")
            elif claim["outcome"] == "accepted-gap" and not claim.get("user_acceptance"):
                errors.append("gap requires explicit user acceptance evidence")
    for key in ("scope_review", "broader_changes", "residuals"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            errors.append(f"{key} missing (use 'none' when checked and absent)")
    return errors


def reviews(root: Path, directory: Path, initialize=False):
    base = resolve_base(root)
    commits = source_commits(root)
    directory.mkdir(parents=True, exist_ok=True)
    if initialize and any((directory / f"{name}.json").exists() for name in commits):
        raise CatalogError("refusing to overwrite existing reviews, use a new directory")
    failures = []
    for name in sorted(commits):
        identity = closure_identity(root, name, base)
        path = directory / f"{name}.json"
        if initialize:
            if path.exists():
                raise CatalogError(f"refusing to overwrite review: {path}")
            path.write_text(json.dumps({"identity": identity, "commit": commits[name],
                "status": "pending", "reviewer": "", "claims": [], "scope_review": "",
                "broader_changes": "", "residuals": ""}, indent=2) + "\n")
            continue
        try:
            record = json.loads(path.read_text())
            if not isinstance(record, dict):
                raise ValueError("review must be an object")
            errors = evidence_errors(record, identity, commits[name])
        except (OSError, ValueError) as error:
            errors = [str(error)]
        if errors:
            failures.append(name)
            print(f"INCOMPLETE {name}: {'; '.join(errors)}")
        else:
            print(f"CURRENT {name}")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("init", "check"))
    parser.add_argument("--directory", type=Path, default=Path("sync-scratch/reviews"))
    args = parser.parse_args()
    try:
        root = repository_root()
        directory = args.directory.resolve()
        if not directory.is_relative_to((root / "sync-scratch").resolve()):
            raise CatalogError("review records must be under repository sync-scratch")
        raise SystemExit(reviews(root, directory, args.action == "init"))
    except CatalogError as error:
        fail(str(error))


if __name__ == "__main__":
    main()
