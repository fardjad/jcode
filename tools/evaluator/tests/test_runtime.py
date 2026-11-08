"""Live blind-boundary integration tests for launch_blind_harness.

Every subprocess here runs a synthetic mock daemon/bridge inside a real
bubblewrap sandbox against a real host hook broker. No provider, no real
native jcode binary, no personal configuration, and no paid call is ever
launched; the private canary and private hook state are synthetic test
fixtures under the pytest-private temporary directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from jcode_harness_client import blind_runtime
from jcode_harness_client.hook_broker import (
    CANONICAL_NATIVE_HOOK_ENV,
    BrokerOperation,
    install_public_proxy,
)
from jcode_harness_client.runtime import (
    blind_preflight,
    launch_blind_harness,
    runtime_environment,
)

SECRET_TEXT = "synthetic-private-canary"
PRIVATE_TOKEN = "synthetic-broker-private-token"

MOCK_DAEMON_SOURCE = '''#!/usr/bin/env python3
"""Synthetic native daemon: exercises the hook boundary from inside bwrap."""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path


def main():
    daemon_socket = sys.argv[sys.argv.index("--socket") + 1]
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(daemon_socket)
    listener.listen(1)
    print("mock daemon started", flush=True)
    result = {
        "canary": "unreadable",
        "controller_source_present": None,
        "env_keys": sorted(
            key
            for key in os.environ
            if key.startswith("JCODE_")
        ),
        "home": os.environ.get("HOME"),
        "jcode_home": os.environ.get("JCODE_HOME"),
        "legit_exit": None,
        "legit_stdout": "",
        "forged_exit": None,
        "forged_stderr": "",
    }
    try:
        with open(os.environ["TEST_CANARY_PATH"], encoding="utf-8") as handle:
            result["canary"] = handle.read()
    except OSError:
        result["canary"] = "unreadable"
    result["controller_source_present"] = os.path.isfile(
        os.environ["TEST_CONTROLLER_SOURCE"]
    )
    proxy = shutil.which("hook-proxy")
    identity = dict(os.environ)
    identity.update(
        {
            "JCODE_HOOK_SESSION_ID": "sess-live-1",
            "JCODE_HOOK_TOOL_CALL_ID": "call-live-1",
            "JCODE_HOOK_EVENT": "post_tool_transform",
        }
    )
    # Wait until the host has registered this daemon before exercising hooks,
    # mirroring the real runtime where hooks only fire on client turns.
    trigger = os.environ["TEST_HOOK_TRIGGER"]
    while not os.path.exists(trigger):
        time.sleep(0.05)
    if proxy is None:
        result["legit_exit"] = -1
        result["forged_exit"] = -1
    else:
        payload_root = Path(os.environ["JCODE_RUNTIME_DIR"]) / "jcode-tool-payloads"
        payload_root.mkdir()
        payload = {}
        for key in ("tool_input_file", "tool_result_file"):
            path = payload_root / key
            path.write_text(key + " contents")
            payload[key] = str(path)
        legit = subprocess.run(
            [proxy, "guard"],
            input=json.dumps(payload).encode(),
            env=identity,
            capture_output=True,
        )
        result["legit_exit"] = legit.returncode
        result["legit_stdout"] = legit.stdout.decode("utf-8", "replace")
        forged = subprocess.run(
            [
                "/bin/sh",
                "-c",
                '"$1" guard; exit $?',
                "sh",
                proxy,
            ],
            input=b'{"probe": "forged"}',
            env=identity,
            capture_output=True,
        )
        result["forged_exit"] = forged.returncode
        result["forged_stderr"] = forged.stderr.decode("utf-8", "replace")
    with open("findings.json", "w", encoding="utf-8") as handle:
        json.dump(result, handle)
    while True:
        time.sleep(1)


main()
'''

MOCK_BRIDGE_SOURCE = '''#!/usr/bin/env python3
"""Synthetic bridge: minimal NDJSON harness API responder."""

import json
import socket
import sys
import threading


def handle(conn):
    reader = conn.makefile("rb")
    try:
        for line in reader:
            if not line.strip():
                continue
            try:
                frame = json.loads(line)
            except ValueError:
                continue
            if frame.get("req") == "hello":
                reply = {
                    "v": 1,
                    "reply_to": frame.get("id"),
                    "ev": "hello_ok",
                    "version": 1,
                    "server": "mock-blind-bridge",
                }
            else:
                reply = {
                    "v": 1,
                    "reply_to": frame.get("id"),
                    "ev": "error",
                    "code": "mock",
                }
            conn.sendall((json.dumps(reply) + "\\n").encode("utf-8"))
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main():
    api_socket = sys.argv[1]
    print("mock bridge started", flush=True)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(api_socket)
    listener.listen(4)
    while True:
        conn, _ = listener.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


main()
'''


def _bwrap_available() -> bool:
    if shutil.which("bwrap") is None:
        return False
    try:
        blind_runtime.system_python()
    except RuntimeError:
        return False
    return True


@pytest.fixture()
def staging(tmp_path: Path):
    if not _bwrap_available():
        pytest.skip("bwrap or a plain system python is not available")
    source_dir = tmp_path / "native"
    source_dir.mkdir()
    daemon_source = source_dir / "mock-daemon"
    daemon_source.write_text(MOCK_DAEMON_SOURCE, encoding="utf-8")
    daemon_source.chmod(0o755)
    bridge_source = source_dir / "mock-bridge"
    bridge_source.write_text(MOCK_BRIDGE_SOURCE, encoding="utf-8")
    bridge_source.chmod(0o755)
    base = blind_runtime.public_staging_base()
    root = Path(tempfile.mkdtemp(prefix="jcode-blind-test-", dir=str(base)))
    plan = blind_runtime.plan_staging(
        root, jcode_binary=daemon_source, bridge=bridge_source
    )
    install_public_proxy(plan.bin / "hook-proxy")
    try:
        yield plan
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _wait_for_file(path: Path, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {path}")


def _private_hook_operation(marker: Path) -> BrokerOperation:
    return BrokerOperation(
        name="guard",
        command=[
            blind_runtime.system_python(),
            "-c",
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "payload = json.load(sys.stdin)\n"
            "root = Path(os.environ['JCODE_RUNTIME_DIR']) / 'jcode-tool-payloads'\n"
            "contents = []\n"
            "for value in payload.values():\n"
            "    path = Path(value).resolve(strict=True)\n"
            "    assert path.is_relative_to(root.resolve())\n"
            "    contents.append(path.read_text())\n"
            "token = os.environ.get('GUARD_PRIVATE_TOKEN', 'none')\n"
            "open(sys.argv[1], 'w').write('|'.join(contents) + '|' + token)\n"
            "print('GUARD-OK')\n",
            str(marker),
        ],
        env={"GUARD_PRIVATE_TOKEN": PRIVATE_TOKEN, "JCODE_RUNTIME_DIR": "/wrong-private-root"},
        serialized=True,
    )


def test_blind_launch_registers_daemon_and_runs_hook_turn(staging, tmp_path):
    """Legitimate parent proxy turns run host hooks; forged shells are denied."""
    private = tmp_path / "private"
    private.mkdir()
    canary = private / "canary.txt"
    canary.write_text(SECRET_TEXT, encoding="utf-8")
    controller_source = private / "escalation_controller.py"
    controller_source.write_text("# synthetic private evaluator source\n")
    marker = private / "hook-marker.txt"
    broker_log = private / "hook-broker.log"
    trigger = staging.workspace / "hook-trigger"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "task.txt").write_text("fixture\n", encoding="utf-8")
    findings = staging.workspace / "findings.json"
    with launch_blind_harness(
        staging=staging,
        config="[features]\nmemory = false\n",
        profile="",
        model="fixture",
        workspace=workspace,
        environment={
            "TEST_CANARY_PATH": str(canary),
            "TEST_CONTROLLER_SOURCE": str(controller_source),
            "TEST_HOOK_TRIGGER": str(trigger),
        },
        broker_operations={"guard": _private_hook_operation(marker)},
        broker_log=broker_log,
        turn_timeout=30,
        daemon_prefix=[blind_runtime.system_python()],
        bridge_prefix=[blind_runtime.system_python()],
    ) as runtime:
        assert runtime.broker is not None
        assert runtime.broker.registered_native_pids == {runtime.native_pid}
        assert os.path.exists(f"/proc/{runtime.native_pid}")
        assert "mock daemon started" in (runtime.home / "daemon.log").read_text()
        assert "mock bridge started" in (runtime.home / "bridge.log").read_text()
        assert not (staging.home / "daemon.log").exists()
        trigger.write_text("go\n", encoding="utf-8")
        _wait_for_file(findings)
    result = json.loads(findings.read_text(encoding="utf-8"))
    # The legitimate proxy child of the registered daemon runs the host hook.
    assert result["legit_exit"] == 0
    assert "GUARD-OK" in result["legit_stdout"]
    # The forged shell (unregistered proxy parent) is denied, fail closed.
    assert result["forged_exit"] == 70
    assert "denied" in result["forged_stderr"]
    # The private hook executed on the host with its private broker env.
    marker_text = marker.read_text(encoding="utf-8")
    assert marker_text == 'tool_input_file contents|tool_result_file contents|' + PRIVATE_TOKEN
    # Broker details stay host-private; the log records the denial.
    log_text = broker_log.read_text(encoding="utf-8")
    assert '"event": "denied"' in log_text
    assert not staging.workspace.joinpath("hook-broker.log").exists()
    assert result["canary"] == "unreadable"
    assert result["controller_source_present"] is False
    assert result["env_keys"] == [
        "JCODE_HOME", "JCODE_HOOK_BROKER_ENDPOINT", "JCODE_RUNTIME_DIR",
    ]
    assert result["home"] == str(staging.home)
    assert result["jcode_home"] == str(staging.runtime / "jcode")
    assert not (workspace / "findings.json").exists()
    assert canary.read_text() == SECRET_TEXT


def test_runtime_environment_drops_personal_path_and_routing(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", "/private/mise/shims:/private/bin")
    monkeypatch.setenv("CARGO_HOME", "/private/cargo")
    monkeypatch.setenv("RUSTUP_HOME", "/private/rustup")
    monkeypatch.setenv("JCODE_HOME", "/private/jcode")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-inherited-secret")
    env = runtime_environment(tmp_path, {"EXPLICIT_KEY": "synthetic"})
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["JCODE_HOME"] == str(tmp_path / "jcode")
    assert env["EXPLICIT_KEY"] == "synthetic"
    assert not {"CARGO_HOME", "RUSTUP_HOME", "OPENAI_API_KEY"} & env.keys()


def test_blind_preflight_fails_closed_without_bwrap(monkeypatch, tmp_path):
    """A missing bwrap aborts the launch; there is no pretend-blind fallback."""
    monkeypatch.setattr(
        "jcode_harness_client.runtime.blind_preflight",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("bwrap is required for the blind runtime")
        ),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(RuntimeError, match="bwrap"):
        with launch_blind_harness(
            staging=blind_runtime.StagingPlan(
                root=tmp_path / "missing-root",
                dirs=(),
                files=(),
            ),
            config="",
            profile="",
            model="fixture",
            workspace=workspace,
            turn_timeout=5,
        ):
            pytest.fail("preflight failure must not yield a runtime")


def test_blind_preflight_requires_real_prerequisites(tmp_path):
    """Preflight reports missing binaries instead of degrading the boundary."""
    if shutil.which("bwrap") is None:
        pytest.skip("bwrap is not installed")
    with pytest.raises(RuntimeError, match="jcode binary"):
        blind_preflight(tmp_path / "missing-jcode", tmp_path / "missing-bridge")


def test_find_child_and_group_discovery(tmp_path):
    """Exact-command discovery matches only the expected child processes."""
    if not _bwrap_available():
        pytest.skip("bwrap or a plain system python is not available")
    command = [
        blind_runtime.system_python(),
        "-c",
        "import time\nwhile True: time.sleep(1)\n",
    ]
    decoy = [
        blind_runtime.system_python(),
        "-c",
        "import time\nwhile True: time.sleep(1)\n#decoy\n",
    ]
    process = subprocess.Popen(
        command, start_new_session=True, stdout=subprocess.DEVNULL
    )
    try:
        pid = blind_runtime.find_group_member(process.pid, command, timeout=10)
        assert pid == process.pid
        with pytest.raises(TimeoutError):
            blind_runtime.find_group_member(
                process.pid, decoy, timeout=0.3, poll_interval=0.05
            )
        child = subprocess.Popen(
            command + ["#child"],
            stdout=subprocess.DEVNULL,
        )
        try:
            with pytest.raises(TimeoutError):
                blind_runtime.find_child_pid(
                    process.pid, decoy, timeout=0.3, poll_interval=0.05
                )
            assert child.pid > 0
        finally:
            child.kill()
            child.wait()
    finally:
        process.kill()
        process.wait()


# -- actual-native blind hook regression (provider-free) ----------------------
#
# Retained hash-verified binaries only; never build or call a provider.

_NATIVE_GUARD_DUMP_CODE = (
    "import json, os, sys\n"
    "record = {'payload': sys.stdin.read(),\n"
    "          'env': {k: v for k, v in os.environ.items()\n"
    "                  if k.startswith('JCODE_HOOK_') or k.startswith('GUARD_')\n"
    "                  or k == 'JCODE_RUNTIME_DIR'}}\n"
    "with open(sys.argv[1], 'a') as handle:\n"
    "    handle.write(json.dumps(record) + '\\n')\n"
    "# Neutral stdout for post_tool_transform: no replacement requested.\n"
    "print('{}')\n"
)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _native_build_binaries() -> dict[str, Path] | None:
    """Locate the newest retained build whose recorded hashes verify."""
    repo_root = Path(__file__).resolve().parents[3]
    override = os.environ.get("JCODE_EVALUATOR_NATIVE_BUILD")
    if override:
        candidates = [Path(override)]
    else:
        builds = repo_root / ".tools" / "worker-eval" / "builds"
        candidates = sorted(
            (path for path in builds.glob("*/success.json") if path.is_file()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    for success_path in candidates:
        try:
            record = json.loads(success_path.read_text(encoding="utf-8"))
        except OSError, ValueError:
            continue
        binaries: dict[str, Path] = {}
        verified = True
        for name in ("jcode", "jcode-harness-api-bridge"):
            entry = (record.get("binaries") or {}).get(name)
            path = Path(entry["path"]) if entry else None
            if (
                path is None
                or not path.is_file()
                or _file_sha256(path) != entry.get("sha256")
            ):
                verified = False
                break
            binaries[name] = path
        if verified:
            return binaries
    return None


@pytest.fixture()
def native_staging(tmp_path: Path):
    """Staging plan with the real retained native binaries, or skip."""
    if not _bwrap_available():
        pytest.skip("bwrap or a plain system python is not available")
    binaries = _native_build_binaries()
    if binaries is None:
        pytest.skip(
            "no verified built native artifacts retained under "
            ".tools/worker-eval/builds (set JCODE_EVALUATOR_NATIVE_BUILD to "
            "point at a success.json stamp)"
        )
    base = blind_runtime.public_staging_base()
    root = Path(tempfile.mkdtemp(prefix="jcode-blind-native-", dir=str(base)))
    plan = blind_runtime.plan_staging(
        root,
        jcode_binary=binaries["jcode"],
        bridge=binaries["jcode-harness-api-bridge"],
    )
    install_public_proxy(plan.bin / "hook-proxy")
    try:
        yield plan, binaries
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _debug_client_env(tmp_path: Path) -> dict[str, str]:
    """Disposable env for the host-side debug socket client."""
    isolated = tmp_path / "debug-client"
    for name in ("home", "jcode"):
        (isolated / name).mkdir(parents=True, exist_ok=True)
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(isolated / "home"),
        "JCODE_HOME": str(isolated / "jcode"),
        "XDG_CONFIG_HOME": str(isolated / "xdg" / "config"),
        "XDG_CACHE_HOME": str(isolated / "xdg" / "cache"),
        "XDG_DATA_HOME": str(isolated / "xdg" / "data"),
        "XDG_STATE_HOME": str(isolated / "xdg" / "state"),
        "LANG": "C",
    }


def test_actual_native_blind_hook_round_trip_is_accepted(native_staging, tmp_path):
    """A real native daemon in bwrap runs the real read tool and its real
    post_tool hook round-trips through the broker and is ACCEPTED.

    Regression for the hook-env mismatch: the actual native hook environment
    (JCODE_HOOK_CWD and the full curated context) used to be rejected by the
    broker's old 8-key allowlist. The proxy now forwards only the canonical
    whitelist and the broker accepts the curated full native context.
    Provider-free: no LLM, no provider, no paid call; driven entirely through
    the host debug socket with the fixture model.
    """
    staging, binaries = native_staging
    jcode = binaries["jcode"]
    private = tmp_path / "private"
    private.mkdir()
    marker = private / "native-hook-record.json"
    broker_log = private / "hook-broker.log"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    probe = workspace / "probe.txt"
    probe.write_text("blind native hook regression ok\n", encoding="utf-8")
    proxy = staging.bin / "hook-proxy"
    blueprint_source = Path(__file__).resolve().parents[3] / "customizations/workers/blueprints"
    blueprints = staging.config / "blueprints"
    shutil.copytree(blueprint_source, blueprints)
    worker_ids = {path.stem for path in blueprints.glob("*.md")} - {"coordinator"}
    assert worker_ids
    # HookCommands entries are full command lines (not argv): one command
    # line per entry. post_tool_transform carries the real envelope payload
    # on stdin with TOOL_CALL_ID; post_tool is an observer with null stdin
    # (the proxy forwards the neutral placeholder payload).
    hook_line = json.dumps(f"{proxy} guard")
    config = (
        "[features]\n"
        "memory = false\n"
        "\n[display]\n"
        "debug_socket = true\n"
        "\n[hooks]\n"
        f"post_tool_transform = [{hook_line}]\n"
        "post_tool_transform_timeout_ms = 5000\n"
        f"post_tool = [{hook_line}]\n"
        "\n[agents]\n"
        f"worker_blueprints_dir = {json.dumps(str(blueprints))}\n"
    )
    operation = BrokerOperation(
        name="guard",
        command=[
            blind_runtime.system_python(),
            "-c",
            _NATIVE_GUARD_DUMP_CODE,
            str(marker),
        ],
        env={
            "GUARD_PRIVATE_TOKEN": PRIVATE_TOKEN,
            "GUARD_SELECTOR": "strict",
            "GUARD_THRESHOLD": "3",
        },
        serialized=True,
    )
    daemon_socket = staging.runtime / "daemon.sock"
    client_env = _debug_client_env(tmp_path)
    with launch_blind_harness(
        staging=staging,
        config=config,
        profile="",
        model="fixture",
        workspace=workspace,
        environment={"JCODE_SWARM_ENABLED": "true", "JCODE_SWARM_SPAWN_MODE": "headless"},
        broker_operations={"guard": operation},
        broker_log=broker_log,
        turn_timeout=30,
    ) as runtime:
        assert runtime.broker is not None
        assert runtime.broker.registered_native_pids == {runtime.native_pid}
        session = runtime.client.create_session(working_dir=runtime.workspace)
        tools = {tool.name for tool in runtime.client.list_tools(session.session_id)}
        # Effective coordinator policy, not merely staged file presence.
        assert {"read", "write", "todo", "skill_manage"} <= tools
        assert not {"bash", "edit", "glob", "grep", "swarm"} & tools
        diagnostics = [
            line for path in runtime.home.rglob("*.log")
            for line in path.read_text(errors="replace").splitlines()
            if "blueprint" in line.lower()
        ]
        created = subprocess.run(
            [str(jcode), "debug", "-s", str(daemon_socket), "create_session"],
            capture_output=True,
            env=client_env,
            timeout=60,
        )
        assert created.returncode == 0, (created.stdout + created.stderr).decode(
            "utf-8", "replace"
        )[:500]
        tool_argv = [
            str(jcode),
            "debug",
            "-s",
            str(daemon_socket),
            "tool",
            "read " + json.dumps({"file_path": str(staging.workspace / "probe.txt")}),
        ]
        tool = subprocess.run(
            tool_argv, capture_output=True, env=client_env, timeout=60
        )
        assert tool.returncode == 0, (tool.stdout + tool.stderr).decode(
            "utf-8", "replace"
        )[:500]
        # The real read tool executed inside the bwrap sandbox.
        assert "blind native hook regression ok" in tool.stdout.decode(
            "utf-8", "replace"
        )
        _wait_for_file(marker, timeout=60.0)
        # The transform is synchronous, but the post_tool observer is
        # detached: wait until both hook records have landed.
        deadline = time.monotonic() + 60.0
        events: set[str] = set()
        while time.monotonic() < deadline:
            events = {
                json.loads(line)["env"].get("JCODE_HOOK_EVENT")
                for line in marker.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
            if {"post_tool_transform", "post_tool"} <= events:
                break
            time.sleep(0.1)
        assert {"post_tool_transform", "post_tool"} <= events, events
    records = [
        json.loads(line)
        for line in marker.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    by_event = {record["env"].get("JCODE_HOOK_EVENT"): record for record in records}
    # Both native hook events reached the host broker and were ACCEPTED.
    assert set(by_event) == {"post_tool_transform", "post_tool"}, by_event.keys()
    transform = by_event["post_tool_transform"]
    observer = by_event["post_tool"]
    for record in (transform, observer):
        env = record["env"]
        assert env["JCODE_HOOK_TOOL_NAME"] == "read"
        assert env["JCODE_HOOK_STATUS"] == "ok"
        assert env["JCODE_HOOK_CWD"]
        assert env["JCODE_HOOK_SESSION_ID"].startswith("session_")
        assert env["JCODE_HOOK_TOOL_CALL_ID"]
        # Only canonical whitelist keys crossed: the native payload mirror
        # and any unknown JCODE_HOOK_* key were filtered by the proxy.
        forwarded = {key for key in env if key.startswith("JCODE_HOOK_")}
        assert forwarded <= CANONICAL_NATIVE_HOOK_ENV
        assert "JCODE_HOOK_PAYLOAD" not in forwarded
        # Guard selector/threshold are broker-private operation env: applied
        # host-side to the hook, never part of the sandbox request.
        assert env["GUARD_SELECTOR"] == "strict"
        assert env["GUARD_THRESHOLD"] == "3"
        assert env["JCODE_RUNTIME_DIR"] == str(staging.runtime)
    # The synchronous transform received the real envelope payload on stdin
    # (the framed payload crosses byte-exact, not as an env var).
    assert "probe.txt" in transform["payload"]
    assert "blind native hook regression ok" in transform["payload"]
    # The observer hook has no stdin: the proxy sent the neutral placeholder.
    assert observer["payload"] == "{}"
    assert "JCODE_HOOK_DURATION_MS" in observer["env"]
    # The broker accepted every request: no invalid or denied events, and
    # each hook completed with exit status 0.
    log_text = broker_log.read_text(encoding="utf-8")
    assert '"event": "invalid"' not in log_text
    assert '"event": "denied"' not in log_text
    assert log_text.count('"event": "hook_end"') == len(records)
    assert '"exit_code": 0' in log_text
    assert {"swarm_" + worker_id for worker_id in worker_ids} <= tools, (
        "Staged workers are missing from the retained-native effective tools: "
        + repr(sorted(tools)) + "\n" + "\n".join(diagnostics)
    )
    assert not any("[excluded]" in line for line in diagnostics), diagnostics
