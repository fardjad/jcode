"""Thread-safe synchronous client for jcode's versioned NDJSON harness API."""

from __future__ import annotations

import json
import math
import os
import queue
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_API_MAJOR = 1
_KNOWN_EVENTS = frozenset(
    {
        "hello_ok",
        "ok",
        "sessions",
        "tools",
        "error",
        "attached",
        "message_accepted",
        "text_delta",
        "text_done",
        "text_replace",
        "turn_stopped",
        "turn_done",
        "tool_start",
        "tool_input_delta",
        "tool_exec",
        "tool_done",
    }
)
_DEFAULT_TURN_TIMEOUT = 300.0
_HANDSHAKE_TIMEOUT = 30.0
_MAX_FRAME_BYTES = 16 * 1024 * 1024
_MAX_INITIAL_HISTORY_PAIRS = 100
_MAX_INITIAL_HISTORY_MESSAGE_BYTES = 64 * 1024
_MAX_INITIAL_HISTORY_TOTAL_BYTES = 1024 * 1024


class HarnessAPIError(RuntimeError):
    """Base class for harness client failures."""


class UnsupportedCapabilityError(HarnessAPIError):
    """The connected bridge does not implement a requested capability."""


class HarnessConnectionError(HarnessAPIError):
    """The API connection closed or could not exchange a frame."""


class HarnessProtocolError(HarnessAPIError):
    """The server sent a malformed or incompatible protocol frame."""


class HarnessRequestError(HarnessAPIError):
    """The harness API rejected a request."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"harness API request failed ({code}): {message}")


class HarnessTimeoutError(HarnessAPIError, TimeoutError):
    """A handshake, request, or assistant turn exceeded its timeout."""


@dataclass(frozen=True, slots=True)
class ConversationTurn:
    """One plain-text user or assistant turn in imported conversation history."""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class NativeSessionInfo:
    """Read-only native session inventory with additive fields preserved."""

    session_id: str
    parent_session_id: str | None
    agent_label: str | None
    swarm_status: Any
    worker_blueprint_policy: dict[str, Any] | None
    edit_stats: Any
    raw: dict[str, Any]


@dataclass(frozen=True, slots=True)
class NativeToolDefinition:
    """One effective native tool definition, including unknown fields."""

    name: str
    raw: dict[str, Any]


def _session_info(value: Any) -> NativeSessionInfo:
    if not isinstance(value, dict):
        raise HarnessProtocolError("list_sessions entries must be objects")
    session_id = _required_string(value, "session_id", "list_sessions")
    parent = value.get("parent_session_id")
    label = value.get("agent_label")
    policy = value.get("worker_blueprint_policy")
    if parent is not None and not isinstance(parent, str):
        raise HarnessProtocolError("parent_session_id must be a string or null")
    if label is not None and not isinstance(label, str):
        raise HarnessProtocolError("agent_label must be a string or null")
    if policy is not None and not isinstance(policy, dict):
        raise HarnessProtocolError("worker_blueprint_policy must be an object or null")
    return NativeSessionInfo(
        session_id=session_id,
        parent_session_id=parent,
        agent_label=label,
        swarm_status=value.get("swarm_status"),
        worker_blueprint_policy=policy,
        edit_stats=value.get("edit_stats"),
        raw=dict(value),
    )


def _tool_definition(value: Any) -> NativeToolDefinition:
    if not isinstance(value, dict):
        raise HarnessProtocolError("list_tools entries must be objects")
    name = value.get("name")
    if not isinstance(name, str):
        function = value.get("function")
        name = function.get("name") if isinstance(function, dict) else None
    if not isinstance(name, str) or not name:
        raise HarnessProtocolError("effective tool definition has no name")
    return NativeToolDefinition(name=name, raw=dict(value))


def _normalize_history(
    history: Sequence[ConversationTurn | Mapping[str, object]] | None,
) -> list[dict[str, str]]:
    if history is None:
        return []
    if isinstance(history, (str, bytes)) or not isinstance(history, Sequence):
        raise TypeError("history must be a sequence of ConversationTurn values")
    normalized: list[dict[str, str]] = []
    total_bytes = 0
    for index, turn in enumerate(history):
        if index >= _MAX_INITIAL_HISTORY_PAIRS * 2:
            raise ValueError("history exceeds the limit of 100 user/assistant pairs")
        if isinstance(turn, ConversationTurn):
            role, content = turn.role, turn.content
        elif isinstance(turn, Mapping):
            if set(turn) != {"role", "content"}:
                raise ValueError(
                    "history mappings must contain exactly role and content"
                )
            raw_role, raw_content = turn["role"], turn["content"]
            if type(raw_role) is not str:
                raise ValueError("history role must be a string")
            if type(raw_content) is not str:
                raise TypeError("history content must be a string")
            role, content = raw_role, raw_content
        else:
            raise TypeError(
                "history entries must be ConversationTurn or role/content mappings"
            )
        if type(role) is not str or role not in {"user", "assistant"}:
            raise ValueError("history role must be 'user' or 'assistant'")
        if type(content) is not str:
            raise TypeError("history content must be a string")
        if not content:
            raise ValueError("history content must not be empty")
        expected_role = "user" if index % 2 == 0 else "assistant"
        if role != expected_role:
            raise ValueError(
                "history must begin with user and alternate user/assistant"
            )
        content_bytes = len(content.encode("utf-8"))
        if content_bytes > _MAX_INITIAL_HISTORY_MESSAGE_BYTES:
            raise ValueError("history message exceeds the 64 KiB UTF-8 limit")
        total_bytes += content_bytes
        if total_bytes > _MAX_INITIAL_HISTORY_TOTAL_BYTES:
            raise ValueError("history exceeds the 1 MiB UTF-8 total limit")
        normalized.append({"role": role, "content": content})
    if normalized and normalized[-1]["role"] != "assistant":
        raise ValueError("history must end with an assistant turn")
    return normalized


class SessionBusyError(HarnessAPIError):
    """A second turn was submitted while this session already has one."""


class SessionSettlingError(HarnessAPIError):
    """A timed-out session is still draining its cancelled turn."""


@dataclass(frozen=True, slots=True)
class HarnessResult:
    """Final assistant text and the terminal status of a single user turn."""

    text: str
    session_id: str
    stop_reason: str | None = None
    stop_message: str | None = None
    provider_stop_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    events: tuple[dict[str, Any], ...] = ()

    @property
    def completed_normally(self) -> bool:
        """Whether the API reported a natural ``TurnDone`` without a stop."""
        return self.stop_reason is None

    @property
    def abnormal(self) -> bool:
        """Whether the turn stopped abnormally."""
        return self.stop_reason is not None

    def as_dict(self) -> dict[str, str]:
        """Return a generic assistant role/content message for callback APIs."""
        return {"role": "assistant", "content": self.text}

    @property
    def stop_info(self) -> dict[str, str | None] | None:
        """Structured abnormal stop reason, message, and provider reason."""
        if self.stop_reason is None:
            return None
        return {
            "reason": self.stop_reason,
            "message": self.stop_message,
            "provider_stop_reason": self.provider_stop_reason,
        }


@dataclass(slots=True)
class _Turn:
    session: HarnessSession
    events: queue.Queue[dict[str, Any] | BaseException]
    request_id: int
    event_sink: list[dict[str, Any]] | None = None
    stop_seen: bool = False
    abandoned: bool = False


class _SocketTransport:
    """Serialize writes and expose one buffered NDJSON stream reader."""

    def __init__(self, stream: socket.socket) -> None:
        self._socket = stream
        self._reader = stream.makefile("rb")
        self._write_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closed = False

    @classmethod
    def unix(cls, socket_path: str | os.PathLike[str]) -> _SocketTransport:
        stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            stream.connect(os.fspath(socket_path))
        except OSError:
            stream.close()
            raise
        return cls(stream)

    @classmethod
    def tcp(cls, host: str, port: int) -> _SocketTransport:
        return cls(socket.create_connection((host, port)))

    def send(self, frame: dict[str, Any]) -> None:
        encoded = (
            json.dumps(frame, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            + b"\n"
        )
        with self._write_lock:
            if self._closed:
                raise OSError("harness API transport is closed")
            self._socket.sendall(encoded)

    def receive(self) -> dict[str, Any]:
        while True:
            line = self._reader.readline(_MAX_FRAME_BYTES + 1)
            if not line:
                raise EOFError("harness API stream closed")
            if len(line) > _MAX_FRAME_BYTES:
                raise HarnessProtocolError(
                    f"harness API frame exceeds {_MAX_FRAME_BYTES} bytes"
                )
            if not line.endswith(b"\n"):
                raise HarnessProtocolError(
                    "harness API frame is not newline-terminated"
                )
            if line.strip():
                break
        try:
            frame = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HarnessProtocolError(
                f"invalid harness API JSON frame: {error}"
            ) from error
        if not isinstance(frame, dict):
            raise HarnessProtocolError("harness API frame must be a JSON object")
        return frame

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._reader.close()
            except OSError:
                pass
            self._socket.close()


type _Reply = dict[str, Any]


class HarnessClient:
    """Connect to an already-running jcode harness API bridge.

    The client owns one connection and a receive thread. It never starts the
    jcode daemon, harness bridge, or a TCP relay.
    """

    def __init__(
        self,
        transport: _SocketTransport,
        *,
        turn_timeout: float = _DEFAULT_TURN_TIMEOUT,
        _transport_factory: Any | None = None,
    ) -> None:
        self._transport = transport
        try:
            self._turn_timeout = _positive_timeout(turn_timeout, "turn_timeout")
        except BaseException:
            transport.close()
            raise
        self._transport_factory = _transport_factory
        self._lock = threading.RLock()
        self._session_create_lock = threading.Lock()
        self._closed = False
        self._closing = False
        self._next_id = 1
        self._pending_replies: dict[int, queue.Queue[_Reply | BaseException]] = {}
        self._turns_by_request: dict[int, _Turn] = {}
        self._sessions: dict[str, HarnessSession] = {}
        self._children: list[HarnessClient] = []
        self._thread_sessions: dict[str, tuple[str | None, HarnessSession]] = {}
        self._thread_locks: dict[tuple[str, str], threading.Lock] = {}
        self._reader_error: BaseException | None = None
        self._reader = threading.Thread(
            target=self._receive_loop,
            name="jcode-harness-client-receiver",
            daemon=True,
        )
        self._reader.start()
        try:
            hello = self._request(
                "hello",
                {
                    "min_version": _API_MAJOR,
                    "max_version": _API_MAJOR,
                    "client": f"jcode-harness-client/{_package_version()}",
                },
                timeout=_HANDSHAKE_TIMEOUT,
            )
            if hello.get("ev") == "error":
                raise _request_error(hello)
            if hello.get("ev") != "hello_ok":
                raise HarnessProtocolError(
                    "harness API handshake did not return hello_ok"
                )
            version = hello.get("version")
            if isinstance(version, bool) or not isinstance(version, int):
                raise HarnessProtocolError("hello_ok has no integer protocol version")
            if version != _API_MAJOR:
                raise HarnessProtocolError(
                    "unsupported harness API major version "
                    f"{version}; expected {_API_MAJOR}"
                )
            self.server = _required_string(hello, "server", "hello_ok")
            capabilities = hello.get("capabilities", [])
            if not isinstance(capabilities, list) or not all(
                isinstance(value, str) for value in capabilities
            ):
                raise HarnessProtocolError(
                    "hello_ok capabilities must be a string list"
                )
            # Unknown capability names are intentionally retained but ignored.
            self.capabilities = tuple(capabilities)
        except BaseException:
            self.close()
            raise

    @classmethod
    def unix(
        cls,
        socket_path: str | os.PathLike[str],
        *,
        turn_timeout: float = _DEFAULT_TURN_TIMEOUT,
    ) -> HarnessClient:
        """Connect directly to a local Unix-domain harness API socket."""
        socket_path = os.fspath(socket_path)

        def factory() -> _SocketTransport:
            return _SocketTransport.unix(socket_path)

        try:
            transport = factory()
        except OSError as error:
            raise HarnessConnectionError(
                f"cannot connect to {socket_path}: {error}"
            ) from error
        return cls(transport, turn_timeout=turn_timeout, _transport_factory=factory)

    @classmethod
    def tcp(
        cls,
        host: str,
        port: int,
        *,
        turn_timeout: float = _DEFAULT_TURN_TIMEOUT,
    ) -> HarnessClient:
        """Connect to a TCP stream, typically forwarded by a user's socat relay."""

        def factory() -> _SocketTransport:
            return _SocketTransport.tcp(host, port)

        try:
            transport = factory()
        except OSError as error:
            raise HarnessConnectionError(
                f"cannot connect to {host}:{port}: {error}"
            ) from error
        return cls(transport, turn_timeout=turn_timeout, _transport_factory=factory)

    def create_session(
        self,
        *,
        working_dir: str | os.PathLike[str] | None = None,
        history: Sequence[ConversationTurn | Mapping[str, object]] | None = None,
    ) -> HarnessSession:
        """Create and attach a new session using the daemon's configuration."""
        normalized_history = _normalize_history(history)
        directory = _path_string(working_dir)
        if normalized_history and "initial_history" not in self.capabilities:
            raise UnsupportedCapabilityError(
                "the connected jcode bridge does not support initial_history"
            )
        with self._session_create_lock:
            with self._lock:
                self._ensure_open_locked()
                needs_connection = bool(self._sessions)
                factory = self._transport_factory
            if needs_connection:
                if factory is None:
                    raise HarnessAPIError(
                        "this client transport cannot create a second attached session"
                    )
                try:
                    transport = factory()
                except OSError as error:
                    raise HarnessConnectionError(
                        f"cannot open a connection for another session: {error}"
                    ) from error
                child = HarnessClient(
                    transport,
                    turn_timeout=self._turn_timeout,
                    _transport_factory=factory,
                )
                try:
                    with self._lock:
                        self._ensure_open_locked()
                        self._children.append(child)
                    return child.create_session(
                        working_dir=directory,
                        history=normalized_history or None,
                    )
                except BaseException:
                    child.close()
                    with self._lock:
                        self._children.remove(child)
                    raise
            return self._create_local_session(directory, normalized_history or None)

    def _create_local_session(
        self, directory: str | None, history: list[dict[str, str]] | None = None
    ) -> HarnessSession:
        payload: dict[str, Any] = (
            {"working_dir": directory} if directory is not None else {}
        )
        if history:
            payload["initial_history"] = history
        reply = self._request(
            "create_session",
            payload,
            timeout=_HANDSHAKE_TIMEOUT,
        )
        if reply.get("ev") == "error":
            raise _request_error(reply)
        if reply.get("ev") != "attached":
            raise HarnessProtocolError("create_session did not return attached")
        info = reply.get("session")
        if not isinstance(info, dict):
            raise HarnessProtocolError("attached reply has no session object")
        session_id = _required_string(info, "session_id", "attached.session")
        session = HarnessSession(self, session_id, directory)
        with self._lock:
            self._ensure_open_locked()
            if session_id in self._sessions:
                raise HarnessProtocolError(
                    f"server reused active session id {session_id!r}"
                )
            self._sessions[session_id] = session
        return session

    def list_sessions(self) -> list[NativeSessionInfo]:
        """Return the daemon's current native session inventory."""

        reply = self._request(
            "list_sessions", {"include_archived": True}, timeout=_HANDSHAKE_TIMEOUT
        )
        if reply.get("ev") == "error":
            raise _request_error(reply)
        if reply.get("ev") != "sessions" or not isinstance(reply.get("sessions"), list):
            raise HarnessProtocolError(
                "list_sessions did not return a native session list"
            )
        return [_session_info(value) for value in reply["sessions"]]

    def list_tools(self, session_id: str) -> list[NativeToolDefinition]:
        """Return effective tool definitions for one existing native session."""

        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session_id must be a nonempty string")
        reply = self._request(
            "list_tools", {"session_id": session_id}, timeout=_HANDSHAKE_TIMEOUT
        )
        if reply.get("ev") == "error":
            raise _request_error(reply)
        if (
            reply.get("ev") != "tools"
            or reply.get("session_id") != session_id
            or not isinstance(reply.get("tools"), list)
        ):
            raise HarnessProtocolError("list_tools did not return a native tool list")
        return [_tool_definition(value) for value in reply["tools"]]

    def send_message(
        self,
        content: str,
        *,
        thread_id: str | None = None,
        working_dir: str | os.PathLike[str] | None = None,
        timeout: float | None = None,
        capture_events: bool = False,
        event_sink: list[dict[str, Any]] | None = None,
    ) -> HarnessResult:
        """Send one message, optionally reusing an in-memory thread mapping.

        Without ``thread_id``, every call creates a fresh session. A thread ID
        stays bound to its first call's working directory for this client's
        lifetime.
        """
        directory = _path_string(working_dir)
        if thread_id is None:
            call_thread = str(threading.get_ident())
            with self._lock:
                self._ensure_open_locked()
                call_lock = self._thread_locks.setdefault(
                    ("direct", call_thread), threading.Lock()
                )
            with call_lock:
                session = self.create_session(working_dir=directory)
                return session.send_message(
                    content,
                    timeout=timeout,
                    capture_events=capture_events,
                    event_sink=event_sink,
                )
        if not isinstance(thread_id, str) or not thread_id:
            raise ValueError("thread_id must be a non-empty string")
        with self._lock:
            self._ensure_open_locked()
            thread_lock = self._thread_locks.setdefault(
                ("mapped", thread_id), threading.Lock()
            )
        with thread_lock:
            mapped_session: HarnessSession | None = None
            with self._lock:
                bound = self._thread_sessions.get(thread_id)
                if bound is not None:
                    bound_directory, mapped_session = bound
                    if mapped_session._client._reader_error is not None:
                        raise HarnessConnectionError(
                            "thread session connection is unavailable; "
                            "create a new client to start another session"
                        )
                    if directory is not None and directory != bound_directory:
                        raise ValueError(
                            f"thread_id {thread_id!r} is bound to working_dir "
                            f"{bound_directory!r}, not {directory!r}"
                        )
                else:
                    mapped_session = None
                self._ensure_open_locked()
            if mapped_session is None:
                mapped_session = self.create_session(working_dir=directory)
                with self._lock:
                    self._ensure_open_locked()
                    self._thread_sessions[thread_id] = (directory, mapped_session)
            return mapped_session.send_message(
                content,
                timeout=timeout,
                capture_events=capture_events,
                event_sink=event_sink,
            )

    def close(self) -> None:
        """Best-effort detach all created sessions, then close the transport."""
        with self._lock:
            if self._closed or self._closing:
                return
            self._closing = True
            sessions = tuple(self._sessions.values())
            children = tuple(self._children)
            detach_frames: list[dict[str, Any]] = []
            for session in sessions:
                detach_frames.append(
                    {
                        "v": _API_MAJOR,
                        "id": self._allocate_id_locked(),
                        "req": "detach_session",
                        "session_id": session.session_id,
                    }
                )
        for frame in detach_frames:
            try:
                self._transport.send(frame)
            except OSError:
                pass
        with self._lock:
            self._closed = True
        self._transport.close()
        for child in children:
            child.close()
        self._fail_waiters(HarnessConnectionError("harness client closed"))

    def __enter__(self) -> HarnessClient:
        with self._lock:
            self._ensure_open_locked()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _request(
        self,
        request: str,
        fields: dict[str, Any],
        *,
        timeout: float,
    ) -> _Reply:
        reply_queue: queue.Queue[_Reply | BaseException] = queue.Queue(maxsize=1)
        with self._lock:
            self._ensure_open_locked()
            request_id = self._allocate_id_locked()
            self._pending_replies[request_id] = reply_queue
            frame = {"v": _API_MAJOR, "id": request_id, "req": request, **fields}
        try:
            self._transport.send(frame)
        except OSError as error:
            with self._lock:
                self._pending_replies.pop(request_id, None)
            raise HarnessConnectionError(
                f"failed to send {request}: {error}"
            ) from error
        try:
            response = reply_queue.get(timeout=timeout)
        except queue.Empty as error:
            with self._lock:
                self._pending_replies.pop(request_id, None)
            raise HarnessTimeoutError(
                f"timed out waiting for {request} reply"
            ) from error
        finally:
            with self._lock:
                self._pending_replies.pop(request_id, None)
        if isinstance(response, BaseException):
            raise response
        return response

    def _start_turn(
        self,
        session: HarnessSession,
        content: str,
        event_sink: list[dict[str, Any]] | None = None,
    ) -> _Turn:
        with self._lock:
            self._ensure_open_locked()
            if session._settling:
                raise SessionSettlingError(
                    f"session {session.session_id!r} is still settling a cancelled turn"
                )
            if session._active_turn is not None:
                raise SessionBusyError(
                    f"session {session.session_id!r} already has an in-flight turn"
                )
            request_id = self._allocate_id_locked()
            turn = _Turn(session, queue.Queue(), request_id, event_sink)
            session._active_turn = turn
            self._turns_by_request[request_id] = turn
            frame = {
                "v": _API_MAJOR,
                "id": request_id,
                "req": "send_message",
                "session_id": session.session_id,
                "content": content,
            }
        try:
            self._transport.send(frame)
        except OSError as error:
            with self._lock:
                self._turns_by_request.pop(request_id, None)
                if session._active_turn is turn:
                    session._active_turn = None
            raise HarnessConnectionError(
                f"failed to send user message: {error}"
            ) from error
        return turn

    def _finish_turn(self, turn: _Turn) -> None:
        with self._lock:
            self._turns_by_request.pop(turn.request_id, None)
            session = turn.session
            if session._active_turn is turn:
                session._active_turn = None
                session._settling = False

    def _timeout_turn(self, turn: _Turn) -> None:
        session = turn.session
        with self._lock:
            if session._active_turn is not turn:
                return
            turn.abandoned = True
            session._settling = True
            cancel = {
                "v": _API_MAJOR,
                "id": self._allocate_id_locked(),
                "req": "cancel",
                "session_id": session.session_id,
            }
        try:
            self._transport.send(cancel)
        except OSError:
            # Preserve the original turn timeout. The reader will surface a
            # subsequent EOF to all other outstanding callers.
            return

    def _allocate_id_locked(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    def _ensure_open_locked(self) -> None:
        if self._closed or getattr(self, "_closing", False):
            raise HarnessConnectionError("harness client is closed")
        if self._reader_error is not None:
            raise HarnessConnectionError(
                f"harness API reader failed: {self._reader_error}"
            )

    def _receive_loop(self) -> None:
        failure: BaseException | None = None
        try:
            while True:
                frame = self._transport.receive()
                self._dispatch_frame(frame)
        except BaseException as error:
            failure = error
        if failure is not None:
            if isinstance(failure, HarnessAPIError):
                reader_error = failure
            elif isinstance(failure, EOFError | OSError):
                reader_error = HarnessConnectionError(str(failure))
            else:
                reader_error = HarnessProtocolError(str(failure))
            with self._lock:
                self._reader_error = reader_error
            self._fail_waiters(reader_error)

    def _dispatch_frame(self, frame: dict[str, Any]) -> None:
        version = frame.get("v")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != _API_MAJOR
        ):
            raise HarnessProtocolError(
                f"server frame has protocol major {version!r}; expected {_API_MAJOR}"
            )
        event_name = frame.get("ev")
        if not isinstance(event_name, str):
            raise HarnessProtocolError("server frame has no string event kind")
        if event_name not in _KNOWN_EVENTS:
            # Unknown events and replies are additive by protocol contract.
            return
        reply_to = frame.get("reply_to")
        if reply_to is not None and (
            isinstance(reply_to, bool) or not isinstance(reply_to, int) or reply_to < 0
        ):
            raise HarnessProtocolError("reply_to must be a non-negative integer")

        with self._lock:
            if reply_to is not None:
                reply_queue = self._pending_replies.get(reply_to)
                if reply_queue is not None:
                    self._queue_put(reply_queue, frame)
                    return
                turn = self._turns_by_request.get(reply_to)
                if turn is not None and event_name == "error":
                    self._route_turn_event_locked(turn, frame)
                    return
                # A reply with an unknown request ID cannot safely be
                # attributed to any current turn or connection-level waiter.
                return
            if event_name == "error":
                session_id = frame.get("session_id")
                if isinstance(session_id, str):
                    session = self._sessions.get(session_id)
                    if session is not None and session._active_turn is not None:
                        self._route_turn_event_locked(session._active_turn, frame)
                    return
                self._dispatch_unattributed_error_locked(frame)
                return
            session_id = frame.get("session_id")
            if event_name == "attached":
                # Attached is normally matched by reply_to. No session turn
                # can own a reply-only frame without a top-level session_id.
                return
            if not isinstance(session_id, str):
                return
            session = self._sessions.get(session_id)
            if session is None or session._active_turn is None:
                return
            self._route_turn_event_locked(session._active_turn, frame)

    def _dispatch_unattributed_error_locked(self, frame: _Reply) -> None:
        error = _request_error(frame)
        for reply_queue in tuple(self._pending_replies.values()):
            self._queue_put(reply_queue, error)
        active: list[_Turn] = []
        for session in self._sessions.values():
            turn = session._active_turn
            if turn is not None and turn not in active:
                active.append(turn)
        for turn in active:
            # The bridge reports failed turns as TurnStopped, then an
            # unattributed Error, then TurnDone. Preserve the abnormal result
            # for stopped turns while treating the same frame as a connection
            # error for every other in-flight turn.
            if turn.stop_seen:
                self._route_turn_event_locked(turn, frame)
            else:
                self._route_turn_event_locked(
                    turn,
                    {
                        "_connection_error": True,
                        "ev": "error",
                        "code": frame.get("code", "internal"),
                        "message": frame.get("message", "unattributed API error"),
                    },
                )

    def _route_turn_event_locked(self, turn: _Turn, frame: _Reply) -> None:
        event_name = frame.get("ev")
        if turn.event_sink is not None:
            turn.event_sink.append(dict(frame))
        if event_name == "turn_stopped":
            turn.stop_seen = True
        if turn.abandoned:
            # Timed-out turns are drained and discarded. Do not allow their
            # late text or terminal frames to contaminate the next turn.
            if event_name == "turn_done" or (
                event_name == "error" and not turn.stop_seen
            ):
                self._settle_abandoned_turn_locked(turn)
            return
        self._queue_put(turn.events, frame)

    def _settle_abandoned_turn_locked(self, turn: _Turn) -> None:
        turn.session._settling = False
        if turn.session._active_turn is turn:
            turn.session._active_turn = None
        self._turns_by_request.pop(turn.request_id, None)

    def _fail_waiters(self, error: BaseException) -> None:
        with self._lock:
            for reply_queue in tuple(self._pending_replies.values()):
                self._queue_put(reply_queue, error)
            for turn in tuple(self._turns_by_request.values()):
                self._queue_put(turn.events, error)

    @staticmethod
    def _queue_put(
        target: queue.Queue[_Reply | BaseException],
        value: _Reply | BaseException,
    ) -> None:
        try:
            target.put_nowait(value)
        except queue.Full:
            # One reply/event is sufficient to unblock a waiter. The turn
            # reader drains the remainder after waking.
            pass


class HarnessSession:
    """A session created by one ``HarnessClient`` instance."""

    def __init__(
        self,
        client: HarnessClient,
        session_id: str,
        working_dir: str | None,
    ) -> None:
        self._client = client
        self.session_id = session_id
        self.working_dir = working_dir
        self._active_turn: _Turn | None = None
        self._settling = False

    def send_message(
        self,
        content: str,
        *,
        timeout: float | None = None,
        capture_events: bool = False,
        event_sink: list[dict[str, Any]] | None = None,
    ) -> HarnessResult:
        """Send one user turn and synchronously await its terminal event."""
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        turn_timeout = (
            self._client._turn_timeout
            if timeout is None
            else _positive_timeout(timeout, "timeout")
        )
        turn = self._client._start_turn(self, content, event_sink)
        messages: OrderedDict[str | None, _MessageText] = OrderedDict()
        captured: list[dict[str, Any]] = []
        stop: _Reply | None = None
        trailing_error: _Reply | None = None
        completed_message_ids: set[str | None] = set()
        deadline = time.monotonic() + turn_timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HarnessTimeoutError(
                        f"turn in session {self.session_id!r} exceeded "
                        f"{turn_timeout:g}s"
                    )
                try:
                    item = turn.events.get(timeout=remaining)
                except queue.Empty:
                    raise HarnessTimeoutError(
                        f"turn in session {self.session_id!r} exceeded "
                        f"{turn_timeout:g}s"
                    ) from None
                if isinstance(item, BaseException):
                    raise item
                if capture_events:
                    captured.append(dict(item))
                name = item.get("ev")
                if name == "error":
                    error = _request_error(item)
                    if stop is not None:
                        trailing_error = item
                        continue
                    if item.get("_connection_error"):
                        raise HarnessRequestError(error.code, error.message)
                    raise error
                if name in {"text_delta", "text_replace", "text_done"}:
                    self._apply_text_event(messages, item)
                    if name == "text_done":
                        completed_message_ids.add(item.get("message_id"))
                    continue
                if name == "turn_stopped":
                    _required_string(item, "reason", "turn_stopped")
                    _required_string(item, "message", "turn_stopped")
                    stop = item
                    continue
                if name == "turn_done":
                    if stop is None:
                        result_text = _joined_text(
                            messages,
                            completed_only=True,
                            completed_ids=completed_message_ids,
                        )
                        result = HarnessResult(
                            result_text, self.session_id, events=tuple(captured)
                        )
                    else:
                        result = HarnessResult(
                            text=_joined_text(
                                messages,
                                completed_only=False,
                                completed_ids=completed_message_ids,
                            ),
                            session_id=self.session_id,
                            events=tuple(captured),
                            stop_reason=_required_string(
                                stop, "reason", "turn_stopped"
                            ),
                            stop_message=_required_string(
                                stop, "message", "turn_stopped"
                            ),
                            provider_stop_reason=_optional_string(
                                stop, "provider_stop_reason"
                            ),
                            error_code=(
                                _optional_string(trailing_error, "code")
                                if trailing_error is not None
                                else None
                            ),
                            error_message=(
                                _optional_string(trailing_error, "message")
                                if trailing_error is not None
                                else None
                            ),
                        )
                    self._client._finish_turn(turn)
                    return result
        except HarnessTimeoutError:
            self._client._timeout_turn(turn)
            raise
        except BaseException:
            self._client._finish_turn(turn)
            raise

    @staticmethod
    def _apply_text_event(
        messages: OrderedDict[str | None, _MessageText], frame: _Reply
    ) -> None:
        message_id = frame.get("message_id")
        if message_id is not None and not isinstance(message_id, str):
            raise HarnessProtocolError("text event message_id must be a string or null")
        message = messages.setdefault(message_id, _MessageText())
        event_name = frame.get("ev")
        if event_name == "text_delta":
            message.text += _required_string(frame, "text", "text_delta")
        elif event_name == "text_replace":
            message.text = _required_string(frame, "text", "text_replace")


@dataclass(slots=True)
class _MessageText:
    text: str = ""


def _joined_text(
    messages: OrderedDict[str | None, _MessageText],
    *,
    completed_only: bool,
    completed_ids: set[str | None],
) -> str:
    return "".join(
        message.text
        for message_id, message in messages.items()
        if not completed_only or message_id in completed_ids
    )


def _request_error(frame: _Reply) -> HarnessRequestError:
    code = frame.get("code", "internal")
    message = frame.get("message", "harness API request failed")
    if not isinstance(code, str):
        code = str(code)
    if not isinstance(message, str):
        message = str(message)
    return HarnessRequestError(code, message)


def _required_string(frame: _Reply, key: str, source: str) -> str:
    value = frame.get(key)
    if not isinstance(value, str):
        raise HarnessProtocolError(f"{source} has no string {key!r}")
    return value


def _optional_string(frame: _Reply | None, key: str) -> str | None:
    if frame is None:
        return None
    value = frame.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HarnessProtocolError(f"event field {key!r} must be a string or null")
    return value


def _path_string(path: str | os.PathLike[str] | None) -> str | None:
    if path is None:
        return None
    value = os.fspath(path)
    if not isinstance(value, str):
        raise TypeError("working_dir must resolve to a string path")
    return value


def _positive_timeout(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a positive number of seconds")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError(f"{name} must be a finite positive number of seconds")
    return timeout


def _package_version() -> str:
    # Keep the handshake identifier aligned with the project metadata without
    # importing packaging or relying on an installed distribution record.
    return "0.1.0"
