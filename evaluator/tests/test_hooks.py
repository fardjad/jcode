from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
from pathlib import Path

from jcode_harness_client.hooks import make_hook_wrapper


def install_wrapper(
    root: Path,
    program: str,
) -> tuple[Path, Path]:
    trace = root / "traces"
    wrapper = make_hook_wrapper(
        command=[sys.executable, "-c", program],
        plugin="arbitrary-plugin",
        trace_dir=trace,
        destination=root / "hook-wrapper",
    )
    return wrapper, trace


def invoke(
    wrapper: Path,
    payload: bytes,
    secret: str = "",
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment["JCODE_ACCEPTANCE_API_KEY"] = secret
    environment.update(extra_env or {})
    return subprocess.run(
        [str(wrapper)],
        input=payload,
        capture_output=True,
        env=environment,
        timeout=10,
        check=False,
    )


def test_exact_pass_through_nonzero_exit_and_secret_redaction(tmp_path: Path) -> None:
    program = (
        "import os,sys; data=sys.stdin.buffer.read(); "
        "sys.stdout.buffer.write(b'not-json:'+data); "
        "sys.stderr.buffer.write(os.environ['JCODE_ACCEPTANCE_API_KEY'].encode()); "
        "sys.exit(7)"
    )
    wrapper, trace = install_wrapper(tmp_path, program)
    payload = b'{"session_id":"s","tool_call_id":"c","value":"secret-token"}'
    result = invoke(wrapper, payload, "secret-token")
    assert result.returncode == 7
    assert result.stdout == b"not-json:" + payload
    assert result.stderr == b"secret-token"
    invocation = next(path for path in trace.iterdir() if path.is_dir())
    metadata = json.loads((invocation / "trace.json").read_text(encoding="utf-8"))
    assert metadata["exit_status"] == 7
    assert metadata["context"]["session_id"] == "s"
    assert metadata["context"]["tool_call_id"] == "c"
    assert metadata["output"]["bytes"] == len(result.stdout)
    assert (invocation / "stdout.bin").read_bytes() == b"not-json:" + payload.replace(
        b"secret-token", b"[REDACTED]"
    )
    assert (invocation / "stderr.bin").read_bytes() == b"[REDACTED]"
    assert b"secret-token" not in (invocation / "stdin.bin").read_bytes()


def test_large_binary_payload_has_no_wrapper_timeout_or_truncation(
    tmp_path: Path,
) -> None:
    program = (
        "import sys,time; data=sys.stdin.buffer.read(); time.sleep(0.05); "
        "sys.stdout.buffer.write(data); sys.stderr.buffer.write(b'ok')"
    )
    wrapper, trace = install_wrapper(tmp_path, program)
    payload = bytes(range(256)) * 8192
    result = invoke(wrapper, payload)
    assert result.returncode == 0
    assert result.stdout == payload
    assert result.stderr == b"ok"
    invocation = next(path for path in trace.iterdir() if path.is_dir())
    assert (invocation / "stdout.bin").read_bytes() == payload
    metadata = json.loads((invocation / "trace.json").read_text(encoding="utf-8"))
    assert metadata["input"]["bytes"] == len(payload)
    assert metadata["duration_seconds"] >= 0.05


def test_referenced_payload_is_retained_before_plugin_and_env_context(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    payload_dir = runtime / "jcode-tool-payloads" / "session-s"
    payload_dir.mkdir(parents=True)
    native_payload = payload_dir / "output-1.txt"
    native_payload.write_text("large secret-token payload", encoding="utf-8")
    program = (
        "import os,sys; "
        f"os.unlink({str(native_payload)!r}); "
        "data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)"
    )
    wrapper, trace = install_wrapper(tmp_path / "wrapper-root", program)
    # install_wrapper's parent is created by make_hook_wrapper.
    envelope = json.dumps(
        {
            "event": "post_tool_transform",
            "session_id": "session-s",
            "tool_name": "bash",
            "tool_result_file": str(native_payload),
        }
    ).encode()
    result = invoke(
        wrapper,
        envelope,
        "secret-token",
        {
            "JCODE_RUNTIME_DIR": str(runtime),
            "JCODE_HOOK_SESSION_ID": "session-s",
            "JCODE_HOOK_TOOL_NAME": "bash",
            "JCODE_HOOK_PROCESS_ROLE": "coordinator",
        },
    )
    assert result.returncode == 0
    assert result.stdout == envelope
    assert not native_payload.exists()
    invocation = next(path for path in trace.iterdir() if path.is_dir())
    metadata = json.loads((invocation / "trace.json").read_text(encoding="utf-8"))
    assert metadata["context"]["session_id"] == "session-s"
    assert metadata["context"]["tool_name"] == "bash"
    assert metadata["context"]["process_role"] == "coordinator"
    referenced = metadata["referenced_payloads"][0]
    assert "error" not in referenced
    retained = invocation / referenced["path"]
    assert retained.read_text(encoding="utf-8") == "large [REDACTED] payload"


def test_payload_escape_is_an_explicit_trace_gap(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    wrapper, trace = install_wrapper(
        tmp_path / "wrapper-root",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
    )
    envelope = json.dumps({"tool_result_file": str(outside)}).encode()
    result = invoke(
        wrapper,
        envelope,
        extra_env={"JCODE_RUNTIME_DIR": str(tmp_path / "runtime")},
    )
    assert result.returncode == 0
    invocation = next(path for path in trace.iterdir() if path.is_dir())
    metadata = json.loads((invocation / "trace.json").read_text(encoding="utf-8"))
    assert "escapes" in metadata["referenced_payloads"][0]["error"]
    assert outside.read_text(encoding="utf-8") == "outside"


def test_concurrent_invocations_have_unique_ordered_traces(tmp_path: Path) -> None:
    program = "import sys; data=sys.stdin.buffer.read(); sys.stdout.buffer.write(data)"
    wrapper, trace = install_wrapper(tmp_path, program)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        results = list(
            executor.map(
                lambda index: invoke(wrapper, str(index).encode()),
                range(12),
            )
        )
    assert [result.stdout for result in results] == [
        str(index).encode() for index in range(12)
    ]
    traces = sorted(trace.glob("*/trace.json"))
    assert len(traces) == 12
    sequences = [json.loads(path.read_text())["sequence"] for path in traces]
    assert sorted(sequences) == list(range(1, 13))
    assert len({path.parent.name for path in traces}) == 12


def test_spawn_failure_is_retained(tmp_path: Path) -> None:
    wrapper = make_hook_wrapper(
        command=[str(tmp_path / "missing-executable")],
        plugin="missing-plugin",
        trace_dir=tmp_path / "traces",
        destination=tmp_path / "wrapper",
    )
    result = invoke(wrapper, b"payload")
    assert result.returncode == 127
    assert result.stdout == b""
    invocation = next(path for path in (tmp_path / "traces").iterdir() if path.is_dir())
    metadata = json.loads((invocation / "trace.json").read_text(encoding="utf-8"))
    assert metadata["exit_status"] == 127
    assert metadata["spawn_error"]
