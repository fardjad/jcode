"""Tests for the bwrap blind-runtime command builder.

The bwrap tests launch a real bubblewrap subprocess with synthetic staging
and synthetic private fixtures only. No real user configuration, personal
path content, or provider session is ever read or launched.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from jcode_harness_client import blind_runtime
from jcode_harness_client.scenarios import (
    JudgeScenario,
    Scenario,
    SimulatedUserScenario,
    materialize_scenario,
)

PUBLIC_BASE = Path("/var/tmp")

SECRET_TEXT = "synthetic-private-secret"


def _public_base() -> Path:
    if PUBLIC_BASE.is_dir() and os.access(PUBLIC_BASE, os.W_OK):
        return PUBLIC_BASE
    return Path(tempfile.gettempdir())


@pytest.fixture()
def staging() -> blind_runtime.StagingPlan:
    root = Path(tempfile.mkdtemp(prefix="blind-root-", dir=_public_base()))
    plan = blind_runtime.plan_staging(
        root,
        jcode_binary=Path(blind_runtime.system_python()),
    )
    blind_runtime.stage_files(plan)
    try:
        yield plan
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture()
def private_sibling() -> Path:
    """A synthetic private host directory that is never staged or mounted."""
    sibling = Path(tempfile.mkdtemp(prefix="blind-private-", dir=_public_base()))
    (sibling / "secret.txt").write_text(SECRET_TEXT)
    try:
        yield sibling
    finally:
        shutil.rmtree(sibling, ignore_errors=True)


@pytest.fixture(scope="module")
def bwrap_ok() -> None:
    executable = shutil.which("bwrap")
    if executable is None:
        pytest.skip("bwrap is not installed")
    root = Path(tempfile.mkdtemp(prefix="blind-canary-", dir=_public_base()))
    try:
        plan = blind_runtime.plan_staging(root)
        command = blind_runtime.build_blind_command(plan)
        probe = subprocess.run(
            command.command([command.python, "-c", "print('canary-ok')"]),
            env=command.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if probe.returncode != 0 or "canary-ok" not in probe.stdout:
            pytest.skip(f"bwrap cannot build a sandbox here: {probe.stderr[:200]}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _launch(
    command: blind_runtime.BlindCommand, inner: list[str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command.command(inner),
        env=command.env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_public_dirs_env_channel_authorizes_only_staged_paths(
    staging: blind_runtime.StagingPlan,
) -> None:
    home = staging.runtime / "home"
    jcode_home = staging.runtime / "jcode"
    command = blind_runtime.build_blind_command(
        staging,
        extra_env={"JCODE_ACCEPTANCE_API_KEY": "synthetic"},
        public_dirs={
            "HOME": str(home),
            "JCODE_HOME": str(jcode_home),
            "JCODE_RUNTIME_DIR": str(staging.runtime),
        },
    )
    assert command.env["HOME"] == str(home)
    assert command.env["JCODE_HOME"] == str(jcode_home)
    assert command.env["JCODE_RUNTIME_DIR"] == str(staging.runtime)
    # The scrubbed keys stay rejected through the free-form channel.
    for key in ("HOME", "JCODE_HOME", "JCODE_RUNTIME_DIR", "TMPDIR"):
        with pytest.raises(ValueError, match="scrubbed"):
            blind_runtime.build_blind_env(staging, extra_env={key: "/private/leak"})
    # Unknown dir keys are not a global custom env channel.
    with pytest.raises(ValueError, match="not a public sandbox dir"):
        blind_runtime.build_blind_env(
            staging, public_dirs={"JCODE_CUSTOM_DIR": str(staging.root)}
        )
    # Values must resolve inside the public staging root.
    with pytest.raises(ValueError, match="inside the public staging root"):
        blind_runtime.build_blind_env(staging, public_dirs={"HOME": "/home/leak"})
    escape = staging.runtime / "escape"
    escape.symlink_to("/var/tmp")
    with pytest.raises(ValueError, match="inside the public staging root"):
        blind_runtime.build_blind_env(
            staging, public_dirs={"JCODE_RUNTIME_DIR": str(escape)}
        )


def test_argv_mounts_minimal_system_and_no_host_root(
    staging: blind_runtime.StagingPlan,
) -> None:
    command = blind_runtime.build_blind_command(staging, host_env=os.environ)
    argv = list(command.argv)

    assert argv[0] == "bwrap"
    # Empty sandbox root: nothing from the host filesystem is inherited.
    assert "--tmpfs" in argv and argv[argv.index("--tmpfs") + 1] == "/"
    ro_binds = blind_runtime._bind_pairs(argv, "--ro-bind")
    assert ("/usr", "/usr") in ro_binds
    assert ("/etc", "/etc") in ro_binds
    for source, destination in ro_binds:
        if source != destination:
            # Only the staged public DNS snapshot may map elsewhere, onto
            # the symlink target under the otherwise absent /run tree.
            assert source.startswith(f"{staging.root}/")
            assert destination == "/etc/resolv.conf" or destination.startswith("/run/")
        assert source not in ("/", "/home", "/run", "/root")
        assert destination not in ("/", "/home", "/root")
    assert ("--ro-bind", "/") not in list(zip(argv, argv[1:], strict=False))

    # Fresh PID namespace and devices; tmpfs /tmp; host networking kept.
    assert "--unshare-pid" in argv
    assert "--unshare-net" not in argv
    assert ("--proc", "/proc") in list(zip(argv, argv[1:], strict=False))
    assert ("--dev", "/dev") in list(zip(argv, argv[1:], strict=False))
    assert "--tmpfs /tmp" in " ".join(argv)

    # Merged-/usr shell directories exist as binds or symlinks.
    for name in ("bin", "lib", "lib64"):
        assert f"/{name}" in argv

    # Only the staged public directories are read-write, at identical paths.
    binds = blind_runtime._bind_pairs(argv, "--bind")
    assert {str(path) for path in staging.dirs} == {source for source, _ in binds}
    for source, destination in binds:
        assert source == destination
    assert ("--chdir", str(staging.workspace)) in list(
        zip(argv, argv[1:], strict=False)
    )


def test_env_is_rebuilt_and_scrubs_private_leakage(
    staging: blind_runtime.StagingPlan,
) -> None:
    dirty = {
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "SSH_AUTH_SOCK": "/run/user/1000/agent.sock",
        "JCODE_HOME": "/home/leak/.jcode",
        "CARGO_HOME": "/home/leak/.cargo",
        "PYTHONPATH": "/eval/venv/site-packages",
        "VIRTUAL_ENV": "/eval/venv",
        "PATH": "/eval/venv/bin:/home/leak/.local/bin:/usr/bin",
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
    }
    env = blind_runtime.build_blind_env(staging, host_env=dirty)

    assert env["PATH"] == f"{staging.bin}:/usr/bin:/bin"
    assert env["HOME"] == str(staging.home)
    assert env["TMPDIR"] == "/tmp"
    assert env["TERM"] == "xterm-256color"
    assert env["LANG"] == "C.UTF-8"
    for key in blind_runtime.SCRUBBED_ENV_KEYS:
        assert key not in env

    with pytest.raises(ValueError, match="scrubbed"):
        blind_runtime.build_blind_env(
            staging, extra_env={"DBUS_SESSION_BUS_ADDRESS": "unix:path=/leak"}
        )
    extended = blind_runtime.build_blind_env(
        staging, host_env=dirty, extra_env={"HTTPS_PROXY": "http://127.0.0.1:1"}
    )
    assert extended["HTTPS_PROXY"] == "http://127.0.0.1:1"


def test_public_path_validation_rejects_private_roots() -> None:
    for rejected in (
        "/home/leak/run",
        "/root/run",
        "/run/leak",
        "/tmp/leak",
        "/proc/leak",
        "/dev/leak",
        "/usr/local/run",
        "/etc/run",
        "/var",
        "/",
        "relative/path",
    ):
        with pytest.raises(ValueError):
            blind_runtime.plan_staging(rejected)
    plan = blind_runtime.plan_staging(
        Path(tempfile.mkdtemp(prefix="blind-validate-", dir=_public_base()))
    )
    try:
        with pytest.raises(ValueError):
            blind_runtime.build_blind_argv(plan, ro_cache_paths=["/home/leak/cache"])
        with pytest.raises(ValueError):
            blind_runtime.build_blind_argv(plan, ro_cache_paths=["/root/.cargo"])
    finally:
        shutil.rmtree(plan.root, ignore_errors=True)


def test_blind_fixture_is_readable_and_writable(
    bwrap_ok: None, staging: blind_runtime.StagingPlan, tmp_path: Path
) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "in.txt").write_text("hello-blind")
    scenario = Scenario(
        id="test",
        fixture=fixture,
        prompt="test",
        accept=("true",),
        judge=JudgeScenario(("test",)),
        simulated_user=SimulatedUserScenario(1, "test"),
        settings={},
        source=tmp_path / "config.toml",
        source_sha256="",
    )
    host_workspace = tmp_path / "workspace"
    materialize_scenario(scenario, host_workspace)
    shutil.copytree(host_workspace, staging.workspace, dirs_exist_ok=True)
    command = blind_runtime.build_blind_command(staging, host_env=os.environ)
    git = _launch(command, ["git", "status", "--porcelain"])
    assert git.returncode == 0, git.stderr
    assert git.stdout == ""
    uid = _launch(command, [command.python, "-c", "import os; print(os.getuid())"])
    assert uid.returncode == 0, uid.stderr
    assert int(uid.stdout) == host_workspace.stat().st_uid
    inner = [
        command.python,
        "-c",
        "import sys; data = open(sys.argv[1]).read();"
        " open(sys.argv[2], 'w').write(data.upper());"
        " print('ROUNDTRIP', data)",
        str(staging.workspace / "in.txt"),
        str(staging.workspace / "out.txt"),
    ]
    result = _launch(command, inner)
    assert result.returncode == 0, result.stderr
    assert "ROUNDTRIP hello-blind" in result.stdout
    assert (staging.workspace / "out.txt").read_text() == "HELLO-BLIND"


def test_private_sibling_is_unreadable_inside(
    bwrap_ok: None, staging: blind_runtime.StagingPlan, private_sibling: Path
) -> None:
    secret = private_sibling / "secret.txt"
    assert secret.read_text() == SECRET_TEXT  # synthetic fixture, host side
    command = blind_runtime.build_blind_command(staging, host_env=os.environ)
    inner = [
        command.python,
        "-c",
        "import sys\n"
        "try:\n"
        "    open(sys.argv[1]).read()\n"
        "    print('LEAKED')\n"
        "except OSError:\n"
        "    print('HIDDEN')\n"
        "import os\n"
        "print('EXISTS', os.path.exists(sys.argv[1]))",
        str(secret),
    ]
    result = _launch(command, inner)
    assert result.returncode == 0, result.stderr
    assert "HIDDEN" in result.stdout
    assert "LEAKED" not in result.stdout
    assert "EXISTS False" in result.stdout


def test_proc_hides_host_processes_and_session_env(
    bwrap_ok: None, staging: blind_runtime.StagingPlan
) -> None:
    command = blind_runtime.build_blind_command(
        staging,
        host_env={
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "TERM": "dumb",
        },
    )
    inner = [
        command.python,
        "-c",
        "import os\n"
        "pids = [int(p) for p in os.listdir('/proc') if p.isdigit()]\n"
        "print('PIDS', pids)\n"
        "print('HOSTPID', os.path.exists('/proc/" + str(os.getpid()) + "'))\n"
        "print('ENV', 'DBUS_SESSION_BUS_ADDRESS' in os.environ,"
        " 'XDG_RUNTIME_DIR' in os.environ)\n"
        "print('HIDDEN', os.path.exists('/home'), os.path.exists('/root'),"
        " os.path.exists('/run/user'))",
    ]
    result = _launch(command, inner)
    assert result.returncode == 0, result.stderr
    assert "HOSTPID False" in result.stdout
    assert "ENV False False" in result.stdout
    assert "HIDDEN False False False" in result.stdout
    pids_line = next(
        line for line in result.stdout.splitlines() if line.startswith("PIDS")
    )
    pids = [int(value) for value in re.findall(r"\d+", pids_line)]
    assert pids and all(pid <= 5 for pid in pids)


def test_system_python_runs_inside_sandbox(
    bwrap_ok: None, staging: blind_runtime.StagingPlan
) -> None:
    command = blind_runtime.build_blind_command(staging, host_env=os.environ)
    result = _launch(
        command,
        [command.python, "-c", "import sys; print(*sys.version_info[:2])"],
    )
    assert result.returncode == 0, result.stderr
    major, minor = (int(value) for value in result.stdout.split())
    assert (major, minor) >= blind_runtime.MINIMUM_PYTHON


def test_public_socket_and_host_network_are_reachable(
    bwrap_ok: None, staging: blind_runtime.StagingPlan
) -> None:
    unix_path = staging.runtime / "broker.sock"
    tcp_listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    tcp_listener.bind(("127.0.0.1", 0))
    tcp_listener.listen(1)
    port = tcp_listener.getsockname()[1]
    received: list[tuple[str, bytes]] = []
    received_lock = threading.Lock()

    def serve(listener: socket.socket, channel: str) -> None:
        listener.settimeout(30)
        connection, _ = listener.accept()
        with connection:
            data = connection.recv(1024)
        with received_lock:
            received.append((channel, data))

    unix_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    unix_listener.bind(str(unix_path))
    unix_listener.listen(1)
    threads = [
        threading.Thread(target=serve, args=(unix_listener, "unix"), daemon=True),
        threading.Thread(target=serve, args=(tcp_listener, "tcp"), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        command = blind_runtime.build_blind_command(staging, host_env=os.environ)
        inner = [
            command.python,
            "-c",
            "import socket, sys\n"
            "unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
            "unix.connect(sys.argv[1]); unix.sendall(b'ping-unix'); unix.close()\n"
            "tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
            "tcp.connect((sys.argv[2], int(sys.argv[3])));"
            " tcp.sendall(b'ping-tcp'); tcp.close()\n"
            "print('NETOK')",
            str(unix_path),
            "127.0.0.1",
            str(port),
        ]
        result = _launch(command, inner)
        assert result.returncode == 0, result.stderr
        assert "NETOK" in result.stdout
        for thread in threads:
            thread.join(timeout=30)
        assert ("unix", b"ping-unix") in received
        assert ("tcp", b"ping-tcp") in received
    finally:
        unix_listener.close()
        tcp_listener.close()
        unix_path.unlink(missing_ok=True)


def test_etc_is_read_only_and_serves_tls_and_dns(
    bwrap_ok: None, staging: blind_runtime.StagingPlan
) -> None:
    host_resolv = Path("/etc/resolv.conf").exists()
    host_content: str | None = None
    if host_resolv:
        host_content = Path("/etc/resolv.conf").read_text()
    host_certs = any(
        Path(candidate).exists()
        for candidate in (
            "/etc/ssl/certs",
            "/etc/pki/tls",
            "/etc/ssl/cert.pem",
            "/etc/pki/ca-trust",
        )
    )
    command = blind_runtime.build_blind_command(staging, host_env=os.environ)
    inner = [
        command.python,
        "-c",
        "import os\n"
        "content = None\n"
        "try:\n"
        "    content = open('/etc/resolv.conf').read()\n"
        "except OSError:\n"
        "    pass\n"
        "print('RESOLV', content is not None)\n"
        "print('CONTENT', (content or '').replace('\\n', ';'))\n"
        "print('CERTS', any(os.path.exists(p) for p in"
        " ('/etc/ssl/certs', '/etc/pki/tls', '/etc/ssl/cert.pem',"
        " '/etc/pki/ca-trust')))\n"
        "print('READONLY',"
        " bool(os.statvfs('/etc').f_flag & os.ST_RDONLY),"
        " bool(os.statvfs('/usr').f_flag & os.ST_RDONLY))",
    ]
    result = _launch(command, inner)
    assert result.returncode == 0, result.stderr
    assert f"RESOLV {host_resolv}" in result.stdout
    assert f"CERTS {host_certs}" in result.stdout
    assert "READONLY True True" in result.stdout
    if host_content is not None:
        normalized = host_content.replace("\n", ";")
        assert f"CONTENT {normalized}" in result.stdout
