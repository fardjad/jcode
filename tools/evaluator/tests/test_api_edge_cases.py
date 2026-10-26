from __future__ import annotations

import json
import queue
import socket
import threading
import time
from typing import Any

import pytest
from conftest import FakeHarnessServer

from jcode_harness_client import (
    HarnessClient,
    HarnessConnectionError,
    HarnessProtocolError,
    HarnessRequestError,
    SessionSettlingError,
)
from jcode_harness_client.client import _SocketTransport


def client_from_handler(handler: Any) -> tuple[HarnessClient, FakeHarnessServer]:
    server = FakeHarnessServer(on_message=handler)
    host, port = server.address[:2]
    return HarnessClient.tcp(host, port), server


def test_frame_version_is_checked_and_unattributed_error_fails_turn() -> None:
    def wrong_version(_content: str, sid: str, send: Any) -> None:
        send({"v": 2, "ev": "turn_done", "session_id": sid})

    client, server = client_from_handler(wrong_version)
    try:
        session = client.create_session()
        with pytest.raises(HarnessProtocolError, match="protocol major 2"):
            session.send_message("bad version")
    finally:
        client.close()
        server.close()


def test_text_stream_without_message_id_uses_legacy_single_message(
    fake_server: FakeHarnessServer,
) -> None:
    def legacy_text(_content: str, sid: str, send: Any) -> None:
        send({"v": 1, "ev": "text_delta", "session_id": sid, "text": "legacy"})
        send({"v": 1, "ev": "text_done", "session_id": sid})
        send({"v": 1, "ev": "turn_done", "session_id": sid})

    fake_server.on_message = legacy_text
    with fake_server.client() as client:
        result = client.create_session().send_message("legacy")
    assert result.text == "legacy"


def test_server_connection_close_fails_in_flight_turn() -> None:
    server = FakeHarnessServer()
    original = server._serve

    def closing_serve(connection: socket.socket) -> None:
        reader = connection.makefile("rb")
        try:
            for raw in reader:
                frame = json.loads(raw)
                if frame["req"] == "hello":
                    server.send(
                        connection,
                        {
                            "v": 1,
                            "reply_to": frame["id"],
                            "ev": "hello_ok",
                            "version": 1,
                            "server": "fake/1",
                        },
                    )
                elif frame["req"] == "create_session":
                    server.send(
                        connection,
                        {
                            "v": 1,
                            "reply_to": frame["id"],
                            "ev": "attached",
                            "session": {"session_id": "broken", "status": "idle"},
                        },
                    )
                elif frame["req"] == "send_message":
                    return
        finally:
            connection.close()

    server._serve = closing_serve  # type: ignore[method-assign]
    del original
    try:
        with server.client() as client:
            session = client.create_session()
            with pytest.raises(HarnessConnectionError, match="closed"):
                session.send_message("close connection")
    finally:
        server.close()


def test_matched_stream_error_reply_to_turn_request_raises() -> None:
    def matched_error(_content: str, sid: str, send: Any) -> None:
        send(
            {
                "v": 1,
                "reply_to": next(
                    request["id"]
                    for request in server.requests
                    if request["req"] == "send_message"
                ),
                "ev": "error",
                "code": "invalid_request",
                "message": f"turn failed for {sid}",
            }
        )

    server = FakeHarnessServer(on_message=matched_error)
    try:
        with server.client() as client:
            session = client.create_session()
            with pytest.raises(HarnessRequestError, match="turn failed"):
                session.send_message("reply error")
    finally:
        server.close()


def test_message_accepted_is_not_a_terminal_turn_event(
    fake_server: FakeHarnessServer,
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def delayed(content: str, sid: str, send: Any) -> None:
        entered.set()
        release.wait(timeout=2)
        FakeHarnessServer._default_message(content, sid, send)

    fake_server.on_message = delayed
    with fake_server.client() as client:
        session = client.create_session()
        results: list[Any] = []
        worker = threading.Thread(
            target=lambda: results.append(session.send_message("wait for output"))
        )
        worker.start()
        assert entered.wait(timeout=1)
        assert not results
        release.set()
        worker.join(timeout=1)
    assert results and results[0].text == "reply"


def test_unattributed_error_unblocks_pending_request_reply_queue() -> None:
    server = FakeHarnessServer()
    original = server._serve

    def connection_error(connection: socket.socket) -> None:
        reader = connection.makefile("rb")
        try:
            for raw in reader:
                frame = json.loads(raw)
                if frame["req"] == "hello":
                    server.send(
                        connection,
                        {
                            "v": 1,
                            "reply_to": frame["id"],
                            "ev": "hello_ok",
                            "version": 1,
                            "server": "fake/1",
                        },
                    )
                elif frame["req"] == "create_session":
                    server.send(
                        connection,
                        {
                            "v": 1,
                            "ev": "error",
                            "code": "internal",
                            "message": "bridge-level error",
                        },
                    )
        finally:
            connection.close()

    server._serve = connection_error  # type: ignore[method-assign]
    del original
    try:
        with server.client() as client:
            with pytest.raises(HarnessRequestError, match="bridge-level error"):
                client.create_session()
    finally:
        server.close()


def test_turn_stopped_without_error_is_returned_and_next_turn_is_clean(
    fake_server: FakeHarnessServer,
) -> None:
    calls = 0

    def stop_once(content: str, sid: str, send: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            send(
                {
                    "v": 1,
                    "ev": "turn_stopped",
                    "session_id": sid,
                    "reason": "limit_reached",
                    "message": "budget",
                    "provider_stop_reason": "max_tokens",
                }
            )
            send({"v": 1, "ev": "turn_done", "session_id": sid})
        else:
            FakeHarnessServer._default_message(content, sid, send)

    fake_server.on_message = stop_once
    with fake_server.client() as client:
        session = client.create_session()
        abnormal = session.send_message("limited")
        normal = session.send_message("normal")
    assert abnormal.abnormal
    assert abnormal.provider_stop_reason == "max_tokens"
    assert normal.completed_normally
    assert normal.text == "reply"


def test_unattributed_api_error_without_stop_fails_turn(
    fake_server: FakeHarnessServer,
) -> None:
    def error_first(content: str, sid: str, send: Any) -> None:
        if content == "bad":
            send(
                {
                    "v": 1,
                    "ev": "error",
                    "code": "invalid_request",
                    "message": "stale request",
                }
            )
            return
        else:
            FakeHarnessServer._default_message(content, sid, send)

    fake_server.on_message = error_first
    with fake_server.client() as client:
        session = client.create_session()
        with pytest.raises(HarnessRequestError, match="stale request"):
            session.send_message("bad")


def test_unknown_reply_id_error_is_skipped_without_failing_turn(
    fake_server: FakeHarnessServer,
) -> None:
    def stale_reply(_content: str, sid: str, send: Any) -> None:
        send(
            {
                "v": 1,
                "reply_to": 999_999,
                "ev": "error",
                "code": "invalid_request",
                "message": "stale request",
            }
        )
        FakeHarnessServer._default_message("", sid, send)

    fake_server.on_message = stale_reply
    with fake_server.client() as client:
        result = client.create_session().send_message("stale")
    assert result.text == "reply"


def test_timeout_remains_gated_until_terminal_turn_done_even_after_error(
    fake_server: FakeHarnessServer,
) -> None:
    channel: queue.Queue[tuple[str, Any]] = queue.Queue()

    def wait_for_cancel(content: str, sid: str, send: Any) -> None:
        if content == "late":
            channel.put((sid, send))
        else:
            FakeHarnessServer._default_message(content, sid, send)

    fake_server.on_message = wait_for_cancel
    with fake_server.client(turn_timeout=0.05) as client:
        session = client.create_session()
        with pytest.raises(TimeoutError):
            session.send_message("late")
        sid, send = channel.get(timeout=1)
        send(
            {
                "v": 1,
                "ev": "turn_stopped",
                "session_id": sid,
                "reason": "interrupted",
                "message": "cancelled",
            }
        )
        send({"v": 1, "ev": "error", "code": "internal", "message": "after stop"})
        time.sleep(0.02)
        with pytest.raises(SessionSettlingError):
            session.send_message("still settling")
        send({"v": 1, "ev": "turn_done", "session_id": sid})
        deadline = time.monotonic() + 1
        while session._settling and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not session._settling


def test_context_manager_detaches_and_closes_custom_socket_transport() -> None:
    client_socket, server_socket = socket.socketpair()
    received: queue.Queue[dict[str, Any]] = queue.Queue()

    def serve() -> None:
        reader = server_socket.makefile("rb")
        first = json.loads(reader.readline())
        received.put(first)
        server_socket.sendall(
            json.dumps(
                {
                    "v": 1,
                    "reply_to": first["id"],
                    "ev": "hello_ok",
                    "version": 1,
                    "server": "socketpair",
                }
            ).encode()
            + b"\n"
        )
        create = json.loads(reader.readline())
        received.put(create)
        server_socket.sendall(
            json.dumps(
                {
                    "v": 1,
                    "reply_to": create["id"],
                    "ev": "attached",
                    "session": {"session_id": "pair", "status": "idle"},
                }
            ).encode()
            + b"\n"
        )
        detach = json.loads(reader.readline())
        received.put(detach)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        with HarnessClient(_SocketTransport(client_socket)) as client:
            client.create_session()
        worker.join(timeout=1)
        requests = [received.get_nowait() for _ in range(3)]
    finally:
        server_socket.close()
    assert [request["req"] for request in requests] == [
        "hello",
        "create_session",
        "detach_session",
    ]


def test_invalid_turn_timeout_closes_transport() -> None:
    left, right = socket.socketpair()
    with pytest.raises(ValueError, match="turn_timeout"):
        HarnessClient(_SocketTransport(left), turn_timeout=0)
    assert left.fileno() == -1
    right.close()
