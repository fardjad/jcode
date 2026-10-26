"""Offline tests for the advisory's daily cache and failure boundaries."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace
import urllib.error

import pytest

from . import nextest_updates, validation_state
from .bootstrap_nextest import VERSION as PINNED
from .patch_catalog import CatalogError

major, minor, patch = map(int, PINNED.split("."))
NEWER = f"{major}.{minor}.{patch + 1}"
NOTICE = (
    f"cargo-nextest {NEWER} is available (pinned: {PINNED}); "
    "update the catalog pin explicitly.\n"
)


@pytest.fixture
def advisory(tmp_path, monkeypatch):
    state = SimpleNamespace(
        root=tmp_path,
        now=1_000_000.0,
        calls=[],
        payload={"tag_name": f"cargo-nextest-{NEWER}", "draft": False, "prerelease": False},
        raw=None,
        error=None,
        before_request=None,
    )
    state.cache = tmp_path / ".tools" / "nextest" / "update-check.json"
    state.lock = state.cache.with_name("update-check.lock")
    monkeypatch.setattr(nextest_updates.time, "time", lambda: state.now)

    def urlopen(request, *, timeout):
        state.calls.append((request, timeout))
        if state.before_request is not None:
            state.before_request()
        if state.error is not None:
            raise state.error
        text = state.raw if state.raw is not None else json.dumps(state.payload)
        return io.StringIO(text)

    monkeypatch.setattr(nextest_updates.urllib.request, "urlopen", urlopen)
    return state


def test_first_check_uses_official_endpoint_timeout_and_stderr_only(advisory, capsys):
    nextest_updates.check(advisory.root, PINNED)

    assert len(advisory.calls) == 1
    request, timeout = advisory.calls[0]
    assert request.full_url == "https://api.github.com/repos/nextest-rs/nextest/releases/latest"
    assert request.get_header("User-agent") == nextest_updates.USER_AGENT
    assert timeout == 3
    assert json.loads(advisory.cache.read_text()) == {
        "checked_at": advisory.now,
        "latest": NEWER,
    }
    assert advisory.lock.exists()
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == NOTICE


@pytest.mark.parametrize("elapsed", [0, 1, 86399, 86399.999])
def test_cached_check_suppresses_requests_and_repeat_notices(advisory, capsys, elapsed):
    nextest_updates.check(advisory.root, PINNED)
    assert capsys.readouterr().err == NOTICE
    original = advisory.cache.read_bytes()
    advisory.now += elapsed

    nextest_updates.check(advisory.root, PINNED)

    assert len(advisory.calls) == 1
    assert advisory.cache.read_bytes() == original
    output = capsys.readouterr()
    assert output.out == output.err == ""


@pytest.mark.parametrize("elapsed", [86400, 86401, 172800])
def test_exact_daily_boundary_and_stale_cache_retry(advisory, capsys, elapsed):
    nextest_updates.check(advisory.root, PINNED)
    capsys.readouterr()
    advisory.now += elapsed

    nextest_updates.check(advisory.root, PINNED)

    assert len(advisory.calls) == 2
    assert json.loads(advisory.cache.read_text())["checked_at"] == advisory.now
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == NOTICE


@pytest.mark.parametrize("error", [TimeoutError("timeout"), urllib.error.URLError("offline")])
def test_failed_request_is_cached_and_retried_only_when_stale(advisory, capsys, error):
    advisory.error = error
    nextest_updates.check(advisory.root, PINNED)
    assert json.loads(advisory.cache.read_text()) == {"checked_at": advisory.now}
    advisory.now += 86399
    nextest_updates.check(advisory.root, PINNED)
    assert len(advisory.calls) == 1
    advisory.now += 1
    nextest_updates.check(advisory.root, PINNED)
    assert len(advisory.calls) == 2
    output = capsys.readouterr()
    assert output.out == output.err == ""


@pytest.mark.parametrize(
    ("pinned", "latest", "notice"),
    [
        ("0.9.99", "0.9.100", True),
        ("0.9.100", "0.9.99", False),
        ("0.9.143", "0.9.143", False),
        ("0.9.143", "0.10.0", True),
        ("0.10.0", "0.9.999", False),
        ("0.9.143", "1.0.0", True),
    ],
)
def test_versions_are_compared_as_numeric_tuples(advisory, capsys, pinned, latest, notice):
    advisory.payload["tag_name"] = f"cargo-nextest-{latest}"
    nextest_updates.check(advisory.root, pinned)
    assert len(advisory.calls) == 1
    output = capsys.readouterr()
    assert output.out == ""
    expected = (
        f"cargo-nextest {latest} is available (pinned: {pinned}); "
        "update the catalog pin explicitly.\n"
    )
    assert output.err == (expected if notice else "")


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "cargo-nextest-0.9.144",
        {},
        {"tag_name": "cargo-nextest-0.9.144", "draft": True, "prerelease": False},
        {"tag_name": "cargo-nextest-0.9.144", "draft": False, "prerelease": True},
        {"tag_name": "cargo-nextest-0.9.144", "draft": 0, "prerelease": False},
        {"tag_name": "cargo-nextest-0.9.144", "draft": False, "prerelease": 0},
        {"tag_name": "cargo-nextest-0.9.144", "prerelease": False},
        {"tag_name": "cargo-nextest-0.9.144", "draft": False},
        {"tag_name": None, "draft": False, "prerelease": False},
        {"tag_name": 144, "draft": False, "prerelease": False},
        {"tag_name": "cargo-nextest-0.9.144-rc.1", "draft": False, "prerelease": False},
        {"tag_name": "cargo-nextest-0.9.144+build", "draft": False, "prerelease": False},
        {"tag_name": "v0.9.144", "draft": False, "prerelease": False},
        {"tag_name": "prefix-cargo-nextest-0.9.144", "draft": False, "prerelease": False},
        {"tag_name": "cargo-nextest-0.9.144\n", "draft": False, "prerelease": False},
        {"tag_name": "cargo-nextest-0.9", "draft": False, "prerelease": False},
    ],
)
def test_unstable_or_malformed_payload_is_silent_and_throttled(advisory, capsys, payload):
    advisory.payload = payload
    nextest_updates.check(advisory.root, PINNED)
    assert json.loads(advisory.cache.read_text()) == {"checked_at": advisory.now}
    nextest_updates.check(advisory.root, PINNED)
    assert len(advisory.calls) == 1
    output = capsys.readouterr()
    assert output.out == output.err == ""


@pytest.mark.parametrize("raw", ["", "not json", "{\"tag_name\":"])
def test_bad_response_json_is_nonfatal_and_throttled(advisory, capsys, raw):
    advisory.raw = raw
    nextest_updates.check(advisory.root, PINNED)
    nextest_updates.check(advisory.root, PINNED)
    assert len(advisory.calls) == 1
    assert json.loads(advisory.cache.read_text()) == {"checked_at": advisory.now}
    output = capsys.readouterr()
    assert output.out == output.err == ""


@pytest.mark.parametrize(
    "raw",
    [
        "garbage",
        "[]",
        "null",
        "{}",
        '{"checked_at": true}',
        '{"checked_at": false}',
        '{"checked_at": "1000000"}',
        '{"checked_at": null}',
        '{"checked_at": []}',
        '{"checked_at": -1}',
        '{"checked_at": NaN}',
        '{"checked_at": Infinity}',
        '{"checked_at": -Infinity}',
        '{"checked_at": ' + "1" + "0" * 400 + "}",
    ],
)
def test_corrupt_or_invalid_timestamp_cache_recovers_atomically(advisory, capsys, raw):
    advisory.cache.parent.mkdir(parents=True)
    advisory.cache.write_text(raw)
    writes = []
    atomic_json = nextest_updates.execution.atomic_json

    def record_atomic_json(path, payload):
        writes.append((path, payload.copy()))
        atomic_json(path, payload)

    monkeypatch = pytest.MonkeyPatch()
    with monkeypatch.context() as patch:
        patch.setattr(nextest_updates.execution, "atomic_json", record_atomic_json)
        nextest_updates.check(advisory.root, PINNED)

    assert len(advisory.calls) == 1
    assert writes == [
        (advisory.cache, {"checked_at": advisory.now}),
        (advisory.cache, {"checked_at": advisory.now, "latest": NEWER}),
    ]
    assert json.loads(advisory.cache.read_text())["latest"] == NEWER
    assert not list(advisory.cache.parent.glob(".update-check.json.*"))
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == NOTICE


@pytest.mark.parametrize("checked_at", [1_000_001, 1_086_400.0, 2_000_000])
def test_future_timestamp_suppresses_clock_rollback_retries(advisory, capsys, checked_at):
    advisory.cache.parent.mkdir(parents=True)
    advisory.cache.write_text(json.dumps({"checked_at": checked_at, "latest": NEWER}))
    original = advisory.cache.read_bytes()
    nextest_updates.check(advisory.root, PINNED)
    assert advisory.calls == []
    assert advisory.cache.read_bytes() == original
    output = capsys.readouterr()
    assert output.out == output.err == ""


@pytest.mark.parametrize("checked_at", [0, 0.0, 913600, 913600.0])
def test_valid_nonnegative_numeric_timestamp_can_be_stale(advisory, checked_at):
    advisory.cache.parent.mkdir(parents=True)
    advisory.cache.write_text(json.dumps({"checked_at": checked_at}))
    nextest_updates.check(advisory.root, PINNED)
    assert len(advisory.calls) == 1


def test_cache_write_failure_prevents_network_request(advisory, monkeypatch, capsys):
    def fail_write(path, payload):
        raise PermissionError("read-only cache")

    monkeypatch.setattr(nextest_updates.execution, "atomic_json", fail_write)
    nextest_updates.check(advisory.root, PINNED)
    assert advisory.calls == []
    assert not advisory.cache.exists()
    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_contended_existing_os_lock_silently_skips_request(advisory, capsys):
    with validation_state.exclusive_lock(advisory.lock, "test owner"):
        owner_record = advisory.lock.read_bytes()
        nextest_updates.check(advisory.root, PINNED)
        assert advisory.lock.read_bytes() == owner_record
        assert advisory.calls == []
        assert not advisory.cache.exists()
    assert advisory.lock.exists()
    nextest_updates.check(advisory.root, PINNED)
    assert len(advisory.calls) == 1
    assert advisory.lock.exists()
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == NOTICE


def test_timestamp_is_written_and_lock_is_held_before_request(advisory):
    def before_request():
        assert json.loads(advisory.cache.read_text()) == {"checked_at": advisory.now}
        with pytest.raises(CatalogError, match="already running"):
            with validation_state.exclusive_lock(advisory.lock, "competitor"):
                pytest.fail("advisory released its lock during request")

    advisory.before_request = before_request
    nextest_updates.check(advisory.root, PINNED)
    assert len(advisory.calls) == 1
    assert json.loads(advisory.cache.read_text())["latest"] == NEWER


def test_attempt_timestamp_survives_process_exit_and_throttles_retry(advisory, capsys):
    advisory.error = SystemExit(17)
    with pytest.raises(SystemExit) as error:
        nextest_updates.check(advisory.root, PINNED)
    assert error.value.code == 17
    assert json.loads(advisory.cache.read_text()) == {"checked_at": advisory.now}
    advisory.error = None
    nextest_updates.check(advisory.root, PINNED)
    assert len(advisory.calls) == 1
    assert advisory.lock.exists()
    output = capsys.readouterr()
    assert output.out == output.err == ""


def test_failure_to_persist_success_does_not_repeat_request(advisory, monkeypatch, capsys):
    atomic_json = nextest_updates.execution.atomic_json

    def write_attempt_only(path, payload):
        if "latest" in payload:
            raise OSError("cache became unavailable")
        atomic_json(path, payload)

    monkeypatch.setattr(nextest_updates.execution, "atomic_json", write_attempt_only)
    nextest_updates.check(advisory.root, PINNED)
    nextest_updates.check(advisory.root, PINNED)
    assert len(advisory.calls) == 1
    assert json.loads(advisory.cache.read_text()) == {"checked_at": advisory.now}
    output = capsys.readouterr()
    assert output.out == output.err == ""
