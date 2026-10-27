"""Provide disposable HOME/XDG/runtime directories for repository subprocesses."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# macOS limits sockaddr_un.sun_path to 104 bytes and the other supported Unix
# systems are no more generous. jcode binds both a shared socket and a debug
# socket under its runtime dir, so every derived socket path must fit.
_MAX_SOCKET_PATH_BYTES = 104

# jcode derives its socket names inside the runtime dir: the shared socket is
# "jcode.sock" and the debug socket replaces ".sock" with "-debug.sock".
_RUNTIME_SOCKET_NAMES = ("jcode.sock", "jcode-debug.sock")


def _runtime_socket_paths(runtime: Path) -> tuple[Path, ...]:
    """Return the socket paths jcode derives inside ``runtime``."""
    return tuple(runtime / name for name in _RUNTIME_SOCKET_NAMES)


def _runtime_socket_paths_fit(runtime: Path) -> bool:
    """True when every derived socket path fits sockaddr_un.sun_path."""
    return all(
        len(os.fsencode(str(path))) <= _MAX_SOCKET_PATH_BYTES
        for path in _runtime_socket_paths(runtime)
    )


def _make_private_directory(path: Path) -> None:
    """Create ``path`` with owner-only permissions despite the caller umask."""
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)


def _short_temp_root() -> Path | None:
    """Return a short parent for the disposable isolation root, or None.

    The isolated directories are tiny, so their single disposable root lives
    directly under the platform's short generic temp location instead of the
    inherited TMPDIR. That keeps derived AF_UNIX socket paths short no matter
    how long the caller's temp base is and keeps nested isolated
    environments from compounding path lengths. /private/tmp is the canonical
    macOS spelling of /tmp: both spellings are short, but the canonical one
    keeps socket paths free of a symlinked parent. Windows, and any system
    without a usable generic location, falls back to the platform's default
    tempfile root.
    """
    if sys.platform == "win32":
        return None
    generic = Path("/private/tmp" if sys.platform == "darwin" else "/tmp")
    return generic if generic.is_dir() else None


@contextmanager
def _private_runtime_dir(root: Path) -> Iterator[Path]:
    """Yield a disposable runtime dir whose derived socket paths fit AF_UNIX.

    The runtime dir lives under the disposable ``root`` whenever jcode's
    normal and debug socket paths fit the sockaddr_un.sun_path limit. When
    the chosen root is still too long (a platform whose generic temp root is
    long or missing), the runtime dir moves to the platform's short generic
    temp location instead of inheriting the caller's temp path as the socket
    container.
    """
    candidate = root / "runtime"
    if _runtime_socket_paths_fit(candidate):
        _make_private_directory(candidate)
        yield candidate
        return
    # /private/tmp is the canonical macOS spelling of /tmp: both spellings
    # are short, but the canonical one keeps the socket path free of a
    # symlinked parent.
    generic = Path("/private/tmp" if sys.platform == "darwin" else "/tmp")
    if not generic.is_dir():
        _make_private_directory(candidate)
        yield candidate
        return
    with tempfile.TemporaryDirectory(prefix="jc-", dir=generic) as short_root:
        runtime = Path(short_root) / "runtime"
        _make_private_directory(runtime)
        yield runtime


@contextmanager
def isolated_environment() -> Iterator[dict[str, str]]:
    """Yield an environment that cannot discover the invoking user's config."""
    # One short disposable root holds every isolated directory, independent
    # of the inherited TMPDIR: never reuse the caller's temp paths, and keep
    # every derived socket path short even when isolated environments nest.
    with tempfile.TemporaryDirectory(prefix="jc-", dir=_short_temp_root()) as directory:
        root = Path(directory)
        # mkdtemp already creates the root owner-only; chmod again so the
        # root stays 0700 regardless of platform behavior.
        os.chmod(root, 0o700)
        inherited = os.environ.copy()
        home = root / "home"
        config = root / "xdg-config"
        cache = root / "xdg-cache"
        data = root / "xdg-data"
        state = root / "xdg-state"
        temp = root / "tmp"
        for path in (home, config, cache, data, state, temp):
            path.mkdir()
        temp.chmod(0o700)

        with _private_runtime_dir(root) as runtime:
            environment = inherited.copy()
            environment.update({
                "HOME": str(home),
                "JCODE_HOME": str(home / ".jcode"),
                "XDG_CONFIG_HOME": str(config),
                "XDG_CACHE_HOME": str(cache),
                "XDG_DATA_HOME": str(data),
                "XDG_STATE_HOME": str(state),
                "XDG_RUNTIME_DIR": str(runtime),
                "JCODE_RUNTIME_DIR": str(runtime),
                "JCODE_SOCKET": str(runtime / "jcode.sock"),
                "TMPDIR": str(temp),
                "TMP": str(temp),
                "TEMP": str(temp),
                "JCODE_WORKFLOW_CONFIG_ISOLATED": "1",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_AUTHOR_NAME": "jcode validation",
                "GIT_AUTHOR_EMAIL": "jcode-validation@example.invalid",
                "GIT_COMMITTER_NAME": "jcode validation",
                "GIT_COMMITTER_EMAIL": "jcode-validation@example.invalid",
            })
            # Inherited runtime and socket control must never reach the
            # isolated command: the private runtime replaces the caller's
            # paths, and debug control stays unset so no live server is
            # contacted through it.
            environment.pop("JCODE_DEBUG_CONTROL", None)

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
