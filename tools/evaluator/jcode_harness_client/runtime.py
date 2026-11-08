"""Disposable daemon/bridge lifecycle for integration and behavioral evaluations.

No provider discovery, personal configuration, login, or TCP relay. Callers
supply config text and explicit provider environment values. Raw logs and durable
sessions remain available until the context exits, then the whole home is removed.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from .blind_runtime import (
    BWRAP_EXECUTABLE,
    StagingPlan,
    build_blind_command,
    find_child_pid,
    find_group_member,
    public_dirs_env,
    stage_files,
    system_python,
)
from .client import HarnessClient
from .hook_broker import (
    PROXY_ENDPOINT_ENV,
    BrokerOperation,
    HostHookBroker,
)


@dataclass(frozen=True)
class HarnessRuntime:
    client: HarnessClient
    home: Path
    workspace: Path
    root: Path


@dataclass(frozen=True)
class BlindHarnessRuntime:
    """A live blind sandbox harness: client plus its public staging facts.

    ``root`` is the public staging root (host and sandbox see identical
    absolute paths); ``home`` is the sandbox HOME/JCODE tree inside it;
    ``workspace`` is the staged public workspace copy the agent edits;
    ``native_pid`` is the trusted native daemon's host PID registered with
    the host hook broker. The staging root outlives the context so the
    caller can run acceptance and retention on the staged tree, then remove
    it.
    """

    client: HarnessClient
    home: Path
    workspace: Path
    root: Path
    staging: StagingPlan
    broker: HostHookBroker | None
    native_pid: int


def blind_preflight(jcode: Path | None = None, bridge: Path | None = None) -> str:
    """Fail closed unless the blind boundary can actually be established.

    Returns the bubblewrap executable path. Missing bwrap, no plain system
    Python, or (when given) missing native binaries abort the run: there is
    never a fallback to a pretend-blind or host-visible launch.
    """
    if os.name != "posix":
        raise RuntimeError("blind runtime requires POSIX")
    bwrap = shutil.which(BWRAP_EXECUTABLE)
    if bwrap is None:
        raise RuntimeError(
            "bwrap is required for the blind runtime: refusing to launch"
        )
    system_python()
    for role, path in (("jcode binary", jcode), ("bridge", bridge)):
        if path is not None and not Path(path).is_file():
            raise RuntimeError(f"blind runtime requires the {role}: {path}")
    return bwrap


# Staged generic supervisor: starts the native daemon and bridge inside the
# sandbox and stays alive as their parent so host /proc PPid authentication
# keeps a reliable mapping for the whole run. Stdlib only; it runs inside the
# sandbox where only system Python is available.
_SUPERVISOR_SOURCE = '''#!/usr/bin/env python3
"""Generic blind sandbox supervisor: daemon, bridge, then wait."""

import socket
import subprocess
import sys
import time


def wait_socket(path, deadline):
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.connect(path)
            return
        except OSError:
            time.sleep(0.05)
    raise SystemExit("socket startup exceeded the deadline: " + path)


def main():
    argv = sys.argv[1:]
    if len(argv) < 6 or argv[4] != "--":
        raise SystemExit("usage: supervisor SOCKETS LOGS -- DAEMON -- BRIDGE")
    daemon_socket, api_socket, daemon_log, bridge_log = argv[0:4]
    rest = argv[5:]
    bridge_index = rest.index("--")
    daemon_argv, bridge_argv = rest[:bridge_index], rest[bridge_index + 1:]
    daemon_log_file = open(daemon_log, "ab", 0)
    bridge_log_file = open(bridge_log, "ab", 0)
    daemon = subprocess.Popen(
        daemon_argv, stdout=daemon_log_file, stderr=daemon_log_file
    )
    bridge = None
    try:
        wait_socket(daemon_socket, time.monotonic() + 60)
        bridge = subprocess.Popen(
            bridge_argv, stdout=bridge_log_file, stderr=bridge_log_file
        )
        wait_socket(api_socket, time.monotonic() + 60)
        daemon_rc = daemon.wait()
        bridge_rc = bridge.wait()
        raise SystemExit(daemon_rc or bridge_rc or 0)
    finally:
        if bridge is not None and bridge.poll() is None:
            bridge.kill()
            bridge.wait()
        if daemon.poll() is None:
            daemon.kill()
            daemon.wait()


if __name__ == "__main__":
    main()
'''


@contextmanager
def launch_blind_harness(
    *,
    staging: StagingPlan,
    config: str,
    profile: str,
    model: str,
    workspace: Path,
    environment: dict[str, str] | None = None,
    broker_operations: Mapping[str, BrokerOperation]
    | Iterable[BrokerOperation]
    | None = None,
    broker_log: Path | str | None = None,
    turn_timeout: float = 90,
    shutdown_gaps: list[str] | None = None,
    created: Callable[[Path], None] | None = None,
    stopped: Callable[[], None] | None = None,
    ro_cache_paths: Sequence[Path | str] = (),
    daemon_prefix: Sequence[str] = (),
    bridge_prefix: Sequence[str] = (),
) -> Iterator[BlindHarnessRuntime]:
    """Launch the native daemon and bridge INSIDE a blind bwrap sandbox.

    The boundary is fail-closed: preflight requires bwrap, a plain system
    Python, and the staged native binaries; a staging base that is not a
    public path, an unauthenticated native child, or a missing socket aborts
    the launch rather than degrading to a host-visible run. The caller stages
    the plan (binaries, public proxy) and owns removing the staging root
    after acceptance/retention; nothing private is ever mounted or exposed.
    ``daemon_prefix``/``bridge_prefix`` allow launching a staged native via an
    explicit interpreter (for scripted test doubles); the exact full argv is
    what gets discovered and registered, so PID authentication stays exact.
    """
    bwrap_executable = blind_preflight()
    stage_files(staging)
    bwrap_executable = blind_preflight(staging.bin / "jcode", staging.bin / "bridge")
    python = system_python()
    home = staging.home
    jcode_home = staging.runtime / "jcode"
    for name in ("config", "cache", "data", "state"):
        (staging.runtime / name).mkdir(parents=True, exist_ok=True)
    jcode_home.mkdir(parents=True, exist_ok=True)
    config_path = jcode_home / "config.toml"
    config_path.touch(mode=0o600)
    config_path.write_text(config, encoding="utf-8")
    xdg_config = staging.runtime / "config" / "jcode"
    xdg_config.mkdir(parents=True, exist_ok=True)
    xdg_mirror = xdg_config / "config.toml"
    xdg_mirror.touch(mode=0o600)
    xdg_mirror.write_text(config, encoding="utf-8")
    staged_workspace = staging.workspace
    staged_workspace.mkdir(parents=True, exist_ok=True)
    shutil.copytree(workspace, staged_workspace, dirs_exist_ok=True)
    supervisor = staging.bin / "blind-supervisor"
    if supervisor.exists():
        raise ValueError(f"supervisor destination already exists: {supervisor}")
    supervisor.write_text(_SUPERVISOR_SOURCE, encoding="utf-8")
    supervisor.chmod(0o755)
    daemon_socket = staging.runtime / "daemon.sock"
    api_socket = staging.runtime / "api.sock"
    endpoint = staging.runtime / "hook-broker.sock"
    explicit = dict(environment or {})
    if "CARGO_TARGET_DIR" in explicit and not str(
        explicit["CARGO_TARGET_DIR"]
    ).startswith(f"{staging.root}/"):
        raise ValueError("CARGO_TARGET_DIR must stay inside the public staging root")
    explicit["CARGO_TARGET_DIR"] = str(staging.runtime / "cargo-target")
    (staging.runtime / "cargo-target").mkdir(parents=True, exist_ok=True)
    public_dirs = public_dirs_env(
        staging,
        {
            "HOME": str(home),
            "JCODE_HOME": str(jcode_home),
            "JCODE_RUNTIME_DIR": str(staging.runtime),
            "XDG_CONFIG_HOME": str(staging.runtime / "config"),
            "XDG_CACHE_HOME": str(staging.runtime / "cache"),
            "XDG_DATA_HOME": str(staging.runtime / "data"),
            "XDG_STATE_HOME": str(staging.runtime / "state"),
        },
    )
    broker: HostHookBroker | None = None
    if broker_operations is not None:
        # Payload files share the public runtime mount, but its authoritative
        # root is host-configured, never accepted from a proxy request.
        def with_payload_root(operation: BrokerOperation) -> BrokerOperation:
            return replace(
                operation,
                env={**operation.env, "JCODE_RUNTIME_DIR": public_dirs["JCODE_RUNTIME_DIR"]},
            )

        operations = (
            {name: with_payload_root(op) for name, op in broker_operations.items()}
            if isinstance(broker_operations, Mapping)
            else [with_payload_root(op) for op in broker_operations]
        )
        explicit[PROXY_ENDPOINT_ENV] = str(endpoint)
        broker = HostHookBroker(endpoint, operations, log_path=broker_log)
    command = build_blind_command(
        staging,
        ro_cache_paths=ro_cache_paths,
        host_env=os.environ,
        extra_env=explicit,
        public_dirs=public_dirs,
        bwrap_executable=bwrap_executable,
    )
    daemon_argv = [
        *daemon_prefix,
        str(staging.bin / "jcode"),
        "--no-update",
        *(["--provider-profile", profile] if profile else []),
        "--model",
        model,
        "--socket",
        str(daemon_socket),
        "serve",
    ]
    bridge_argv = [
        *bridge_prefix,
        str(staging.bin / "bridge"),
        str(api_socket),
        str(daemon_socket),
    ]
    supervisor_argv = [
        python,
        str(supervisor),
        str(daemon_socket),
        str(api_socket),
        str(jcode_home / "daemon.log"),
        str(jcode_home / "bridge.log"),
        "--",
        *daemon_argv,
        "--",
        *bridge_argv,
    ]
    if created is not None:
        created(staging.root)
    if broker is not None:
        broker.start()
    process = subprocess.Popen(
        command.command(supervisor_argv),
        env=command.env,
        start_new_session=True,
    )
    native_pid: int | None = None
    yielded = False
    try:
        supervisor_pid = find_group_member(process.pid, supervisor_argv)
        native_pid = find_child_pid(supervisor_pid, daemon_argv)
        if broker is not None:
            broker.register_native_pid(native_pid, daemon_argv)
        _wait_socket(api_socket, process)
        with HarnessClient.unix(api_socket, turn_timeout=turn_timeout) as client:
            yielded = True
            yield BlindHarnessRuntime(
                client=client,
                home=jcode_home,
                workspace=staged_workspace,
                root=staging.root,
                staging=staging,
                broker=broker,
                native_pid=native_pid,
            )
    finally:
        forced = _stop(process)
        if broker is not None:
            if native_pid is not None:
                broker.unregister_native_pid(native_pid)
            broker.stop()
        if forced and shutdown_gaps is not None:
            shutdown_gaps.append(
                "a native process exceeded the six-second graceful shutdown window"
            )
        if (yielded or process) and stopped is not None:
            stopped()


def runtime_environment(root: Path, explicit: dict[str, str]) -> dict[str, str]:
    """Start from a minimal environment, never inherited provider/config routing."""
    environment = {
        key: os.environ[key]
        for key in ("SYSTEMROOT", "LANG")
        if key in os.environ
    }
    environment["PATH"] = "/usr/bin:/bin"
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
