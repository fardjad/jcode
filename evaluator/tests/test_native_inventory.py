from __future__ import annotations

import pytest
from conftest import FakeHarnessServer

from jcode_harness_client import (
    HarnessProtocolError,
    HarnessRequestError,
)


def test_native_list_sessions_and_tools_preserve_additive_fields() -> None:
    with FakeHarnessServer() as server, server.client() as client:
        session = client.create_session()
        sessions = client.list_sessions()
        tools = client.list_tools(session.session_id)
    assert sessions[0].session_id == session.session_id
    assert sessions[0].swarm_status == "ready"
    assert sessions[0].edit_stats == {
        "added": 0,
        "removed": 0,
        "approximate": False,
    }
    assert sessions[0].raw["future_session_field"] == {"retained": True}
    assert tools[0].name == "read"
    assert tools[0].raw["parameters"] == {"type": "object"}
    assert tools[0].raw["future_tool_field"] == [1, 2, 3]
    list_request = next(
        request for request in server.requests if request["req"] == "list_sessions"
    )
    assert list_request["include_archived"] is True
    assert "limit" not in list_request


def test_native_inventory_api_errors_are_not_silently_ignored() -> None:
    responses = {
        "list_sessions": {
            "ev": "error",
            "code": "busy",
            "message": "session inventory busy",
        }
    }
    with (
        FakeHarnessServer(request_responses=responses) as server,
        server.client() as client,
    ):
        with pytest.raises(HarnessRequestError, match="busy"):
            client.list_sessions()


@pytest.mark.parametrize(
    "response",
    [
        {"ev": "tools", "session_id": "wrong", "tools": []},
        {"ev": "tools", "session_id": "session-1", "tools": [{"parameters": {}}]},
        {"ev": "ok", "session_id": "session-1", "tools": []},
    ],
)
def test_malformed_native_tool_inventory_is_rejected(response: dict) -> None:
    with (
        FakeHarnessServer(request_responses={"list_tools": response}) as server,
        server.client() as client,
    ):
        session = client.create_session()
        with pytest.raises(HarnessProtocolError):
            client.list_tools(session.session_id)
