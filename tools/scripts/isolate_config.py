"""Provide disposable HOME/XDG directories for repository subprocesses."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def isolated_environment() -> Iterator[dict[str, str]]:
    """Yield an environment that cannot discover the invoking user's config."""
    with tempfile.TemporaryDirectory(prefix="jcode-isolated-config-") as directory:
        root = Path(directory)
        inherited = os.environ.copy()
        home = root / "home"
        config = root / "xdg-config"
        cache = root / "xdg-cache"
        data = root / "xdg-data"
        state = root / "xdg-state"
        for path in (home, config, cache, data, state):
            path.mkdir()

        environment = inherited.copy()
        environment.update({
            "HOME": str(home),
            "JCODE_HOME": str(home / ".jcode"),
            "XDG_CONFIG_HOME": str(config),
            "XDG_CACHE_HOME": str(cache),
            "XDG_DATA_HOME": str(data),
            "XDG_STATE_HOME": str(state),
            "JCODE_WORKFLOW_CONFIG_ISOLATED": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "jcode validation",
            "GIT_AUTHOR_EMAIL": "jcode-validation@example.invalid",
            "GIT_COMMITTER_NAME": "jcode validation",
            "GIT_COMMITTER_EMAIL": "jcode-validation@example.invalid",
        })

        # Cargo and rustup use HOME-relative defaults. Keep those toolchain
        # locations pointed at the invoking environment without carrying over
        # any jcode or XDG configuration paths. Explicit values take
        # precedence, matching cargo and rustup's normal environment lookup.
        inherited_home = inherited.get("HOME")
        for variable, default_directory in (
            ("CARGO_HOME", ".cargo"),
            ("RUSTUP_HOME", ".rustup"),
        ):
            if variable not in inherited and inherited_home:
                environment[variable] = str(Path(inherited_home) / default_directory)

        yield environment


def main() -> int:
    """Run one command with disposable HOME/XDG directories."""
    command = sys.argv[1:]
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        print(f"usage: {Path(sys.argv[0]).name} [--] COMMAND [ARGUMENT ...]", file=sys.stderr)
        return 2
    with isolated_environment() as environment:
        return subprocess.run(command, env=environment).returncode


if __name__ == "__main__":
    raise SystemExit(main())
