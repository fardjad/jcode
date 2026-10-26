"""Disposable daemon/bridge lifecycle for integration and behavioral evaluations.

No provider discovery, personal configuration, login, or TCP relay. Callers
supply config text and explicit provider environment values. Raw logs and durable
sessions remain available until the context exits, then the whole home is removed.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .client import HarnessClient


@dataclass(frozen=True)
class HarnessRuntime:
    client: HarnessClient
    home: Path
    workspace: Path
    root: Path


def _expand_toolchain_path(raw_path: str) -> str:
    """Resolve mise shims to direct tool bin directories so isolated XDG works."""
    entries: list[str] = []
    for entry in raw_path.split(os.pathsep):
        if "mise" in entry and entry.endswith("shims"):
            installs = Path(entry).parent / "installs"
            if installs.is_dir():
                for candidate in sorted(installs.glob("*/latest/bin")) + sorted(
                    installs.glob("*/latest")
                ):
                    if candidate.is_dir():
                        entries.append(str(candidate))
        entries.append(entry)
    return os.pathsep.join(entries)


def runtime_environment(root: Path, explicit: dict[str, str]) -> dict[str, str]:
    """Start from a minimal environment, never inherited provider/config routing."""
    environment = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "LANG", "CARGO_HOME", "RUSTUP_HOME")
        if key in os.environ
    }
    if "PATH" in environment:
        environment["PATH"] = _expand_toolchain_path(environment["PATH"])
    environment.update(explicit)
    environment.update(
        {
            "HOME": str(root / "home"),
            "JCODE_HOME": str(root / "jcode"),
            "JCODE_RUNTIME_DIR": str(root / "runtime"),
            "XDG_RUNTIME_DIR": str(root / "runtime"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_STATE_HOME": str(root / "state"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
    )
    return environment


def _wait_socket(path: Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("harness subprocess exited before its socket was ready")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.connect(str(path))
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError("harness socket startup exceeded 20 seconds")


def _stop(process: subprocess.Popen[bytes]) -> bool:
    """Allow native save draining, then clean the whole process group."""

    forced = False
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=6 if sig == signal.SIGTERM else 3)
        except subprocess.TimeoutExpired:
            if sig == signal.SIGTERM:
                forced = True
            continue
        # A final group kill also cleans descendants whose parent already exited.
    return forced


@contextmanager
def launch_harness(
    *,
    jcode: Path,
    bridge: Path,
    config: str,
    profile: str,
    model: str,
    workspace: Path,
    environment: dict[str, str] | None = None,
    turn_timeout: float = 90,
    shutdown_gaps: list[str] | None = None,
    created: Callable[[Path], None] | None = None,
    stopped: Callable[[], None] | None = None,
) -> Iterator[HarnessRuntime]:
    """Launch isolated native processes and always terminate their process groups.

    POSIX Unix sockets are required. No configuration path or provider is inferred.
    The caller owns the fixture workspace and retained, sanitized evidence.
    """
    if os.name != "posix":
        raise RuntimeError("native harness lifecycle currently requires POSIX")
    jcode = jcode.resolve(strict=True)
    bridge = bridge.resolve(strict=True)
    workspace = workspace.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="jcode-eval-") as directory:
        root = Path(directory)
        env = runtime_environment(root, environment or {})
        home = Path(env["JCODE_HOME"])
        for key in (
            "HOME",
            "JCODE_HOME",
            "JCODE_RUNTIME_DIR",
            "XDG_CONFIG_HOME",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
        ):
            Path(env[key]).mkdir(parents=True, exist_ok=True)
        config_path = home / "config.toml"
        config_path.touch(mode=0o600)
        config_path.write_text(config, encoding="utf-8")
        # Mirror the explicit config for binaries using XDG discovery.
        xdg_config = Path(env["XDG_CONFIG_HOME"]) / "jcode"
        xdg_config.mkdir()
        xdg_config.joinpath("config.toml").touch(mode=0o600)
        xdg_config.joinpath("config.toml").write_text(config, encoding="utf-8")
        daemon_socket = root / "runtime" / "daemon.sock"
        api_socket = root / "runtime" / "api.sock"
        if created is not None:
            created(root)
        processes: list[subprocess.Popen[bytes]] = []
        logs = []
        yielded = False
        try:
            commands = [
                [
                    str(jcode),
                    "--no-update",
                    *(["--provider-profile", profile] if profile else []),
                    "--model",
                    model,
                    "--socket",
                    str(daemon_socket),
                    "serve",
                ],
                [str(bridge), str(api_socket), str(daemon_socket)],
            ]
            for name, command, path in zip(
                ("daemon", "bridge"), commands, (daemon_socket, api_socket), strict=True
            ):
                log_path = home / f"{name}.log"
                log_path.touch(mode=0o600)
                log = log_path.open("wb")
                logs.append(log)
                process = subprocess.Popen(
                    command,
                    cwd=workspace,
                    env=env,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
                processes.append(process)
                _wait_socket(path, process)
            with HarnessClient.unix(api_socket, turn_timeout=turn_timeout) as client:
                yielded = True
                yield HarnessRuntime(client, home, workspace, root)
        finally:
            forced = False
            for process in reversed(processes):
                forced = _stop(process) or forced
            for log in logs:
                log.close()
            if forced and shutdown_gaps is not None:
                shutdown_gaps.append(
                    "a native process exceeded the six-second graceful shutdown window"
                )
            if (yielded or logs) and stopped is not None:
                stopped()
