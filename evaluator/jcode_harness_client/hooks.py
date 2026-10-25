#!/usr/bin/env python3
"""Transparent, evaluation-only hook tracing with byte-for-byte pass-through."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SECRET_ENV = "JCODE_ACCEPTANCE_API_KEY"
_WARMUP_ENV = "JCODE_HOOK_WRAPPER_WARMUP"


def _redact(raw: bytes, secrets: list[bytes]) -> bytes:
    for secret in secrets:
        if secret:
            raw = raw.replace(secret, b"[REDACTED]")
    return raw


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".trace-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _next_sequence(trace_dir: Path) -> int:
    """Allocate invocation order across concurrent hook processes."""

    import fcntl

    trace_dir.mkdir(parents=True, exist_ok=True)
    with (trace_dir / "sequence.lock").open("a+b") as lock:
        os.chmod(trace_dir / "sequence.lock", 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = trace_dir / "sequence"
        try:
            value = int(current.read_text()) + 1
        except FileNotFoundError, ValueError:
            value = 1
        current.write_text(f"{value}\n")
        os.chmod(current, 0o600)
        return value


def _context(payload: bytes, secrets: list[bytes]) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except ValueError, UnicodeError:
        value = None
    keys = (
        "session_id",
        "parent_session_id",
        "tool_call_id",
        "call_id",
        "tool_name",
        "name",
        "hook",
        "event",
        "worker_blueprint",
    )
    context = {
        "input_json": isinstance(value, dict),
        **(
            {key: value[key] for key in keys if key in value}
            if isinstance(value, dict)
            else {}
        ),
    }
    environment_keys = {
        "JCODE_HOOK_EVENT": "event",
        "JCODE_HOOK_SESSION_ID": "session_id",
        "JCODE_HOOK_TOOL_CALL_ID": "tool_call_id",
        "JCODE_HOOK_TOOL_NAME": "tool_name",
        "JCODE_HOOK_PROCESS_ROLE": "process_role",
        "JCODE_HOOK_WORKER_ID": "worker_id",
        "JCODE_HOOK_WORKER_BLUEPRINT_ID": "worker_blueprint",
        "JCODE_HOOK_WORKER_BLUEPRINT_NAME": "worker_blueprint_name",
    }
    for environment_key, context_key in environment_keys.items():
        if environment_key in os.environ:
            context[context_key] = _redact(
                os.environ[environment_key].encode(), secrets
            ).decode("utf-8", errors="replace")
    return context


def _retain_referenced_payloads(
    payload: bytes,
    invocation: Path,
    secrets: list[bytes],
) -> list[dict[str, Any]]:
    """Copy large native payload files before their TTL sweep or plugin run."""

    try:
        envelope = json.loads(payload)
    except ValueError, UnicodeError:
        return []
    if not isinstance(envelope, dict):
        return []
    retained: list[dict[str, Any]] = []
    runtime = os.environ.get("JCODE_RUNTIME_DIR")
    expected_root = Path(runtime).resolve() / "jcode-tool-payloads" if runtime else None
    for key in ("tool_input_file", "tool_result_file"):
        value = envelope.get(key)
        if value is None:
            continue
        safe_source = (
            _redact(value.encode(), secrets).decode("utf-8", errors="replace")
            if isinstance(value, str)
            else value
        )
        record: dict[str, Any] = {"field": key, "source": safe_source}
        if not isinstance(value, str) or not value or expected_root is None:
            record["error"] = (
                "invalid payload reference or missing isolated runtime root"
            )
            retained.append(record)
            continue
        candidate = Path(value)
        try:
            if not candidate.is_absolute():
                raise ValueError("payload path is not absolute")
            resolved = candidate.resolve(strict=True)
            if not resolved.is_relative_to(expected_root.resolve()):
                raise ValueError("payload path escapes jcode-tool-payloads")
            descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise ValueError("payload is not a regular file")
                raw = handle.read()
            destination = invocation / "payloads" / f"{key}.bin"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_redact(raw, secrets))
            os.chmod(destination, 0o600)
            record.update(
                path=str(destination.relative_to(invocation)),
                bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
            )
        except (OSError, ValueError) as error:
            record["error"] = _redact(str(error).encode(), secrets).decode(
                "utf-8", errors="replace"
            )
        retained.append(record)
    return retained


def traced_hook_main() -> int:
    """Run the command named by the adjacent JSON sidecar."""

    wrapper = Path(__file__).resolve()
    config_path = wrapper.with_suffix(".json")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        command = config["command"]
        trace_root = Path(config["trace_dir"])
        plugin = str(config["plugin"])
        if not isinstance(command, list) or not all(
            isinstance(part, str) and part for part in command
        ):
            raise ValueError("invalid traced command")
    except OSError, ValueError, KeyError, TypeError:
        print("invalid evaluation hook trace configuration", file=sys.stderr)
        return 127

    payload = sys.stdin.buffer.read()
    if os.environ.get(_WARMUP_ENV) == "1":
        completed = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            env=os.environ.copy(),
            check=False,
        )
        return (
            completed.returncode
            if completed.returncode >= 0
            else 128 + (-completed.returncode)
        )
    sequence = _next_sequence(trace_root)
    invocation = trace_root / f"{sequence:012d}-{uuid.uuid4().hex}"
    invocation.mkdir(mode=0o700)
    secrets = [os.environ.get(_SECRET_ENV, "").encode()]
    started = datetime.now(UTC).isoformat()
    monotonic = time.monotonic()
    metadata: dict[str, Any] = {
        "status": "running",
        "sequence": sequence,
        "plugin": plugin,
        "production_command": command,
        "started_at": started,
        "context": _context(_redact(payload, secrets), secrets),
        "input": {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "path": "stdin.bin",
        },
    }
    (invocation / "stdin.bin").write_bytes(_redact(payload, secrets))
    os.chmod(invocation / "stdin.bin", 0o600)
    _atomic_json(invocation / "trace.json", metadata)
    metadata["referenced_payloads"] = _retain_referenced_payloads(
        payload, invocation, secrets
    )
    _atomic_json(invocation / "trace.json", metadata)
    try:
        completed = subprocess.run(
            command,
            input=payload,
            capture_output=True,
            env=os.environ.copy(),
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        returncode = completed.returncode
        spawn_error = None
    except OSError as error:
        stdout = b""
        stderr = f"hook execution failed: {error.strerror or 'spawn error'}\n".encode()
        returncode = 127
        spawn_error = error.strerror or "spawn error"
    duration = time.monotonic() - monotonic
    # Only retained traces are redacted. The production byte stream is untouched.
    (invocation / "stdout.bin").write_bytes(_redact(stdout, secrets))
    (invocation / "stderr.bin").write_bytes(_redact(stderr, secrets))
    for name in ("stdout.bin", "stderr.bin"):
        os.chmod(invocation / name, 0o600)
    metadata.update(
        status="completed",
        duration_seconds=duration,
        exit_status=returncode,
        spawn_error=spawn_error,
        output={
            "bytes": len(stdout),
            "sha256": hashlib.sha256(stdout).hexdigest(),
            "path": "stdout.bin",
        },
        error={
            "bytes": len(stderr),
            "sha256": hashlib.sha256(stderr).hexdigest(),
            "path": "stderr.bin",
        },
    )
    _atomic_json(invocation / "trace.json", metadata)
    sys.stdout.buffer.write(stdout)
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(stderr)
    sys.stderr.buffer.flush()
    return returncode if returncode >= 0 else 128 + (-returncode)


def make_hook_wrapper(
    *,
    command: list[str],
    plugin: str,
    trace_dir: Path,
    destination: Path,
) -> Path:
    """Install one standalone traced command without modifying the plugin."""

    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("hook command must be a nonempty argv list")
    if destination.exists() or destination.with_suffix(".json").exists():
        raise ValueError("hook wrapper destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).read_text(encoding="utf-8")
    if source.startswith("#!"):
        source = "#!" + sys.executable + source[source.index("\n") :]
    destination.write_text(source, encoding="utf-8")
    destination.chmod(0o700)
    _atomic_json(
        destination.with_suffix(".json"),
        {
            "command": command,
            "plugin": plugin,
            "trace_dir": str(trace_dir.resolve()),
        },
    )
    return destination


def warm_hook_wrapper(wrapper: Path) -> None:
    """Run one untraced no-op through the wrapper to warm OS/Python caches."""

    environment = os.environ.copy()
    environment[_WARMUP_ENV] = "1"
    subprocess.run(
        [str(wrapper)],
        input=b"{}",
        capture_output=True,
        env=environment,
        timeout=10,
        check=False,
    )


if __name__ == "__main__":
    raise SystemExit(traced_hook_main())
