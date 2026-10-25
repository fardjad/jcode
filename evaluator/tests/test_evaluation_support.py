"""Shared runtime isolation, shutdown retention, and correlated event regressions."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from conftest import FakeHarnessServer

from jcode_harness_client.runtime import launch_harness, runtime_environment


def test_environment_drops_personal_routing_and_replaces_explicit_home(
    tmp_path: Path,
) -> None:
    with patch.dict(
        os.environ,
        {
            "HOME": "/forbidden",
            "JCODE_HOME": "/forbidden",
            "XDG_CONFIG_HOME": "/forbidden",
            "JCODE_PROVIDER_ENV_FILE": "/forbidden",
            "OPENAI_API_KEY": "must-not-inherit",
        },
    ):
        env = runtime_environment(
            tmp_path, {"HOME": "/forbidden", "EXPLICIT_KEY": "test"}
        )
    assert env["HOME"] == str(tmp_path / "home")
    assert env["JCODE_HOME"] == str(tmp_path / "jcode")
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "config")
    assert "OPENAI_API_KEY" not in env
    assert "JCODE_PROVIDER_ENV_FILE" not in env
    assert env["EXPLICIT_KEY"] == "test"


def test_startup_failure_cleans_children_and_disposable_home(tmp_path: Path) -> None:
    binary = tmp_path / "binary"
    binary.write_text("fixture")
    stopped: list[bool] = []
    created_roots: list[Path] = []
    with (
        patch("jcode_harness_client.runtime.subprocess.Popen") as spawn,
        patch(
            "jcode_harness_client.runtime._wait_socket",
            side_effect=RuntimeError("startup"),
        ),
        patch("jcode_harness_client.runtime._stop", return_value=False) as stop,
    ):
        with pytest.raises(RuntimeError, match="startup"):
            with launch_harness(
                jcode=binary,
                bridge=binary,
                config="",
                profile="fixture",
                model="fixture",
                workspace=tmp_path,
                created=created_roots.append,
                stopped=lambda: stopped.append(created_roots[0].exists()),
            ):
                pytest.fail("failed startup must not yield a runtime")
        stop.assert_called_once_with(spawn.return_value)
        env = spawn.call_args.kwargs["env"]
        assert not Path(env["HOME"]).exists()
        assert not Path(env["JCODE_HOME"]).exists()
    assert stopped == [True]


@pytest.mark.parametrize("abnormal", [False, True])
def test_event_capture_and_sink_keep_generic_contract(abnormal: bool) -> None:
    def reply(_content: str, sid: str, send: Any) -> None:
        for event in [
            {"ev": "tool_start", "call_id": "call", "name": "read"},
            {"ev": "tool_done", "call_id": "call", "name": "read", "output": "fixture"},
            {"ev": "text_delta", "text": "reply", "message_id": "one"},
            {"ev": "text_done", "message_id": "one"},
        ]:
            send({"v": 1, "session_id": sid, **event})
        if abnormal:
            send(
                {
                    "v": 1,
                    "session_id": sid,
                    "ev": "turn_stopped",
                    "reason": "failure",
                    "message": "fixture stop",
                }
            )
        send({"v": 1, "ev": "turn_done", "session_id": sid})

    with FakeHarnessServer(on_message=reply) as server, server.client() as client:
        default = client.send_message("First")
        sink: list[dict] = []
        captured = client.send_message("Second", capture_events=True, event_sink=sink)
        assert default.events == ()
        assert captured.as_dict() == {"role": "assistant", "content": "reply"}
        assert captured.abnormal == abnormal
        assert list(captured.events) == sink
        assert [e["ev"] for e in captured.events if e["ev"].startswith("tool_")] == [
            "tool_start",
            "tool_done",
        ]
        assert all(e.get("session_id") == captured.session_id for e in captured.events)
        assert captured.events[-1]["ev"] == "turn_done"


def test_stopped_callback_runs_before_disposable_root_deletion(tmp_path: Path) -> None:
    binary = tmp_path / "binary"
    binary.write_text("fixture")
    observed: list[Path] = []
    with (
        patch("jcode_harness_client.runtime.subprocess.Popen"),
        patch("jcode_harness_client.runtime._wait_socket"),
        patch("jcode_harness_client.runtime._stop", return_value=False),
        patch("jcode_harness_client.runtime.HarnessClient.unix") as unix,
    ):
        unix.return_value.__enter__.return_value = object()
        with launch_harness(
            jcode=binary,
            bridge=binary,
            config="",
            profile="fixture",
            model="fixture",
            workspace=tmp_path,
            stopped=lambda: observed.append(runtime.root / "sessions"),
        ) as runtime:
            (runtime.root / "sessions").mkdir()
        assert observed == [runtime.root / "sessions"]
        assert not runtime.root.exists()
