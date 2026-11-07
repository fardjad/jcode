"""Host-side hook broker and stateless public proxy for blind sandbox runs.

Evaluator hook commands (the delegation guard, the escalation controller,
and the RTK tracing wrapper) must run on the host with private evaluator
environment and state, but the callers that need their verdicts live inside
the blind bwrap sandbox: a fresh PID namespace with no private host mount
and a scrubbed environment. This module provides the bridge.

Interface
=========

``HostHookBroker`` owns a host-side ``AF_UNIX`` stream socket:

* ``endpoint`` must be created under the public staging runtime directory
  (``<root>/runtime`` from :mod:`jcode_harness_client.blind_runtime`), which
  is bind-mounted into the sandbox at its identical absolute path. The
  sandboxed proxy connects through that public mount; no private host path,
  ``/run`` socket, or extra mount is ever exposed.
* ``operations`` map fixed operation names (``rtk``, ``controller``,
  ``guard``) to host-configured hook commands. The request chooses only the
  operation name; client command strings, source files, and state paths are
  not part of the protocol.
* ``register_native_pid(pid, command)`` is the only registration API and is
  host-side only (never reachable through the socket). The host runner calls
  it after launching a trusted native child inside the sandbox, recording
  the child's host PID and exact launch argv; the broker re-verifies both
  against ``/proc`` before accepting the registration.
* ``start()``/``stop()`` manage the listener and one server thread per
  request. Operations marked ``serialized`` share one atomic controller
  lock so controller hooks keep single-writer semantics; the broker never
  touches controller state itself.

Authentication per connection, in this order:

1. ``SO_PEERCRED`` gives the connecting process's host PID and UID. The UID
   must match the broker's own (same host owner) but is never sufficient
   alone.
2. ``/proc/<peer pid>/status`` ``PPid`` must equal one registered native
   host PID, and ``/proc/<ppid>/cmdline`` must still equal the command
   recorded at registration (defends against PID reuse and stale entries).
   Only ``/proc`` status and cmdline are read: no ``/proc/<pid>/exe`` or
   namespace symlink probes that would require ptrace permissions. A plain
   agent bash grandchild has an unregistered parent and is denied; a dead
   native parent cannot be reparented onto an arbitrary daemon inside the
   sandbox because the PID namespace reaper is PID 1 (bwrap), not the
   broker.

Request framing (one request per connection, big-endian ``>Q`` lengths):

* an 8-byte length plus a UTF-8 JSON header
  ``{"version", "op", "session_id", "call_id", "hook_env"}`` and
* an 8-byte length plus the original raw hook payload bytes (proxy stdin).

The broker validates the header strictly: exact key set, protocol version,
a known operation, native session/call ID format, ``hook_env`` restricted
to the canonical curated native hook context keys
(:data:`CANONICAL_NATIVE_HOOK_ENV`, or the operation's narrower configured
subset) with nonempty bounded values, and
envelope/env concordance (the ``JCODE_HOOK_SESSION_ID`` and
``JCODE_HOOK_TOOL_CALL_ID`` values must equal the header fields). Zero-size,
oversized, or unreadable frames are rejected. The hook command runs with a
fully reconstructed environment: the operation's private host-configured
``env`` (state path, source path, secrets) plus the validated identity env.
Nothing is inherited from the broker's own environment.

The response is one length-prefixed JSON frame. Success carries the hook's
exit status and raw stdout/stderr (base64, read size bounded per stream) so
guard and controller verdicts reach the agent byte-exact rather than as an
LLM summary. Every rejection or internal failure carries a generic error
code and message only: private paths, environment values, and stack traces
never cross to the sandbox; details are logged host-side to the private log
path instead.

The public proxy is a stateless stdlib-only script installed into the
staged public ``bin`` tree via :func:`install_public_proxy`. Its argv is
``<proxy> <operation>``; it reads the original hook payload from stdin, and
its environment carries the native hook context plus
``JCODE_HOOK_BROKER_ENDPOINT`` pointing at the public runtime endpoint path.
It forwards ONLY the explicit canonical whitelist entries (the exact same
key set as :data:`CANONICAL_NATIVE_HOOK_ENV`), never the whole
``JCODE_HOOK_*`` prefix: unknown, future, or untrusted keys in its
environment are filtered out before the request is built. It prints the
hook's stdout/stderr and exits with the hook's exit status; broker-side
failures exit 70 with a generic message. Observer hooks (post_tool,
turn_end, ...) are spawned with null stdin: with no payload to forward, the
proxy sends a neutral empty JSON object so the strictly nonempty payload
frame stays valid and payload-parsing host hooks see valid JSON instead of
a closed stdin.

Current staging integration: none yet. The runner will, in order: stage the
sandbox, start the broker bound at ``<root>/runtime/hook-broker.sock`` with
the configured private operations, install the proxy into ``<root>/bin``,
launch the blind sandbox, register each trusted native child PID and argv,
and pass the endpoint variable in the sandbox environment. The broker
launches nothing itself and performs no runner integration here.
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import struct
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "BROKER_FAILURE_EXIT",
    "DEFAULT_ALLOWED_HOOK_ENV",
    "DEFAULT_MAX_HEADER_BYTES",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "DEFAULT_MAX_PAYLOAD_BYTES",
    "PROXY_ENDPOINT_ENV",
    "PROTOCOL_VERSION",
    "BrokerOperation",
    "CANONICAL_NATIVE_HOOK_ENV",
    "HostHookBroker",
    "install_public_proxy",
    "public_proxy_source",
]

PROTOCOL_VERSION = 1

PROXY_ENDPOINT_ENV = "JCODE_HOOK_BROKER_ENDPOINT"
BROKER_FAILURE_EXIT = 70

DEFAULT_MAX_HEADER_BYTES = 64 * 1024
DEFAULT_MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
# Per-value bound matches the native JCODE_HOOK_TOOL_INPUT truncation limit
# (crates/jcode-base/src/hooks.rs TOOL_INPUT_ENV_LIMIT, 16 KiB), the largest
# curated value a real native hook environment carries.
DEFAULT_HOOK_ENV_VALUE_MAX = 16 * 1024
DEFAULT_HOOK_ENV_ENTRIES_MAX = 32
DEFAULT_SOCKET_TIMEOUT = 30.0
DEFAULT_HOOK_TIMEOUT = 120.0

REQUIRED_HOOK_ENV = ("JCODE_HOOK_SESSION_ID", "JCODE_HOOK_TOOL_CALL_ID")

# The canonical native hook context a sandboxed proxy may forward and the
# broker accepts by default. Curated from the actual native hook environment
# (crates/jcode-base/src/hooks.rs ``apply_event_env`` plus its event fields):
# identity (event, session, tool call, tool name, swarm role, worker, and
# blueprint), tool metadata (tool input, status, duration, output size,
# error), and the session working directory. The native ``MODEL`` field is a
# plain model identifier, never a credential or endpoint, so it is safe.
# Everything else is excluded and stays fail-closed: the JSON payload mirror
# (``JCODE_HOOK_PAYLOAD`` travels as the framed stdin payload, never as
# env), the broker endpoint variable, token/credential/endpoint state, and
# any unknown or future ``JCODE_HOOK_*`` key. An operation may still
# restrict below this set, never above it.
CANONICAL_NATIVE_HOOK_ENV = frozenset(
    {
        "JCODE_HOOK_CWD",
        "JCODE_HOOK_DURATION_MS",
        "JCODE_HOOK_ERROR",
        "JCODE_HOOK_EVENT",
        "JCODE_HOOK_MODEL",
        "JCODE_HOOK_OUTPUT_BYTES",
        "JCODE_HOOK_PROCESS_ROLE",
        "JCODE_HOOK_SESSION_ID",
        "JCODE_HOOK_STATUS",
        "JCODE_HOOK_SWARM_ENABLED",
        "JCODE_HOOK_SWARM_MODE",
        "JCODE_HOOK_TOOL_CALL_ID",
        "JCODE_HOOK_TOOL_INPUT",
        "JCODE_HOOK_TOOL_NAME",
        "JCODE_HOOK_WORKER_BLUEPRINT_ID",
        "JCODE_HOOK_WORKER_BLUEPRINT_NAME",
        "JCODE_HOOK_WORKER_ID",
    }
)

# The native hook context keys a request may carry by default. Private
# evaluator variables (state paths, source paths, secrets, and guard
# selector/threshold settings) are host-configured on the operation and can
# never be supplied or replaced by a request.
DEFAULT_ALLOWED_HOOK_ENV = CANONICAL_NATIVE_HOOK_ENV

ERROR_DENIED = "denied"
ERROR_INVALID = "invalid_request"
ERROR_UNKNOWN_OPERATION = "unknown_operation"
ERROR_TIMEOUT = "timeout"
ERROR_HOOK_FAILED = "hook_failed"
ERROR_INTERNAL = "broker_error"

_OP_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

_LENGTH = struct.Struct(">Q")
_CRED = struct.Struct("3i")
_LISTEN_BACKLOG = 16
_READ_CHUNK = 65536

_HEADER_KEYS = frozenset({"version", "op", "session_id", "call_id", "hook_env"})


@dataclass(frozen=True)
class BrokerOperation:
    """One host-configured hook command exposed as a fixed operation."""

    name: str
    command: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    required_hook_env: frozenset[str] = frozenset()
    allowed_hook_env: frozenset[str] = DEFAULT_ALLOWED_HOOK_ENV
    timeout: float = DEFAULT_HOOK_TIMEOUT
    serialized: bool = False
    cwd: str | None = None


class _RequestError(Exception):
    """A rejected request with a stable error code and a private detail."""

    def __init__(self, error: str, detail: str) -> None:
        super().__init__(error)
        self.error = error
        self.detail = detail


def _proc_cmdline(pid: int) -> tuple[str, ...] | None:
    """Return the exact argv of ``pid`` from ``/proc``, or None."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    parts = raw.split(b"\x00")
    return tuple(part.decode("utf-8", "surrogateescape") for part in parts if part)


def _proc_ppid(pid: int) -> int | None:
    """Return the PPid of ``pid`` from ``/proc/<pid>/status``, or None."""
    try:
        status = Path(f"/proc/{pid}/status").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("PPid:"):
            fields = line.split()
            if len(fields) < 2:
                return None
            try:
                return int(fields[1])
            except ValueError:
                return None
    return None


def _recv_exactly(conn: socket.socket, count: int) -> bytes | None:
    """Read exactly ``count`` bytes, or None when the peer closes early."""
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = conn.recv(min(_READ_CHUNK, remaining))
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_frame(conn: socket.socket, limit: int) -> bytes | None:
    """Read one length-prefixed frame, or None on a framing violation."""
    raw = _recv_exactly(conn, _LENGTH.size)
    if raw is None:
        return None
    length = _LENGTH.unpack(raw)[0]
    if length == 0 or length > limit:
        return None
    return _recv_exactly(conn, length)


def _exit_code(returncode: int | None) -> int:
    """Normalize a subprocess return code into a 0..255 shell status."""
    if returncode is None:
        return BROKER_FAILURE_EXIT
    if returncode < 0:
        return 128 + (-returncode)
    return max(0, min(255, returncode))


def _run_bounded(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str | None,
    input_bytes: bytes,
    timeout: float,
    max_bytes: int,
) -> dict[str, Any]:
    """Run one hook command with bounded stdin/stdout/stderr handling.

    The command receives a fully reconstructed environment and the raw
    payload on stdin. Output streams are read with a hard per-stream byte
    cap (the remainder is drained and dropped) so the response can never
    exceed the configured bound, while the hook itself is not blocked.
    """
    result: dict[str, Any] = {
        "returncode": None,
        "stdout": b"",
        "stderr": b"",
        "truncated": False,
        "exec_error": False,
        "timed_out": False,
    }
    try:
        process = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            cwd=cwd,
        )
    except OSError:
        result["exec_error"] = True
        return result
    outputs: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    truncated: dict[str, bool] = {"stdout": False, "stderr": False}

    def _read(stream: Any, key: str) -> None:
        total = 0
        while True:
            try:
                chunk = os.read(stream.fileno(), _READ_CHUNK)
            except OSError, ValueError:
                return
            if not chunk:
                return
            room = max_bytes - total
            if room > 0:
                outputs[key].append(chunk[:room])
                if len(chunk) > room:
                    truncated[key] = True
            else:
                truncated[key] = True
            total += len(chunk)

    def _write() -> None:
        try:
            process.stdin.write(input_bytes)  # type: ignore[union-attr]
            process.stdin.flush()  # type: ignore[union-attr]
        except OSError, ValueError:
            pass
        finally:
            try:
                process.stdin.close()  # type: ignore[union-attr]
            except OSError, ValueError:
                pass

    threads = [
        threading.Thread(target=_write, daemon=True),
        threading.Thread(target=_read, args=(process.stdout, "stdout"), daemon=True),
        threading.Thread(target=_read, args=(process.stderr, "stderr"), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        result["returncode"] = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        result["returncode"] = process.wait()
        result["timed_out"] = True
    for thread in threads:
        thread.join(timeout=5)
    result["stdout"] = b"".join(outputs["stdout"])
    result["stderr"] = b"".join(outputs["stderr"])
    result["truncated"] = truncated["stdout"] or truncated["stderr"]
    return result


class HostHookBroker:
    """Host-side Unix socket broker executing private configured hooks.

    The broker runs entirely on the host. Its endpoint must live under the
    public staging runtime directory so sandboxed proxies reach it through
    the existing bind mount. See the module docstring for the protocol and
    authentication contract.
    """

    def __init__(
        self,
        endpoint: Path | str,
        operations: Mapping[str, BrokerOperation] | Iterable[BrokerOperation],
        *,
        log_path: Path | str | None = None,
        controller_lock: threading.Lock | None = None,
        max_header_bytes: int = DEFAULT_MAX_HEADER_BYTES,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        socket_timeout: float = DEFAULT_SOCKET_TIMEOUT,
    ) -> None:
        if isinstance(operations, Mapping):
            items = list(operations.items())
        else:
            items = [(operation.name, operation) for operation in operations]
        if len(items) != len({name for name, _ in items}):
            raise ValueError("operation names must be unique")
        validated: dict[str, BrokerOperation] = {}
        for name, operation in items:
            if not isinstance(operation, BrokerOperation):
                raise ValueError(f"operation {name!r} is not a BrokerOperation")
            if not _OP_NAME_PATTERN.fullmatch(name):
                raise ValueError(f"invalid operation name: {name!r}")
            if not operation.command or any(
                not isinstance(part, str) or not part for part in operation.command
            ):
                raise ValueError(f"operation {name!r} needs a nonempty argv command")
            if operation.timeout <= 0:
                raise ValueError(f"operation {name!r} needs a positive timeout")
            for key, value in operation.env.items():
                if not isinstance(key, str) or not key or not isinstance(value, str):
                    raise ValueError(f"operation {name!r} has invalid env entry")
                if key.startswith("JCODE_HOOK_") or key in operation.allowed_hook_env:
                    raise ValueError("private env may not hold hook identity: " + key)
            for key in operation.allowed_hook_env:
                if not isinstance(key, str) or not key.startswith("JCODE_HOOK_"):
                    raise ValueError(f"operation {name!r} allows non-hook key: {key}")
            missing = operation.required_hook_env - operation.allowed_hook_env
            if missing:
                raise ValueError(
                    "operation requires unallowed hook env: " + repr(sorted(missing))
                )
            validated[name] = operation
        self._operations = validated
        self._endpoint = Path(str(endpoint))
        self._log_path = Path(str(log_path)) if log_path is not None else None
        self._controller_lock = (
            controller_lock if controller_lock is not None else threading.Lock()
        )
        self._max_header_bytes = int(max_header_bytes)
        self._max_payload_bytes = int(max_payload_bytes)
        self._max_output_bytes = int(max_output_bytes)
        self._socket_timeout = float(socket_timeout)
        self._registrations: dict[int, tuple[str, ...]] = {}
        self._registration_lock = threading.Lock()
        self._connections: dict[socket.socket, threading.Thread] = {}
        self._connection_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stopping = False

    # -- public surface ---------------------------------------------------

    @property
    def endpoint(self) -> Path:
        """The broker's Unix socket path (public staging runtime location)."""
        return self._endpoint

    @property
    def operations(self) -> tuple[str, ...]:
        return tuple(sorted(self._operations))

    @property
    def max_payload_bytes(self) -> int:
        return self._max_payload_bytes

    @property
    def registered_native_pids(self) -> frozenset[int]:
        with self._registration_lock:
            return frozenset(self._registrations)

    def register_native_pid(self, pid: int, command: Sequence[str]) -> None:
        """Record a trusted native child PID and its exact launch argv.

        Host-side only: never reachable through the broker socket. The PID
        must be running and its ``/proc`` cmdline must equal ``command``
        (the runner's own launch argv, never agent-supplied data), so stale
        or forged registrations fail loudly at registration time.
        """
        argv = tuple(str(part) for part in command)
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValueError(f"invalid native pid: {pid!r}")
        if not argv or any(not part for part in argv):
            raise ValueError("native command must be a nonempty argv sequence")
        actual = _proc_cmdline(pid)
        if actual is None:
            raise ValueError(f"native pid is not running: {pid}")
        if actual != argv:
            raise ValueError(f"native pid command mismatch for {pid}")
        with self._registration_lock:
            self._registrations[pid] = argv
        self._log("registered", pid=pid, command=list(argv))

    def unregister_native_pid(self, pid: int) -> None:
        with self._registration_lock:
            self._registrations.pop(pid, None)
        self._log("unregistered", pid=pid)

    def start(self) -> HostHookBroker:
        """Bind the endpoint and start accepting connections."""
        if self._accept_thread is not None:
            return self
        if self._endpoint.exists():
            raise FileExistsError(f"broker endpoint already exists: {self._endpoint}")
        self._endpoint.parent.mkdir(parents=True, exist_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self._endpoint))
            os.chmod(self._endpoint, 0o600)
            listener.listen(_LISTEN_BACKLOG)
        except BaseException:
            listener.close()
            raise
        self._listener = listener
        self._stopping = False
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="hook-broker-accept",
            daemon=True,
        )
        self._accept_thread.start()
        self._log("listening", endpoint=str(self._endpoint), operations=self.operations)
        return self

    def stop(self) -> None:
        """Close the listener, drain request threads, remove the socket."""
        if self._accept_thread is None:
            return
        self._stopping = True
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.close()
        accept_thread, self._accept_thread = self._accept_thread, None
        if accept_thread is not None:
            accept_thread.join(timeout=5)
        with self._connection_lock:
            connections = dict(self._connections)
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        for thread in connections.values():
            thread.join(timeout=2)
        if self._endpoint.is_socket():
            self._endpoint.unlink()
        self._log("stopped", endpoint=str(self._endpoint))

    def __enter__(self) -> HostHookBroker:
        return self.start()

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- internals --------------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stopping:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                break
            thread = threading.Thread(
                target=self._serve,
                args=(conn,),
                name="hook-broker-request",
                daemon=True,
            )
            with self._connection_lock:
                self._connections[conn] = thread
            thread.start()

    def _peer_parent_registered(self, peer_pid: int) -> bool:
        """True when the peer's parent is a live registered native process."""
        ppid = _proc_ppid(peer_pid)
        if ppid is None or ppid <= 0:
            return False
        with self._registration_lock:
            expected = self._registrations.get(ppid)
        if expected is None:
            return False
        actual = _proc_cmdline(ppid)
        if actual is None or actual != expected:
            # PID reuse or a replaced process: the registration is stale.
            return False
        return True

    def _validate_request(
        self, header_bytes: bytes
    ) -> tuple[BrokerOperation, dict[str, str]]:
        try:
            header = json.loads(header_bytes.decode("utf-8"))
        except (ValueError, UnicodeError) as error:
            raise _RequestError(ERROR_INVALID, f"json: {error}") from None
        if not isinstance(header, dict):
            raise _RequestError(ERROR_INVALID, "header is not an object")
        if set(header) != _HEADER_KEYS:
            raise _RequestError(
                ERROR_INVALID, "keys: " + repr(sorted(set(header) ^ _HEADER_KEYS))
            )
        if header["version"] != PROTOCOL_VERSION:
            raise _RequestError(ERROR_INVALID, "version")
        name = header["op"]
        if not isinstance(name, str) or name not in self._operations:
            raise _RequestError(ERROR_UNKNOWN_OPERATION, "op")
        operation = self._operations[name]
        session_id = header["session_id"]
        call_id = header["call_id"]
        for value in (session_id, call_id):
            if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
                raise _RequestError(ERROR_INVALID, "id format")
        hook_env = header["hook_env"]
        if not isinstance(hook_env, dict):
            raise _RequestError(ERROR_INVALID, "hook_env is not an object")
        if len(hook_env) > DEFAULT_HOOK_ENV_ENTRIES_MAX:
            raise _RequestError(ERROR_INVALID, "hook_env too large")
        for key, value in hook_env.items():
            if key not in operation.allowed_hook_env:
                raise _RequestError(ERROR_INVALID, "hook_env key: " + repr(key))
            if not isinstance(value, str) or not value:
                raise _RequestError(ERROR_INVALID, "hook_env value: " + repr(key))
            if len(value.encode("utf-8")) > DEFAULT_HOOK_ENV_VALUE_MAX:
                raise _RequestError(ERROR_INVALID, "hook_env value size: " + repr(key))
        for required in REQUIRED_HOOK_ENV:
            if required not in hook_env:
                raise _RequestError(
                    ERROR_INVALID, "missing hook_env: " + repr(required)
                )
        if hook_env["JCODE_HOOK_SESSION_ID"] != session_id:
            raise _RequestError(ERROR_INVALID, "session concordance")
        if hook_env["JCODE_HOOK_TOOL_CALL_ID"] != call_id:
            raise _RequestError(ERROR_INVALID, "call concordance")
        missing = set(operation.required_hook_env) - set(hook_env)
        if missing:
            raise _RequestError(
                ERROR_INVALID, "missing required hook_env: " + repr(sorted(missing))
            )
        return operation, dict(hook_env)

    def _serve(self, conn: socket.socket) -> None:
        peer_pid: int | None = None
        try:
            conn.settimeout(self._socket_timeout)
            creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _CRED.size)
            peer_pid, peer_uid, _gid = _CRED.unpack(creds)
            if peer_uid != os.geteuid() or peer_pid <= 0:
                self._log("denied", peer_pid=peer_pid, peer_uid=peer_uid, reason="uid")
                self._send(conn, _failure(ERROR_DENIED, "request denied"))
                return
            if not self._peer_parent_registered(peer_pid):
                self._log("denied", peer_pid=peer_pid, reason="parent")
                self._send(conn, _failure(ERROR_DENIED, "request denied"))
                return
            header_bytes = _read_frame(conn, self._max_header_bytes)
            if header_bytes is None:
                self._log("invalid", peer_pid=peer_pid, reason="header frame")
                self._send(conn, _failure(ERROR_INVALID, "invalid request"))
                return
            try:
                operation, hook_env = self._validate_request(header_bytes)
            except _RequestError as error:
                self._log(
                    "invalid",
                    peer_pid=peer_pid,
                    reason=error.error,
                    detail=error.detail,
                )
                self._send(conn, _failure(error.error, "invalid request"))
                return
            payload = _read_frame(conn, self._max_payload_bytes)
            if payload is None:
                self._log(
                    "invalid", peer_pid=peer_pid, op=operation.name, reason="payload"
                )
                self._send(conn, _failure(ERROR_INVALID, "invalid request"))
                return
            env: dict[str, str] = dict(operation.env)
            env.update(hook_env)
            lock = self._controller_lock if operation.serialized else None
            if lock is not None:
                lock.acquire()
            try:
                self._log("hook_start", peer_pid=peer_pid, op=operation.name)
                result = _run_bounded(
                    operation.command,
                    env=env,
                    cwd=operation.cwd,
                    input_bytes=payload,
                    timeout=operation.timeout,
                    max_bytes=self._max_output_bytes,
                )
            finally:
                if lock is not None:
                    lock.release()
            if result["timed_out"]:
                self._log("timeout", peer_pid=peer_pid, op=operation.name)
                self._send(conn, _failure(ERROR_TIMEOUT, "the hook command timed out"))
                return
            if result["exec_error"]:
                self._log("hook_failed", peer_pid=peer_pid, op=operation.name)
                self._send(
                    conn,
                    _failure(ERROR_HOOK_FAILED, "the hook command could not be run"),
                )
                return
            exit_code = _exit_code(result["returncode"])
            self._log(
                "hook_end",
                peer_pid=peer_pid,
                op=operation.name,
                exit_code=exit_code,
                truncated=result["truncated"],
            )
            self._send(
                conn,
                {
                    "ok": True,
                    "exit_code": exit_code,
                    "stdout": base64.b64encode(result["stdout"]).decode("ascii"),
                    "stderr": base64.b64encode(result["stderr"]).decode("ascii"),
                    "truncated": result["truncated"],
                },
            )
        except TimeoutError:
            self._log("timeout", peer_pid=peer_pid)
            self._send(conn, _failure(ERROR_TIMEOUT, "request timed out"))
        except Exception as error:
            self._log("error", peer_pid=peer_pid, detail=repr(error)[:500])
            self._send(conn, _failure(ERROR_INTERNAL, "internal broker error"))
        finally:
            with self._connection_lock:
                self._connections.pop(conn, None)
            try:
                conn.close()
            except OSError:
                pass

    def _send(self, conn: socket.socket, payload: Mapping[str, Any]) -> None:
        try:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            conn.sendall(_LENGTH.pack(len(body)) + body)
        except OSError:
            pass

    def _log(self, event: str, **fields: Any) -> None:
        """Append one host-private JSON log line; never reply with details."""
        if self._log_path is None:
            return
        record = {"time": round(time.time(), 3), "event": event, **fields}
        line = json.dumps(record, sort_keys=True, default=str) + "\n"
        with self._log_lock:
            fresh = not self._log_path.exists()
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
            if fresh:
                self._log_path.parent.chmod(0o700)
                self._log_path.chmod(0o600)


def _failure(error: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "message": message}


# The standalone stdlib proxy installed into the staged public bin tree.
# Kept in sync with the framing and limits above; it must stay importable
# with nothing but the Python standard library inside the sandbox.
PROXY_SOURCE = '''#!/usr/bin/env python3
"""Stateless public proxy for the host hook broker.

argv: hook-proxy <operation>. Reads the original hook payload from stdin,
forwards it with ONLY the canonical whitelisted JCODE_HOOK_* context
entries (unknown keys in the environment are filtered out, never
forwarded), prints the hook's stdout and stderr, and exits with the hook's
exit status.
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import sys

ENDPOINT_ENV = "JCODE_HOOK_BROKER_ENDPOINT"
PROTOCOL_VERSION = 1
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
BROKER_FAILURE_EXIT = 70
_LENGTH = struct.Struct(">Q")

# The explicit canonical native hook context this proxy may forward. Kept
# in sync with jcode_harness_client.hook_broker.CANONICAL_NATIVE_HOOK_ENV:
# identity, tool metadata, cwd, and the safe model identifier. Everything
# else (JCODE_HOOK_PAYLOAD, the endpoint variable, unknown or untrusted
# JCODE_HOOK_* keys) is filtered out before the request is built.
ALLOWED_HOOK_ENV = frozenset(
    {
        "JCODE_HOOK_CWD",
        "JCODE_HOOK_DURATION_MS",
        "JCODE_HOOK_ERROR",
        "JCODE_HOOK_EVENT",
        "JCODE_HOOK_MODEL",
        "JCODE_HOOK_OUTPUT_BYTES",
        "JCODE_HOOK_PROCESS_ROLE",
        "JCODE_HOOK_SESSION_ID",
        "JCODE_HOOK_STATUS",
        "JCODE_HOOK_SWARM_ENABLED",
        "JCODE_HOOK_SWARM_MODE",
        "JCODE_HOOK_TOOL_CALL_ID",
        "JCODE_HOOK_TOOL_INPUT",
        "JCODE_HOOK_TOOL_NAME",
        "JCODE_HOOK_WORKER_BLUEPRINT_ID",
        "JCODE_HOOK_WORKER_BLUEPRINT_NAME",
        "JCODE_HOOK_WORKER_ID",
    }
)


def fail(message):
    sys.stderr.write("hook-proxy: " + message + "\\n")
    raise SystemExit(BROKER_FAILURE_EXIT)


def read_frame(conn, limit):
    header = b""
    while len(header) < _LENGTH.size:
        chunk = conn.recv(_LENGTH.size - len(header))
        if not chunk:
            return None
        header += chunk
    length = _LENGTH.unpack(header)[0]
    if length == 0 or length > limit:
        return None
    body = b""
    while len(body) < length:
        chunk = conn.recv(min(65536, length - len(body)))
        if not chunk:
            return None
        body += chunk
    return body


def main():
    if len(sys.argv) != 2 or not sys.argv[1]:
        fail("usage: hook-proxy <operation>")
    operation = sys.argv[1]
    endpoint = os.environ.get(ENDPOINT_ENV)
    if not endpoint:
        fail("broker endpoint is not configured")
    payload = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
    if len(payload) > MAX_PAYLOAD_BYTES:
        fail("hook payload is too large")
    if not payload:
        # Observer hooks (post_tool, turn_end, ...) are spawned with null
        # stdin: there is no payload to forward. The protocol requires a
        # nonempty frame, so send a neutral empty JSON object; host hooks
        # that parse the payload see valid JSON instead of a closed stdin.
        payload = b"{}"
    hook_env = {}
    for key, value in os.environ.items():
        if key in ALLOWED_HOOK_ENV and value:
            hook_env[key] = value
    header = {
        "version": PROTOCOL_VERSION,
        "op": operation,
        "session_id": hook_env.get("JCODE_HOOK_SESSION_ID", ""),
        "call_id": hook_env.get("JCODE_HOOK_TOOL_CALL_ID", ""),
        "hook_env": hook_env,
    }
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(600.0)
    try:
        conn.connect(endpoint)
        body = json.dumps(header).encode("utf-8")
        conn.sendall(_LENGTH.pack(len(body)))
        conn.sendall(body)
        conn.sendall(_LENGTH.pack(len(payload)))
        if payload:
            conn.sendall(payload)
        response = read_frame(conn, MAX_RESPONSE_BYTES)
    except OSError as error:
        fail("broker communication failed: " + type(error).__name__)
    finally:
        conn.close()
    if response is None:
        fail("broker response was empty or oversized")
    try:
        result = json.loads(response.decode("utf-8"))
    except (ValueError, UnicodeError):
        fail("broker response was not valid JSON")
    if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
        fail("broker response was malformed")
    if not result["ok"]:
        fail("request rejected: " + str(result.get("error", "unknown")))
    sys.stdout.buffer.write(base64.b64decode(result.get("stdout", "")))
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write(base64.b64decode(result.get("stderr", "")))
    sys.stderr.buffer.flush()
    code = result.get("exit_code", BROKER_FAILURE_EXIT)
    if not isinstance(code, int) or not 0 <= code <= 255:
        code = BROKER_FAILURE_EXIT
    raise SystemExit(code)


if __name__ == "__main__":
    main()
'''


def public_proxy_source() -> str:
    """Return the standalone stdlib proxy script source."""
    return PROXY_SOURCE


def install_public_proxy(destination: Path | str) -> Path:
    """Install the stateless public proxy as an executable script.

    The destination belongs in the staged public ``bin`` tree so it is the
    only broker-facing artifact the sandbox ever sees.
    """
    path = Path(str(destination))
    if path.exists():
        raise ValueError(f"proxy destination already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(PROXY_SOURCE, encoding="utf-8")
    path.chmod(0o755)
    return path
