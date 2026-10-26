"""One developer command interface. Workflows own their safety policies."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
from typing import Annotated

import typer

from .isolate_config import isolated_environment
from .patch_catalog import CatalogError, git, repository_root
from .version_base import VersionError

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Develop and validate the jcode patch catalog.")
_install_environment: dict[str, str] | None = None
patches_app = typer.Typer(no_args_is_help=True)
workers_app = typer.Typer(no_args_is_help=True)
version_app = typer.Typer(no_args_is_help=True)
app.add_typer(patches_app, name="patches")
app.add_typer(workers_app, name="workers")
app.add_typer(version_app, name="version")


@contextmanager
def workflow_environment():
    """Enforce isolation even when the CLI is called without just or a wrapper."""
    original = os.environ.copy()
    with isolated_environment() as environment:
        os.environ.clear()
        os.environ.update(environment)
        try:
            yield
        finally:
            os.environ.clear()
            os.environ.update(original)


def workflow(operation: str, patch: str | None = None):
    from .patch_workflow import execute
    execute(repository_root(), operation, patch)


@patches_app.command("check")
def check():
    from .validate_patches import validate_catalog
    validate_catalog(repository_root())


@patches_app.command("isolation")
def isolation():
    from .check_patch_isolation import check
    from .version_base import resolve_base
    root = repository_root()
    check(root, resolve_base(root))


@patches_app.command("create")
def create():
    workflow("create")


@patches_app.command("validate")
def validate():
    workflow("validate")


@patches_app.command("snapshot")
def snapshot(prune: bool = False):
    from .snapshot_patches import snapshot
    snapshot(repository_root(), prune)


@patches_app.command("list")
def list_patches():
    from .snapshot_patches import list_mapping
    list_mapping(repository_root())


@patches_app.command("test")
def test_patch(
    patch: Annotated[str | None, typer.Argument()] = None,
    all_patches: Annotated[bool, typer.Option("--all")] = False,
    only: Annotated[list[str] | None, typer.Option("--only")] = None,
    force: bool = False,
    audit: bool = False,
):
    if all_patches or audit or only:
        if patch:
            raise typer.BadParameter("use a patch argument or --all/--only, not both")
        from .run_all_patch_tests import run_all
        raise typer.Exit(run_all(repository_root(), only, force, audit))
    if not patch:
        raise typer.BadParameter("provide PATCH or --all")
    workflow("test", patch)


@patches_app.command("candidate")
def candidate(patch: str):
    workflow("candidate", patch)


@patches_app.command("learn")
def learn():
    workflow("learn")


@patches_app.command("reviews")
def reviews(action: str, directory: Path = Path("sync-scratch/reviews")):
    from .check_patch_reviews import main
    with arguments(["reviews", action, "--directory", str(directory)]):
        main()


@contextmanager
def arguments(values):
    original = sys.argv
    sys.argv = values
    try:
        yield
    finally:
        sys.argv = original


@workers_app.command("evaluate")
def evaluate(scenario: Annotated[str | None, typer.Argument()] = None):
    """Run paid evaluation only when this command is explicitly invoked."""
    from .eval_workers import main
    with arguments(["evaluate"] + (["--scenario", scenario] if scenario else [])):
        raise typer.Exit(main())


@workers_app.command("build")
def build():
    from .prepare_worker_eval_build import main
    with arguments(["build"]):
        raise typer.Exit(main())


@version_app.command("set")
def set_version(release: str, expected: Annotated[str | None, typer.Argument()] = None):
    from .version_base import require_stable_release, resolve_release, write_version
    root = repository_root()
    if git("branch", "--show-current", cwd=root) != "personalized":
        raise CatalogError("set-version must run on personalized")
    if git("status", "--porcelain", cwd=root):
        raise CatalogError("set-version requires clean personalized checkout")
    require_stable_release(release)
    selected = resolve_release(root, release, fetch=True)
    if expected:
        import re
        if not re.fullmatch(r"[0-9a-f]{7,40}", expected) or not selected.startswith(expected):
            raise CatalogError(f"release commit {selected} does not match reviewed hash {expected}")
    write_version(root, release)
    print(f"VERSION.txt now pins {release}; revert with: git checkout -- VERSION.txt")


@app.command("push")
def push():
    root = repository_root()
    if git("branch", "--show-current", cwd=root) != "personalized":
        raise CatalogError("push must run on personalized")
    refs = ["refs/heads/personalized:refs/heads/personalized"]
    refs += [f"{ref}:{ref}" for ref in git("for-each-ref", "--format=%(refname)",
                                         "refs/heads/upstream-candidate/", cwd=root).splitlines()]
    subprocess.run(["git", "push", "origin", *refs], cwd=root, check=True)


@app.command("install")
def install():
    """Build and install only when explicitly invoked."""
    root = repository_root()
    environment = (_install_environment or os.environ).copy()
    destination = environment.get("JCODE_INSTALL_DIR", str(Path(environment["HOME"]) / ".local/bin"))
    workflow("create")
    environment["JCODE_INSTALL_DIR"] = destination
    if sys.platform == "darwin":
        environment["CARGO_PROFILE_RELEASE_STRIP"] = "none"
    subprocess.run(["bash", "scripts/install_release.sh", "--fast"],
                   cwd=root / ".patched-jcode", env=environment, check=True)


@app.command("test")
def test_catalog():
    """Run unpaid catalog, plugin, worker, and evaluator tests."""
    root = repository_root()
    subprocess.run([sys.executable, "-m", "pytest", "-q"], cwd=root / "tools/catalog", check=True)
    for script in sorted((root / "tools/catalog/src/jcode_catalog").glob("*_test.sh")):
        if script.name == "install_patched_version_recipe_test.sh":
            print(f"skip {script.name} (full release build)")
            continue
        subprocess.run(["bash", str(script)], cwd=root, check=True)
    pytest = root / "tools/evaluator/.venv/bin/pytest"
    if not pytest.is_file():
        raise CatalogError("evaluation tests need: cd tools/evaluator && uv sync --extra eval")
    subprocess.run([str(pytest), "-q"], cwd=root / "tools/evaluator", check=True)


def main():
    # Typer owns dispatch, but the CLI boundary owns isolation and errors.
    global _install_environment
    _install_environment = os.environ.copy()
    try:
        with workflow_environment():
            app()
    except (CatalogError, VersionError, OSError, subprocess.CalledProcessError) as error:
        typer.echo(f"ERROR: {error}", err=True)
        raise SystemExit(1) from error
