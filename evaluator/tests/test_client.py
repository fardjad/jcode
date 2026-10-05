from __future__ import annotations

import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeHarnessServer

from jcode_harness_client import (
    ConversationTurn,
    HarnessConnectionError,
    HarnessProtocolError,
    HarnessRequestError,
    HarnessResult,
    SessionBusyError,
    SessionSettlingError,
    UnsupportedCapabilityError,
)


def test_unix_transport_reassembles_split_ndjson_and_skips_unknown_event(
    tmp_path: Path,
) -> None:
    server = FakeHarnessServer(
        unix_path=tmp_path / "api.sock",
        split_frames=True,
        on_message=lambda _content, sid, send: (
            send({"v": 1, "ev": "future_event", "session_id": sid}),
            FakeHarnessServer._default_message(_content, sid, send),
        ),
    )
    with server:
        with server.client() as client:
            session = client.create_session(working_dir=tmp_path)
            result = session.send_message("hello")

    assert result == HarnessResult(text="reply", session_id="session-1")
    create = next(
        request for request in server.requests if request["req"] == "create_session"
    )
    assert create["working_dir"] == str(tmp_path)
    assert "system_prompt" not in create
    sent = next(
        request for request in server.requests if request["req"] == "send_message"
    )
    assert sent["content"] == "hello"
    assert "no_reply" not in sent


def test_tcp_transport_accepts_newer_minor_server_and_unknown_capabilities(
    fake_server: FakeHarnessServer,
) -> None:
    with fake_server.client() as client:
        assert client.server == "fake/1"
        assert client.capabilities == (
            "sessions",
            "future_capability",
            "initial_history",
        )
        assert client.send_message("hello").text == "reply"


def test_create_session_serializes_history_in_order(
    fake_server: FakeHarnessServer,
) -> None:
    history = [
        ConversationTurn("user", "My name is Ada. 世界"),
        ConversationTurn("assistant", "Nice to meet you, Ada."),
    ]
    with fake_server.client() as client:
        session = client.create_session(history=history)
    create = next(
        request
        for request in fake_server.requests
        if request["req"] == "create_session"
    )
    assert create["initial_history"] == [
        {"role": "user", "content": "My name is Ada. 世界"},
        {"role": "assistant", "content": "Nice to meet you, Ada."},
    ]
    assert session.session_id == "session-1"


def test_none_and_empty_history_keep_legacy_create_payload(
    fake_server: FakeHarnessServer,
) -> None:
    with fake_server.client() as client:
        client.create_session(history=None)
        client.create_session(history=[])
    creates = [
        request
        for request in fake_server.requests
        if request["req"] == "create_session"
    ]
    assert all("initial_history" not in request for request in creates)


def test_history_requires_capability_before_create_request() -> None:
    with FakeHarnessServer(capabilities=["sessions", "future_capability"]) as server:
        with server.client() as client:
            with pytest.raises(UnsupportedCapabilityError, match="initial_history"):
                client.create_session(
                    history=[
                        ConversationTurn("user", "Hi"),
                        ConversationTurn("assistant", "Hello"),
                    ]
                )
    assert [request["req"] for request in server.requests] == ["hello"]


@pytest.mark.parametrize(
    "history",
    [
        [ConversationTurn("assistant", "wrong"), ConversationTurn("user", "wrong")],
        [ConversationTurn("user", "missing answer")],
        [ConversationTurn("user", "one"), ConversationTurn("user", "two")],
        [
            ConversationTurn("system", "unsupported"),
            ConversationTurn("assistant", "answer"),
        ],
        [ConversationTurn("user", ""), ConversationTurn("assistant", "answer")],
        [{"role": "user", "content": "extra", "images": []}],
        [{"role": "user", "content": 1}, {"role": "assistant", "content": "answer"}],
    ],
)
def test_invalid_history_is_rejected_before_create_request(history: object) -> None:
    with FakeHarnessServer() as server:
        with server.client() as client:
            with pytest.raises((TypeError, ValueError)):
                client.create_session(history=history)  # type: ignore[arg-type]
    assert [request["req"] for request in server.requests] == ["hello"]


def test_history_exact_boundaries_and_over_limits() -> None:
    exact = [
        ConversationTurn(role, "x" * (64 * 1024))
        for _ in range(8)
        for role in ("user", "assistant")
    ]
    too_large = exact + [ConversationTurn("user", "overflow")]
    with FakeHarnessServer() as server:
        with server.client() as client:
            client.create_session(history=exact)
            with pytest.raises(ValueError, match="100"):
                client.create_session(
                    history=[
                        ConversationTurn(role, "x")
                        for _ in range(101)
                        for role in ("user", "assistant")
                    ]
                )
            with pytest.raises(ValueError, match="1 MiB"):
                client.create_session(history=too_large)
    assert (
        len(
            [
                request
                for request in server.requests
                if request["req"] == "create_session"
            ]
        )
        == 1
    )


def test_seed_create_errors_and_connection_closure_return_no_session() -> None:
    with FakeHarnessServer(
        create_error={
            "ev": "error",
            "code": "internal",
            "message": "seed persistence failed",
        }
    ) as server:
        with server.client() as client:
            with pytest.raises(HarnessRequestError, match="seed persistence failed"):
                client.create_session(
                    history=[
                        ConversationTurn("user", "Hi"),
                        ConversationTurn("assistant", "Hello"),
                    ]
                )
    with FakeHarnessServer(close_on_create=True) as server:
        with server.client() as client:
            with pytest.raises(HarnessConnectionError):
                client.create_session(
                    history=[
                        ConversationTurn("user", "Hi"),
                        ConversationTurn("assistant", "Hello"),
                    ]
                )


def test_handshake_rejects_version_error_and_incompatible_major() -> None:

    with FakeHarnessServer(hello_error=True) as server:
        with pytest.raises(HarnessRequestError, match="unsupported_version"):
            server.client()

    with FakeHarnessServer(hello_version=2) as server:
        with pytest.raises(HarnessProtocolError, match="major version 2"):
            server.client()


def test_api_error_reply_to_request_is_raised_before_turn_wait() -> None:
    server = FakeHarnessServer()

    # Inject a direct matched Error after a valid handshake.
    def serve(connection: Any) -> None:
        reader = connection.makefile("rb")
        try:
            for raw in reader:
                frame = json.loads(raw)
                server.requests.append(frame)
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
                            "ev": "error",
                            "code": "invalid_request",
                            "message": "create rejected",
                        },
                    )
        finally:
            connection.close()

    server._serve = serve  # type: ignore[method-assign]
    with server:
        with server.client() as client:
            with pytest.raises(HarnessRequestError, match="create rejected"):
                client.create_session()


def test_text_replacements_retractions_and_multiple_message_ids(
    fake_server: FakeHarnessServer,
) -> None:
    def reply(_content: str, sid: str, send: Any) -> None:
        send(
            {
                "v": 1,
                "ev": "text_delta",
                "session_id": sid,
                "message_id": "a",
                "text": "old",
            }
        )
        send(
            {
                "v": 1,
                "ev": "text_replace",
                "session_id": sid,
                "message_id": "a",
                "text": "first",
            }
        )
        send({"v": 1, "ev": "text_done", "session_id": sid, "message_id": "a"})
        send(
            {
                "v": 1,
                "ev": "text_replace",
                "session_id": sid,
                "message_id": "a",
                "text": "final first",
            }
        )
        send(
            {
                "v": 1,
                "ev": "text_delta",
                "session_id": sid,
                "message_id": "b",
                "text": "discard",
            }
        )
        send(
            {
                "v": 1,
                "ev": "text_replace",
                "session_id": sid,
                "message_id": "b",
                "text": "",
            }
        )
        send(
            {
                "v": 1,
                "ev": "text_delta",
                "session_id": sid,
                "message_id": "b",
                "text": "second",
            }
        )
        send({"v": 1, "ev": "text_done", "session_id": sid, "message_id": "b"})
        send(
            {
                "v": 1,
                "ev": "text_replace",
                "session_id": sid,
                "message_id": "b",
                "text": "final second",
            }
        )
        send({"v": 1, "ev": "turn_done", "session_id": sid})

    fake_server.on_message = reply
    with fake_server.client() as client:
        result = client.create_session().send_message("assemble")
    assert result.text == "final firstfinal second"


def test_abnormal_stop_drains_error_then_done_and_returns_partial_result(
    fake_server: FakeHarnessServer,
) -> None:
    def failure(_content: str, sid: str, send: Any) -> None:
        send(
            {
                "v": 1,
                "ev": "text_delta",
                "session_id": sid,
                "message_id": "m",
                "text": "partial",
            }
        )
        send(
            {
                "v": 1,
                "ev": "turn_stopped",
                "session_id": sid,
                "reason": "provider_guardrail",
                "message": "blocked",
                "provider_stop_reason": "refusal",
            }
        )
        send(
            {
                "v": 1,
                "reply_to": next(
                    request["id"]
                    for request in fake_server.requests
                    if request["req"] == "send_message"
                    and request["content"] == "prompt"
                ),
                "ev": "error",
                "code": "internal",
                "message": "provider failed",
            }
        )
        send({"v": 1, "ev": "turn_done", "session_id": sid})

    fake_server.on_message = failure
    with fake_server.client() as client:
        session = client.create_session()
        result = session.send_message("prompt")

    assert result.text == "partial"
    assert result.abnormal
    assert result.stop_reason == "provider_guardrail"
    assert result.stop_message == "blocked"
    assert result.provider_stop_reason == "refusal"
    assert result.error_code == "internal"
    assert result.error_message == "provider failed"
    assert result.as_dict() == {"role": "assistant", "content": "partial"}
    assert result.stop_info == {
        "reason": "provider_guardrail",
        "message": "blocked",
        "provider_stop_reason": "refusal",
    }


def test_unattributed_error_without_stopped_turn_fails_as_request_error(
    fake_server: FakeHarnessServer,
) -> None:
    fake_server.on_message = lambda _content, _sid, send: send(
        {"v": 1, "ev": "error", "code": "internal", "message": "connection-level"}
    )
    with fake_server.client() as client:
        session = client.create_session()
        with pytest.raises(HarnessRequestError, match="connection-level"):
            session.send_message("prompt")


def test_turn_timeout_cancels_discards_stale_events_and_gates_session(
    fake_server: FakeHarnessServer,
) -> None:
    delayed: queue.Queue[tuple[str, Any]] = queue.Queue()

    def delay_first(content: str, sid: str, send: Any) -> None:
        if content == "slow":
            delayed.put((sid, send))
            return
        FakeHarnessServer._default_message(content, sid, send)

    fake_server.on_message = delay_first
    with fake_server.client(turn_timeout=0.08) as client:
        session = client.create_session()
        with pytest.raises(TimeoutError):
            session.send_message("slow")
        deadline = time.monotonic() + 1
        while not any(request["req"] == "cancel" for request in fake_server.requests):
            if time.monotonic() >= deadline:
                break
            time.sleep(0.005)
        assert any(request["req"] == "cancel" for request in fake_server.requests)
        sid, send = delayed.get(timeout=1)
        with pytest.raises(SessionSettlingError):
            session.send_message("too soon")

        send(
            {
                "v": 1,
                "ev": "text_delta",
                "session_id": sid,
                "message_id": "old",
                "text": "stale",
            }
        )
        send(
            {
                "v": 1,
                "ev": "turn_stopped",
                "session_id": sid,
                "reason": "interrupted",
                "message": "cancelled",
            }
        )
        send({"v": 1, "ev": "error", "code": "internal", "message": "cancelled"})
        send({"v": 1, "ev": "turn_done", "session_id": sid})
        deadline = time.monotonic() + 1
        while session._settling and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not session._settling
        result = session.send_message("fresh")
        assert result.text == "reply"


def test_session_rejects_overlap_and_distinct_thread_ids_run_concurrently(
    fake_server: FakeHarnessServer,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def block(_content: str, sid: str, send: Any) -> None:
        started.set()
        release.wait(timeout=3)
        FakeHarnessServer._default_message("", sid, send)

    fake_server.on_message = block
    with fake_server.client() as client:
        session = client.create_session()
        thread = threading.Thread(target=lambda: session.send_message("blocking"))
        thread.start()
        assert started.wait(timeout=1)
        with pytest.raises(SessionBusyError):
            session.send_message("overlap")
        release.set()
        thread.join(timeout=1)

        results: list[str] = []
        workers = [
            threading.Thread(
                target=lambda key=key: results.append(
                    client.send_message("parallel", thread_id=key).text
                )
            )
            for key in ("a", "b")
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
    assert results == ["reply", "reply"]


def test_thread_id_reuse_binding_and_omitted_id_fresh_sessions(
    fake_server: FakeHarnessServer,
    tmp_path: Path,
) -> None:
    with fake_server.client() as client:
        first = client.send_message("one", thread_id="case", working_dir=tmp_path)
        second = client.send_message("two", thread_id="case")
        assert first.session_id == second.session_id
        with pytest.raises(ValueError, match="bound to working_dir"):
            client.send_message(
                "conflict", thread_id="case", working_dir=tmp_path / "other"
            )
        fresh_one = client.send_message("fresh one")
        fresh_two = client.send_message("fresh two")
    assert fresh_one.session_id != fresh_two.session_id
    creates = [
        request
        for request in fake_server.requests
        if request["req"] == "create_session"
    ]
    assert len(creates) == 3


def test_omitted_thread_id_calls_concurrently_create_distinct_sessions(
    fake_server: FakeHarnessServer,
) -> None:
    results: list[HarnessResult] = []
    errors: list[BaseException] = []

    with fake_server.client() as client:

        def send() -> None:
            try:
                results.append(client.send_message("fresh", thread_id="shared"))
            except BaseException as error:
                errors.append(error)

        workers = [threading.Thread(target=send) for _ in range(2)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=2)
    assert not errors
    assert len(results) == 2
    assert results[0].session_id == results[1].session_id


def test_close_sends_best_effort_detach_for_each_created_session(
    fake_server: FakeHarnessServer,
) -> None:
    client = fake_server.client()
    client.create_session()
    client.create_session()
    client.close()
    deadline = time.monotonic() + 1
    while (
        sum(request["req"] == "detach_session" for request in fake_server.requests) < 2
    ):
        if time.monotonic() >= deadline:
            break
        time.sleep(0.005)
    detaches = [
        request
        for request in fake_server.requests
        if request["req"] == "detach_session"
    ]
    assert len(detaches) == 2


def test_transport_close_fails_waiting_turn(fake_server: FakeHarnessServer) -> None:
    started = threading.Event()
    never = threading.Event()

    def block(_content: str, _sid: str, _send: Any) -> None:
        started.set()
        never.wait(timeout=2)

    fake_server.on_message = block
    client = fake_server.client()
    session = client.create_session()
    errors: list[BaseException] = []

    def send() -> None:
        try:
            session.send_message("wait")
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=send)
    worker.start()
    assert started.wait(timeout=1)
    client.close()
    worker.join(timeout=1)
    assert errors and isinstance(errors[0], HarnessConnectionError)
