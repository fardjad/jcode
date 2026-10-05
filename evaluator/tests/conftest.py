from __future__ import annotations

import json
import socket
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

Frame = dict[str, Any]
MessageHandler = Callable[..., None]


class FakeHarnessServer:
    """Small real-socket NDJSON fake supporting Unix and TCP clients."""

    def __init__(
        self,
        *,
        unix_path: Path | None = None,
        hello_error: bool = False,
        hello_version: int = 1,
        on_message: MessageHandler | None = None,
        split_frames: bool = False,
        capabilities: list[str] | None = None,
        create_error: Frame | None = None,
        close_on_create: bool = False,
    ) -> None:
        family = socket.AF_UNIX if unix_path is not None else socket.AF_INET
        self.listener = socket.socket(family, socket.SOCK_STREAM)
        if unix_path is None:
            self.listener.bind(("127.0.0.1", 0))
        else:
            self.listener.bind(str(unix_path))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.address = self.listener.getsockname()
        self.unix_path = unix_path
        self.hello_error = hello_error
        self.hello_version = hello_version
        self.on_message = on_message or self._default_message
        self.split_frames = split_frames
        self.capabilities = (
            capabilities
            if capabilities is not None
            else ["sessions", "future_capability", "initial_history"]
        )
        self.create_error = create_error
        self.close_on_create = close_on_create
        self.requests: list[Frame] = []
        self._request_lock = threading.Lock()
        self._session_count = 0
        self._write_locks: dict[int, threading.Lock] = {}
        self._stop = threading.Event()
        self._accept_thread = threading.Thread(target=self._accept, daemon=True)
        self._accept_thread.start()

    def client(self, *, turn_timeout: float = 1.0) -> Any:
        from jcode_harness_client import HarnessClient

        if self.unix_path is not None:
            return HarnessClient.unix(self.unix_path, turn_timeout=turn_timeout)
        host, port = self.address[:2]
        return HarnessClient.tcp(host, port, turn_timeout=turn_timeout)

    def close(self) -> None:
        self._stop.set()
        self.listener.close()
        self._accept_thread.join(timeout=1)
        if self.unix_path is not None:
            self.unix_path.unlink(missing_ok=True)

    def __enter__(self) -> FakeHarnessServer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self._write_locks[id(connection)] = threading.Lock()
            threading.Thread(
                target=self._serve, args=(connection,), daemon=True
            ).start()

    def _serve(self, connection: socket.socket) -> None:
        session_id: str | None = None
        try:
            reader = connection.makefile("rb")
            for raw in reader:
                if not raw.strip():
                    continue
                request = json.loads(raw)
                with self._request_lock:
                    self.requests.append(request)
                request_id = request["id"]
                kind = request["req"]
                if kind == "hello":
                    if self.hello_error:
                        self.send(
                            connection,
                            {
                                "v": 1,
                                "reply_to": request_id,
                                "ev": "error",
                                "code": "unsupported_version",
                                "message": "version rejected",
                            },
                        )
                    else:
                        self.send(
                            connection,
                            {
                                "v": 1,
                                "reply_to": request_id,
                                "ev": "hello_ok",
                                "version": self.hello_version,
                                "server": "fake/1",
                                "capabilities": self.capabilities,
                            },
                        )
                elif kind == "create_session":
                    if self.close_on_create:
                        connection.close()
                        return
                    if self.create_error is not None:
                        self.send(
                            connection,
                            {"v": 1, "reply_to": request_id, **self.create_error},
                        )
                        continue
                    with self._request_lock:
                        self._session_count += 1
                        session_id = f"session-{self._session_count}"
                    self.send(
                        connection,
                        {"v": 1, "reply_to": request_id, "ev": "future_reply"},
                    )
                    self.send(
                        connection,
                        {
                            "v": 1,
                            "reply_to": request_id,
                            "ev": "attached",
                            "session": {
                                "session_id": session_id,
                                "status": "idle",
                            },
                        },
                    )
                elif kind == "send_message":
                    session_id = request["session_id"]
                    self.send(
                        connection,
                        {
                            "v": 1,
                            "ev": "message_accepted",
                            "session_id": session_id,
                        },
                    )
                    threading.Thread(
                        target=self.on_message,
                        args=(
                            request["content"],
                            session_id,
                            lambda frame, conn=connection: self.send(conn, frame),
                        ),
                        daemon=True,
                    ).start()
                elif kind == "detach_session":
                    pass
                elif kind == "cancel":
                    self.send(
                        connection,
                        {"v": 1, "reply_to": request_id, "ev": "ok"},
                    )
                else:
                    self.send(
                        connection,
                        {"v": 1, "reply_to": request_id, "ev": "ok"},
                    )
        except OSError, ValueError, json.JSONDecodeError:
            pass
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def send(self, connection: socket.socket, frame: Frame) -> None:
        encoded = json.dumps(frame, separators=(",", ":")).encode() + b"\n"
        lock = self._write_locks.get(id(connection), threading.Lock())
        with lock:
            try:
                if self.split_frames:
                    for byte in encoded:
                        connection.sendall(bytes([byte]))
                else:
                    connection.sendall(encoded)
            except OSError:
                pass

    @staticmethod
    def _default_message(
        _content: str,
        session_id: str,
        send: Callable[[Frame], None],
    ) -> None:
        send(
            {
                "v": 1,
                "ev": "text_delta",
                "session_id": session_id,
                "message_id": "assistant-1",
                "text": "reply",
            }
        )
        send(
            {
                "v": 1,
                "ev": "text_done",
                "session_id": session_id,
                "message_id": "assistant-1",
            }
        )
        send({"v": 1, "ev": "turn_done", "session_id": session_id})


@pytest.fixture
def fake_server() -> FakeHarnessServer:
    with FakeHarnessServer() as server:
        yield server
