"""Real-process tests for the host hook broker and public sandbox proxy.

Every subprocess launched here (the simulated native helper, its proxy and
raw-client children, and the hook commands) receives a freshly built
environment with disposable HOME, XDG_*, and JCODE_HOME directories: no
inherited user configuration, personal path, or credential is ever
visible. Secrets are synthetic strings written into temp directories only.
No provider, network, or real jcode process is involved.
"""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from jcode_harness_client import hook_broker

SECRET = "synthetic-broker-secret-8675309"
SESSION_ID = "session_test_1"
CALL_ID = "call_test_1"
PAYLOAD_TEXT = '{"tool_name":"Bash","input":{"command":"cat staged.txt"}}'
IDENTITY = {
    "JCODE_HOOK_SESSION_ID": SESSION_ID,
    "JCODE_HOOK_TOOL_CALL_ID": CALL_ID,
    "JCODE_HOOK_TOOL_NAME": "Bash",
    "JCODE_HOOK_PROCESS_ROLE": "main",
}

# The full actual native hook context (apply_event_env plus its event
# fields) as a real native hook process presents it to the public proxy.
NATIVE_ENV = {
    "JCODE_HOOK_EVENT": "post_tool",
    "JCODE_HOOK_SESSION_ID": SESSION_ID,
    "JCODE_HOOK_TOOL_CALL_ID": CALL_ID,
    "JCODE_HOOK_TOOL_NAME": "read",
    "JCODE_HOOK_TOOL_INPUT": '{"file_path":"/public/workspace/probe.txt"}',
    "JCODE_HOOK_CWD": "/public/workspace",
    "JCODE_HOOK_STATUS": "ok",
    "JCODE_HOOK_DURATION_MS": "42",
    "JCODE_HOOK_OUTPUT_BYTES": "128",
    "JCODE_HOOK_ERROR": "",
    "JCODE_HOOK_MODEL": "fixture-model",
    "JCODE_HOOK_PROCESS_ROLE": "main",
    "JCODE_HOOK_SWARM_ENABLED": "1",
    "JCODE_HOOK_SWARM_MODE": "background",
    "JCODE_HOOK_WORKER_ID": "worker-1",
    "JCODE_HOOK_WORKER_BLUEPRINT_ID": "routine",
    "JCODE_HOOK_WORKER_BLUEPRINT_NAME": "Routine",
}

# Unexpected keys the native environment never sets and the proxy must
# filter out (never forwarded, never accepted): the payload mirror, an
# untrusted synthetic secret, and credential-shaped extras.
UNEXPECTED_ENV = {
    "JCODE_RUNTIME_DIR": "/untrusted-payload-root",
    "JCODE_HOOK_PAYLOAD": '{"future":"payload-mirror"}',
    "JCODE_HOOK_UNTRUSTED": "synthetic-untrusted-secret-424242",
    "JCODE_HOOK_API_TOKEN": "synthetic-token-state",
    "JCODE_HOOK_EMPTY": "",
}

_COUNTER = itertools.count()


GUARD_HOOK_SOURCE = """
import os
import sys

data = sys.stdin.buffer.read()
if len(sys.argv) > 1:
    with open(sys.argv[1], "w") as handle:
        handle.write("ran")
out = sys.stdout.buffer
out.write(b"guard-allowed\\n")
out.write(data)
out.write(b"\\nsession=")
out.write(os.environ.get("JCODE_HOOK_SESSION_ID", "").encode())
out.write(b" role=")
out.write(os.environ.get("JCODE_HOOK_PROCESS_ROLE", "").encode())
out.write(b"\\n")
"""

CRASH_HOOK_SOURCE = """
import sys

sys.stdout.write("partial-output\\n")
sys.exit(9)
"""

SLOW_HOOK_SOURCE = """
import time

time.sleep(5)
"""

CONTROLLER_HOOK_SOURCE = """
import sys
import time

with open(sys.argv[1], "a") as handle:
    handle.write("enter\\n")
    handle.flush()
    time.sleep(0.4)
    handle.write("exit\\n")
"""

# Dumps the exact environment the host hook received (plus the stdin
# payload) so tests can prove which keys crossed the proxy/broker boundary.
ENV_DUMP_HOOK_SOURCE = """
import json
import os
import sys

record = {
    "payload": sys.stdin.read(),
    "env": dict(os.environ),
}
with open(sys.argv[1], "w") as handle:
    json.dump(record, handle)
print("ENV-DUMP-OK")
"""

# The simulated native process. It waits for the registration trigger,
# then behaves per mode:
#   proxy      - spawn the installed proxy as its direct child
#   grandchild - spawn an unregistered intermediate that spawns the proxy
#   raw        - spawn a test-authored raw socket client as its child
#   parallel   - spawn two proxies concurrently (controller serialization)
HELPER_SOURCE = """
import os
import subprocess
import sys
import time

mode, trigger_path, result_path = sys.argv[1], sys.argv[2], sys.argv[3]
deadline = time.monotonic() + 60.0
while not os.path.exists(trigger_path) and time.monotonic() < deadline:
    time.sleep(0.02)
if not os.path.exists(trigger_path):
    raise SystemExit(3)

if mode == "proxy":
    proc = subprocess.run(
        [os.environ["TEST_PROXY_PATH"], os.environ["TEST_OP"]],
        input=os.environ.get("TEST_PAYLOAD", "").encode(),
        capture_output=True,
    )
    record = {
        "returncode": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", "replace"),
        "stderr": proc.stderr.decode("utf-8", "replace"),
    }
elif mode == "grandchild":
    raise SystemExit(
        subprocess.call([sys.executable, "-c", os.environ["TEST_INTERMEDIATE_CODE"]])
    )
elif mode == "raw":
    raise SystemExit(
        subprocess.call([sys.executable, os.environ["TEST_RAW_PATH"]])
    )
elif mode == "parallel":
    procs = [
        subprocess.Popen(
            [os.environ["TEST_PROXY_PATH"], os.environ["TEST_OP"]],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    outs = []
    for proc in procs:
        out, err = proc.communicate(
            os.environ.get("TEST_PAYLOAD", "").encode()
        )
        outs.append(
            {
                "returncode": proc.returncode,
                "stdout": out.decode("utf-8", "replace"),
                "stderr": err.decode("utf-8", "replace"),
            }
        )
    record = {"results": outs}
else:
    raise SystemExit(4)

import json

with open(result_path, "w") as handle:
    json.dump(record, handle)
"""

# The forged agent-descendant intermediate: an unregistered process that
# spawns the proxy, so the proxy's parent is not a registered native pid.
INTERMEDIATE_CODE = (
    "import json, os, subprocess\n"
    "proc = subprocess.run(\n"
    "    [os.environ['TEST_PROXY_PATH'], os.environ['TEST_OP']],\n"
    "    input=os.environ.get('TEST_PAYLOAD', '').encode(),\n"
    "    capture_output=True)\n"
    "with open(os.environ['TEST_RESULT_PATH'], 'w') as handle:\n"
    "    json.dump({'returncode': proc.returncode,\n"
    "               'stdout': proc.stdout.decode('utf-8', 'replace'),\n"
    "               'stderr': proc.stderr.decode('utf-8', 'replace')}, handle)\n"
)

RAW_CLIENT_TEMPLATE = """import os
import socket
import struct

conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
conn.settimeout(20)
conn.connect(os.environ["JCODE_HOOK_BROKER_ENDPOINT"])
__BODY__
header = b""
while len(header) < 8:
    chunk = conn.recv(8 - len(header))
    if not chunk:
        raise SystemExit(2)
    header += chunk
length = struct.unpack(">Q", header)[0]
body = b""
while len(body) < length:
    chunk = conn.recv(min(65536, length - len(body)))
    if not chunk:
        raise SystemExit(2)
    body += chunk
with open(os.environ["TEST_RAW_RESULT"], "w") as handle:
    handle.write(body.decode("utf-8", "replace"))
"""


def _raw_source(body: str) -> str:
    return RAW_CLIENT_TEMPLATE.replace("__BODY__", body)


def _request_code(
    header: dict,
    *,
    header_length: int | None = None,
    payload_length: int | None = None,
    payload: bytes = b"{}",
) -> str:
    """Build raw-client code sending one framed request."""
    encoded = json.dumps(header).encode("utf-8")
    header_len = len(encoded) if header_length is None else header_length
    payload_len = len(payload) if payload_length is None else payload_length
    code = f"conn.sendall(struct.pack('>Q', {header_len}))\n"
    code += f"conn.sendall({encoded!r})\n"
    code += f"conn.sendall(struct.pack('>Q', {payload_len}))\n"
    code += f"conn.sendall({payload!r})\n"
    return code


def _valid_header(
    *,
    op: str = "guard",
    session_id: str = SESSION_ID,
    call_id: str = CALL_ID,
    hook_env_extra: dict | None = None,
    **overrides: object,
) -> dict:
    hook_env = dict(IDENTITY)
    if hook_env_extra:
        hook_env.update(hook_env_extra)
    header = {
        "version": hook_broker.PROTOCOL_VERSION,
        "op": op,
        "session_id": session_id,
        "call_id": call_id,
        "hook_env": hook_env,
    }
    header.update(overrides)
    return header


@pytest.fixture()
def workdir():
    """A short-lived scratch tree for sockets, scripts, and synthetic state."""
    path = Path(tempfile.mkdtemp(prefix="hookbroker-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _write_scripts(workdir: Path) -> None:
    (workdir / "helper.py").write_text(HELPER_SOURCE)
    (workdir / "guard_hook.py").write_text(GUARD_HOOK_SOURCE)
    (workdir / "crash_hook.py").write_text(CRASH_HOOK_SOURCE)
    (workdir / "slow_hook.py").write_text(SLOW_HOOK_SOURCE)
    (workdir / "controller_hook.py").write_text(CONTROLLER_HOOK_SOURCE)
    (workdir / "env_dump_hook.py").write_text(ENV_DUMP_HOOK_SOURCE)


def _isolated_env(workdir: Path, **extra: str) -> dict[str, str]:
    """Rebuild a subprocess env from scratch with disposable personal dirs."""
    isolated = workdir / "isolated"
    for name in ("home", "tmp", "jcode"):
        (isolated / name).mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(isolated / "home"),
        "TMPDIR": str(isolated / "tmp"),
        "XDG_CONFIG_HOME": str(isolated / "xdg" / "config"),
        "XDG_CACHE_HOME": str(isolated / "xdg" / "cache"),
        "XDG_DATA_HOME": str(isolated / "xdg" / "data"),
        "XDG_STATE_HOME": str(isolated / "xdg" / "state"),
        "XDG_RUNTIME_DIR": str(isolated / "tmp"),
        "JCODE_HOME": str(isolated / "jcode"),
        "LANG": "C",
    }
    env.update(extra)
    return env


def _make_operation(name: str, command: list[str], **kwargs: object):
    private_env = {
        "PATH": "/usr/bin:/bin",
        "JCODE_HARNESS_BROKER_SECRET": SECRET,
        "JCODE_ESCALATION_CONTROLLER_STATE_DIR": "/nonexistent-synthetic-state",
    }
    return hook_broker.BrokerOperation(
        name=name, command=tuple(command), env=private_env, **kwargs
    )


def _start_broker(workdir: Path, operations, **kwargs) -> hook_broker.HostHookBroker:
    log_path = workdir / "private" / "broker.log"
    broker = hook_broker.HostHookBroker(
        workdir / "hook-broker.sock",
        operations,
        log_path=log_path,
        **kwargs,
    )
    return broker.start()


def _identity_env(broker: hook_broker.HostHookBroker, **extra: str) -> dict[str, str]:
    env = dict(IDENTITY)
    env[hook_broker.PROXY_ENDPOINT_ENV] = str(broker.endpoint)
    env.update(extra)
    return env


def _wait_for_exec(proc: subprocess.Popen) -> None:
    """Wait until a freshly forked child has exec'd and has a cmdline.

    ``Popen`` can return before the child's ``exec`` completes, so a
    registration read of ``/proc/<pid>/cmdline`` right after launch races
    with an empty pre-exec cmdline. A real runner registers a launched,
    exec'd child; these tests mirror that readiness wait.
    """
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return
        try:
            if Path(f"/proc/{proc.pid}/cmdline").read_bytes():
                return
        except OSError:
            pass
        time.sleep(0.005)


def _launch_helper(
    workdir: Path, mode: str, env: dict[str, str]
) -> tuple[subprocess.Popen, Path, Path]:
    """Launch the simulated native helper and its trigger/result paths."""
    count = next(_COUNTER)
    trigger = workdir / f"trigger-{mode}-{count}.txt"
    result = workdir / f"result-{mode}-{count}.json"
    argv = [
        sys.executable,
        str(workdir / "helper.py"),
        mode,
        str(trigger),
        str(result),
    ]
    proc = subprocess.Popen(argv, env=env)
    _wait_for_exec(proc)
    return proc, trigger, result


def _run_proxy_request(
    broker: hook_broker.HostHookBroker,
    workdir: Path,
    *,
    op: str,
    payload: str = PAYLOAD_TEXT,
    unregister: bool = False,
    extra_env: dict | None = None,
) -> dict:
    """Run one authorized proxy request through a registered helper."""
    identity = _identity_env(
        broker,
        TEST_PROXY_PATH=str(workdir / "hook-proxy"),
        TEST_OP=op,
        TEST_PAYLOAD=payload,
    )
    if extra_env:
        identity.update(extra_env)
    env = _isolated_env(workdir, **identity)
    proc, trigger, result = _launch_helper(workdir, "proxy", env)
    try:
        broker.register_native_pid(proc.pid, proc.args)
        if unregister:
            broker.unregister_native_pid(proc.pid)
        trigger.write_text("go")
        proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 0, helper_failure(result, proc.returncode)
    return json.loads(result.read_text())


def helper_failure(result: Path, code: int | None) -> str:
    text = result.read_text() if result.exists() else "<no result file>"
    return f"helper exit {code}: {text[:500]}"


def _no_private_leak(text: str, workdir: Path) -> None:
    assert SECRET not in text
    assert str(workdir) not in text
    assert "Traceback" not in text


def test_authorized_direct_child_proxy_runs_hook(workdir):
    """A registered native parent's direct proxy child is authorized."""
    _write_scripts(workdir)
    hook_broker.install_public_proxy(workdir / "hook-proxy")
    operation = _make_operation(
        "guard", [sys.executable, str(workdir / "guard_hook.py")]
    )
    with _start_broker(workdir, [operation]) as broker:
        result = _run_proxy_request(broker, workdir, op="guard")
    expected = (
        "guard-allowed\n" + PAYLOAD_TEXT + "\nsession=" + SESSION_ID + " role=main\n"
    )
    assert result["returncode"] == 0, result["stderr"]
    assert result["stdout"] == expected
    assert result["stderr"] == ""
    _no_private_leak(result["stdout"] + result["stderr"], workdir)


def test_proxy_forwards_only_canonical_native_env(workdir):
    """The full native env crosses filtered to the whitelist, accepted."""
    _write_scripts(workdir)
    hook_broker.install_public_proxy(workdir / "hook-proxy")
    marker = workdir / "env-dump.json"
    operation = _make_operation(
        "guard", [sys.executable, str(workdir / "env_dump_hook.py"), str(marker)]
    )
    with _start_broker(workdir, [operation]) as broker:
        result = _run_proxy_request(
            broker,
            workdir,
            op="guard",
            extra_env={**NATIVE_ENV, **UNEXPECTED_ENV},
        )
    assert result["returncode"] == 0, result["stderr"]
    assert result["stdout"] == "ENV-DUMP-OK\n"
    record = json.loads(marker.read_text())
    assert record["payload"] == PAYLOAD_TEXT
    env = record["env"]
    for key, value in NATIVE_ENV.items():
        if value:
            assert env.get(key) == value, key
    for key in UNEXPECTED_ENV:
        assert key not in env
    forwarded = {key for key in env if key.startswith("JCODE_HOOK_")}
    assert forwarded <= hook_broker.CANONICAL_NATIVE_HOOK_ENV
    # Private operation env reaches the hook host-side only.
    assert env["JCODE_HARNESS_BROKER_SECRET"] == SECRET
    log_text = (workdir / "private" / "broker.log").read_text()
    assert '"event": "invalid"' not in log_text
    _no_private_leak(result["stdout"] + result["stderr"], workdir)


def test_native_tool_input_size_bound_is_exact(workdir):
    """A native-truncated 16 KiB TOOL_INPUT passes; one byte more fails."""
    _write_scripts(workdir)
    hook_broker.install_public_proxy(workdir / "hook-proxy")
    operation = _make_operation(
        "guard", [sys.executable, str(workdir / "guard_hook.py")]
    )
    limit = hook_broker.DEFAULT_HOOK_ENV_VALUE_MAX
    with _start_broker(workdir, [operation]) as broker:
        accepted = _run_proxy_request(
            broker,
            workdir,
            op="guard",
            extra_env={"JCODE_HOOK_TOOL_INPUT": "x" * limit},
        )
        rejected = _run_raw_request(
            broker,
            workdir,
            _request_code(
                _valid_header(
                    hook_env_extra={"JCODE_HOOK_TOOL_INPUT": "x" * (limit + 1)}
                )
            ),
        )
    assert accepted["returncode"] == 0, accepted["stderr"]
    assert accepted["stdout"].startswith("guard-allowed")
    assert rejected["ok"] is False
    assert rejected["error"] == "invalid_request"


def test_nonzero_hook_exit_status_is_preserved(workdir):
    """The hook's own exit status and partial output reach the proxy."""
    _write_scripts(workdir)
    hook_broker.install_public_proxy(workdir / "hook-proxy")
    operation = _make_operation(
        "guard", [sys.executable, str(workdir / "crash_hook.py")]
    )
    with _start_broker(workdir, [operation]) as broker:
        result = _run_proxy_request(broker, workdir, op="guard")
    assert result["returncode"] == 9
    assert "partial-output" in result["stdout"]


def test_forged_agent_descendant_is_denied(workdir):
    """A proxy whose parent is an unregistered process is denied."""
    _write_scripts(workdir)
    hook_broker.install_public_proxy(workdir / "hook-proxy")
    marker = workdir / "guard-ran.txt"
    operation = _make_operation(
        "guard", [sys.executable, str(workdir / "guard_hook.py"), str(marker)]
    )
    with _start_broker(workdir, [operation]) as broker:
        env = _isolated_env(
            workdir,
            **_identity_env(
                broker,
                TEST_PROXY_PATH=str(workdir / "hook-proxy"),
                TEST_OP="guard",
                TEST_PAYLOAD=PAYLOAD_TEXT,
                TEST_INTERMEDIATE_CODE=INTERMEDIATE_CODE,
                TEST_RESULT_PATH=str(workdir / "forged-result.json"),
            ),
        )
        proc, trigger, _unused = _launch_helper(workdir, "grandchild", env)
        try:
            # The helper itself is registered, but the proxy's parent is
            # the unregistered intermediate process.
            broker.register_native_pid(proc.pid, proc.args)
            trigger.write_text("go")
            proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        forged = json.loads((workdir / "forged-result.json").read_text())
    assert proc.returncode == 0
    assert forged["returncode"] == hook_broker.BROKER_FAILURE_EXIT
    assert "denied" in forged["stderr"]
    assert not marker.exists()
    _no_private_leak(forged["stdout"] + forged["stderr"], workdir)


def test_unregistered_native_parent_is_denied(workdir):
    """Same-uid peers without a live registered parent are denied."""
    _write_scripts(workdir)
    hook_broker.install_public_proxy(workdir / "hook-proxy")
    operation = _make_operation(
        "guard", [sys.executable, str(workdir / "guard_hook.py")]
    )
    with _start_broker(workdir, [operation]) as broker:
        result = _run_proxy_request(
            broker, workdir, op="guard", unregister=True
        )
    assert result["returncode"] == hook_broker.BROKER_FAILURE_EXIT
    assert "denied" in result["stderr"]
    _no_private_leak(result["stdout"] + result["stderr"], workdir)


def _run_raw_request(
    broker: hook_broker.HostHookBroker,
    workdir: Path,
    body: str,
) -> dict:
    """Run one raw-client request as a child of a registered helper."""
    raw_path = workdir / f"raw-{next(_COUNTER)}.py"
    raw_result = workdir / f"raw-result-{next(_COUNTER)}.json"
    raw_path.write_text(_raw_source(body))
    env = _isolated_env(
        workdir,
        **_identity_env(
            broker,
            TEST_RAW_PATH=str(raw_path),
            TEST_RAW_RESULT=str(raw_result),
        ),
    )
    proc, trigger, _unused = _launch_helper(workdir, "raw", env)
    try:
        broker.register_native_pid(proc.pid, proc.args)
        trigger.write_text("go")
        proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 0, f"helper exit {proc.returncode}"
    return json.loads(raw_result.read_text())


def test_unknown_operation_is_denied(workdir):
    """An authorized proxy naming an unconfigured operation is rejected."""
    _write_scripts(workdir)
    hook_broker.install_public_proxy(workdir / "hook-proxy")
    operation = _make_operation(
        "guard", [sys.executable, str(workdir / "guard_hook.py")]
    )
    with _start_broker(workdir, [operation]) as broker:
        result = _run_proxy_request(broker, workdir, op="rtk")
    assert result["returncode"] == hook_broker.BROKER_FAILURE_EXIT
    assert "unknown_operation" in result["stderr"]
    _no_private_leak(result["stdout"] + result["stderr"], workdir)


def test_request_format_violations_are_rejected(workdir):
    """Empty, oversized, mismatched, and privileged requests are rejected.

    Each raw client is a direct child of a registered native helper, so it
    passes authentication and exercises the protocol validation itself.
    """
    _write_scripts(workdir)
    hook_broker.install_public_proxy(workdir / "hook-proxy")
    marker = workdir / "guard-ran.txt"
    operation = _make_operation(
        "guard", [sys.executable, str(workdir / "guard_hook.py"), str(marker)]
    )
    cases = [
        ("empty header", "conn.sendall(struct.pack('>Q', 0))", "invalid_request"),
        (
            "oversized header length",
            "conn.sendall(struct.pack('>Q', " + str(1 << 40) + "))",
            "invalid_request",
        ),
        (
            "bad json header",
            "conn.sendall(struct.pack('>Q', 5))\nconn.sendall(b'not{[')",
            "invalid_request",
        ),
        (
            "version mismatch",
            _request_code(_valid_header(version=2)),
            "invalid_request",
        ),
        (
            "client command string",
            _request_code(
                _valid_header(command=["/bin/sh", "-c", "cat /proc/self/environ"])
            ),
            "invalid_request",
        ),
        (
            "session id format",
            _request_code(_valid_header(session_id="../home/far/.ssh/id")),
            "invalid_request",
        ),
        (
            "envelope env concordance",
            _request_code(
                _valid_header(hook_env_extra={"JCODE_HOOK_SESSION_ID": "session_other"})
            ),
            "invalid_request",
        ),
        (
            "privileged env key",
            _request_code(
                _valid_header(
                    hook_env_extra={
                        "JCODE_ESCALATION_CONTROLLER_STATE_DIR": "/home/far"
                    }
                )
            ),
            "invalid_request",
        ),
        (
            "untrusted hook env key",
            _request_code(
                _valid_header(
                    hook_env_extra={
                        "JCODE_HOOK_UNTRUSTED": "synthetic-untrusted-secret"
                    }
                )
            ),
            "invalid_request",
        ),
        (
            "token state hook env key",
            _request_code(
                _valid_header(
                    hook_env_extra={"JCODE_HOOK_API_TOKEN": "synthetic-token"}
                )
            ),
            "invalid_request",
        ),
        (
            "payload mirror hook env key",
            _request_code(_valid_header(hook_env_extra={"JCODE_HOOK_PAYLOAD": "{}"})),
            "invalid_request",
        ),
        (
            "missing required identity",
            _request_code(_valid_header(hook_env_extra={"JCODE_HOOK_SESSION_ID": ""})),
            "invalid_request",
        ),
        (
            "oversized payload",
            _request_code(
                _valid_header(),
                payload_length=hook_broker.DEFAULT_MAX_PAYLOAD_BYTES + 1,
                payload=b"x",
            ),
            "invalid_request",
        ),
        (
            "untrusted payload root",
            _request_code(_valid_header(hook_env_extra={"JCODE_RUNTIME_DIR": "/var/tmp"})),
            "invalid_request",
        ),
        (
            "unknown operation",
            _request_code(_valid_header(op="controller")),
            "unknown_operation",
        ),
    ]
    with _start_broker(workdir, [operation]) as broker:
        for label, body, expected in cases:
            response = _run_raw_request(broker, workdir, body)
            assert response["ok"] is False, label
            assert response["error"] == expected, (label, response)
            _no_private_leak(json.dumps(response), workdir)
    assert not marker.exists()


def test_operations_cannot_expand_native_env_whitelist(workdir):
    operation = _make_operation(
        "guard", [sys.executable, "-c", "pass"],
        allowed_hook_env=hook_broker.CANONICAL_NATIVE_HOOK_ENV | {"JCODE_HOOK_API_TOKEN"},
    )
    with pytest.raises(ValueError, match="non-native hook key"):
        hook_broker.HostHookBroker(workdir / "broker.sock", [operation])


def test_hook_output_is_bounded_and_byte_exact(workdir):
    _write_scripts(workdir)
    hook_broker.install_public_proxy(workdir / "hook-proxy")
    operation = _make_operation(
        "guard", [sys.executable, "-c",
                  "import sys; sys.stdout.buffer.write(b'x' * 100000); "
                  "sys.stderr.buffer.write(b'y' * 100000)"],
    )
    with _start_broker(workdir, [operation], max_output_bytes=32) as broker:
        result = _run_proxy_request(broker, workdir, op="guard")
    assert result == {"returncode": 0, "stdout": "x" * 32, "stderr": "y" * 32}


def test_hook_failure_and_timeout_are_generic(workdir, monkeypatch):
    """Exec failure, hook timeout, and internal errors never leak details."""
    _write_scripts(workdir)
    hook_broker.install_public_proxy(workdir / "hook-proxy")
    missing = _make_operation("guard", ["/nonexistent-synthetic-hook"])
    slow = _make_operation(
        "slow",
        [sys.executable, str(workdir / "slow_hook.py")],
        timeout=0.5,
    )
    with _start_broker(workdir, [missing, slow]) as broker:
        failed = _run_proxy_request(broker, workdir, op="guard")
        assert failed["returncode"] == hook_broker.BROKER_FAILURE_EXIT
        assert "hook_failed" in failed["stderr"]
        _no_private_leak(failed["stdout"] + failed["stderr"], workdir)

        timed_out = _run_proxy_request(broker, workdir, op="slow")
        assert timed_out["returncode"] == hook_broker.BROKER_FAILURE_EXIT
        assert "timeout" in timed_out["stderr"]
        _no_private_leak(timed_out["stdout"] + timed_out["stderr"], workdir)

        def _explode(*_args, **_kwargs):
            raise RuntimeError("synthetic-boom-detail")

        monkeypatch.setattr(hook_broker, "_run_bounded", _explode)
        internal = _run_proxy_request(broker, workdir, op="guard")
        assert internal["returncode"] == hook_broker.BROKER_FAILURE_EXIT
        assert "broker_error" in internal["stderr"]
        _no_private_leak(internal["stdout"] + internal["stderr"], workdir)
    log_text = (workdir / "private" / "broker.log").read_text()
    assert "synthetic-boom-detail" in log_text
    assert "hook_failed" in log_text


def test_protocol_timeout_closes_idle_connection(workdir):
    """An authenticated client that sends nothing gets a timeout response."""
    _write_scripts(workdir)
    hook_broker.install_public_proxy(workdir / "hook-proxy")
    operation = _make_operation(
        "guard", [sys.executable, str(workdir / "guard_hook.py")]
    )
    started = time.monotonic()
    with _start_broker(workdir, [operation], socket_timeout=0.5) as broker:
        response = _run_raw_request(broker, workdir, "pass")
    assert time.monotonic() - started < 10
    assert response["ok"] is False
    assert response["error"] == "timeout"
    _no_private_leak(json.dumps(response), workdir)


def test_serialized_controller_operations_do_not_overlap(workdir):
    """Concurrent serialized hooks run under one atomic controller lock."""
    _write_scripts(workdir)
    hook_broker.install_public_proxy(workdir / "hook-proxy")
    marker = workdir / "controller-marker.txt"
    operation = _make_operation(
        "controller",
        [sys.executable, str(workdir / "controller_hook.py"), str(marker)],
        serialized=True,
    )
    with _start_broker(workdir, [operation]) as broker:
        env = _isolated_env(
            workdir,
            **_identity_env(
                broker,
                TEST_PROXY_PATH=str(workdir / "hook-proxy"),
                TEST_OP="controller",
                TEST_PAYLOAD=PAYLOAD_TEXT,
            ),
        )
        proc, trigger, result = _launch_helper(workdir, "parallel", env)
        try:
            broker.register_native_pid(proc.pid, proc.args)
            trigger.write_text("go")
            proc.communicate(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        assert proc.returncode == 0, helper_failure(result, proc.returncode)
        record = json.loads(result.read_text())
    assert [entry["returncode"] for entry in record["results"]] == [0, 0]
    lines = marker.read_text().splitlines()
    # Serialized runs can never interleave as enter/enter/exit/exit.
    assert lines == ["enter", "exit", "enter", "exit"]


def test_register_native_pid_validates_command(workdir):
    """Registration is host-side and verifies the live /proc command."""
    broker = hook_broker.HostHookBroker(workdir / "hook-broker.sock", [])
    sleep_argv = [sys.executable, "-c", "import time; time.sleep(30)"]
    proc = subprocess.Popen(sleep_argv, env=_isolated_env(workdir))
    _wait_for_exec(proc)
    try:
        with pytest.raises(ValueError):
            broker.register_native_pid(proc.pid, [sys.executable, "-c", "wrong"])
        with pytest.raises(ValueError):
            broker.register_native_pid(999_999_999, sleep_argv)
        broker.register_native_pid(proc.pid, sleep_argv)
        assert broker.registered_native_pids == {proc.pid}
        broker.unregister_native_pid(proc.pid)
        assert broker.registered_native_pids == set()
    finally:
        proc.kill()
        proc.wait()


def test_broker_lifecycle_restart(workdir):
    """Start/stop clean the socket, and a restarted broker still serves."""
    _write_scripts(workdir)
    hook_broker.install_public_proxy(workdir / "hook-proxy")
    operation = _make_operation(
        "guard", [sys.executable, str(workdir / "guard_hook.py")]
    )
    broker = _start_broker(workdir, [operation])
    assert broker.endpoint.is_socket()
    broker.stop()
    broker.stop()
    assert not broker.endpoint.exists()
    broker.start()
    try:
        result = _run_proxy_request(broker, workdir, op="guard")
    finally:
        broker.stop()
    assert result["returncode"] == 0
    assert result["stdout"].startswith("guard-allowed")
