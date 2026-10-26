"""Durable write rollback and owned-process termination contracts."""
import json
from pathlib import Path
import signal
import subprocess
import tempfile
from unittest.mock import Mock, patch

import pytest
from .execution import atomic_json, stop_child


def test_atomic_json_failed_replace_preserves_previous_record():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "status.json"
        path.write_text('{"old": true}')
        with patch("jcode_catalog.execution.os.replace", side_effect=OSError("disk error")):
            with pytest.raises(OSError):
                atomic_json(path, {"new": True})
        assert json.loads(path.read_text()) == {"old": True}
        assert list(Path(directory).iterdir()) == [path]


def test_owned_child_waits_after_terminate_then_kills_on_timeout():
    child = Mock(pid=123)
    child.wait.side_effect = [subprocess.TimeoutExpired("fixture", 5), 0]
    with patch("jcode_catalog.execution.os.name", "posix"), \
         patch("jcode_catalog.execution.os.killpg") as kill:
        stop_child(child)
    assert kill.call_args_list[0].args == (123, signal.SIGTERM)
    assert kill.call_args_list[1].args == (123, signal.SIGKILL)
    assert child.wait.call_count == 2
