"""Bootstrap CLI contract tests using fake cached binaries and offline requests."""
from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from . import bootstrap_nextest, nextest_updates, run_nextest
from .bootstrap_nextest import VERSION as PINNED

major, minor, patch = map(int, PINNED.split("."))
NEWER = f"{major}.{minor}.{patch + 1}"


@pytest.fixture
def cached_bootstrap(tmp_path, monkeypatch):
    source = tmp_path / "tools/catalog/src/jcode_catalog/bootstrap_nextest.py"
    monkeypatch.setattr(bootstrap_nextest, "__file__", str(source))
    monkeypatch.setattr(bootstrap_nextest, "target_name", lambda: "test-target")
    monkeypatch.setattr(bootstrap_nextest.platform, "system", lambda: "Linux")
    monkeypatch.setattr(nextest_updates.time, "time", lambda: 1_000_000.0)
    binary = tmp_path / ".tools" / "nextest" / PINNED / "test-target" / "cargo-nextest"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"fake pinned executable, never actually run")
    state = SimpleNamespace(
        root=tmp_path,
        binary=binary,
        events=[],
        error=None,
        latest=NEWER,
        reported=f"cargo-nextest {PINNED}\n",
        cache=tmp_path / ".tools/nextest/update-check.json",
    )

    def urlopen(request, *, timeout):
        state.events.append("advisory")
        assert request.full_url == nextest_updates.LATEST_URL
        assert timeout == 3
        assert json.loads(state.cache.read_text()) == {"checked_at": 1_000_000.0}
        if state.error is not None:
            raise state.error
        return io.StringIO(json.dumps({
            "tag_name": f"cargo-nextest-{state.latest}",
            "draft": False,
            "prerelease": False,
        }))

    def run(command, *, capture_output, text, check):
        state.events.append("cached version")
        assert command == [str(binary), "--version"]
        assert capture_output is text is check is True
        assert state.cache.exists(), "advisory must precede cached version check"
        return SimpleNamespace(stdout=state.reported)

    def no_download():
        pytest.fail("cached bootstrap must not download releases")

    monkeypatch.setattr(nextest_updates.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(bootstrap_nextest.subprocess, "run", run)
    monkeypatch.setattr(bootstrap_nextest, "releases", no_download)
    return state


def test_cached_bootstrap_stdout_is_binary_path_and_pin_is_unchanged(cached_bootstrap, capsys):
    state = cached_bootstrap
    protected = {
        state.binary: state.binary.read_bytes(),
        state.root / "VERSION.txt": b"v0.99.1\n",
        state.root / ".tools/nextest/exclusions/base.json": b'{"failed": []}\n',
        state.root / ".tools/patch-tests/base/results.json": b'{"passed": []}\n',
    }
    for path, content in protected.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    assert bootstrap_nextest.main() == 0

    output = capsys.readouterr()
    assert output.out == f"{state.binary}\n"
    assert output.err == (
        f"cargo-nextest {NEWER} is available (pinned: {PINNED}); "
        "update the catalog pin explicitly.\n"
    )
    assert state.events == ["advisory", "cached version"]
    assert bootstrap_nextest.VERSION == PINNED
    for path, content in protected.items():
        assert path.read_bytes() == content
    assert not (state.binary.parents[2] / NEWER).exists()

    assert bootstrap_nextest.main() == 0
    output = capsys.readouterr()
    assert output.out == f"{state.binary}\n"
    assert output.err == ""
    assert state.events == ["advisory", "cached version", "cached version"]


@pytest.mark.parametrize("error", [TimeoutError("offline"), OSError("connection refused")])
def test_advisory_failure_keeps_cached_bootstrap_successful(cached_bootstrap, capsys, error):
    state = cached_bootstrap
    state.error = error
    assert bootstrap_nextest.main() == 0
    output = capsys.readouterr()
    assert output.out == f"{state.binary}\n"
    assert output.err == ""
    assert state.events == ["advisory", "cached version"]
    assert bootstrap_nextest.VERSION == PINNED
    assert json.loads(state.cache.read_text()) == {"checked_at": 1_000_000.0}


def test_no_newer_release_keeps_bootstrap_stderr_empty(cached_bootstrap, capsys):
    state = cached_bootstrap
    state.latest = PINNED
    assert bootstrap_nextest.main() == 0
    output = capsys.readouterr()
    assert output.out == f"{state.binary}\n"
    assert output.err == ""


def test_runner_reexports_authoritative_bootstrap_pin():
    assert run_nextest.NEXTEST_VERSION == bootstrap_nextest.VERSION == PINNED


@pytest.mark.parametrize("reported", [
    f"cargo-nextest {PINNED}\n",
    f"cargo-nextest {PINNED} (abc123 2026-10-20)\n",
])
def test_cached_bootstrap_accepts_exact_version(cached_bootstrap, capsys, reported):
    state = cached_bootstrap
    state.latest = PINNED
    state.reported = reported
    assert bootstrap_nextest.main() == 0
    output = capsys.readouterr()
    assert output.out == f"{state.binary}\n"
    assert output.err == ""


@pytest.mark.parametrize("reported", [
    "",
    "cargo-nextest\n",
    "cargo-nextest 0.9.143\n",
    f"cargo-nextest {PINNED}0\n",
    f"cargo-nextest {PINNED}-rc.1\n",
    f"cargo-nextest {PINNED}+build\n",
    f"other-program {PINNED}\n",
    f"cargo-nextest {NEWER} (previous: {PINNED})\n",
])
def test_cached_bootstrap_rejects_nonexact_version(cached_bootstrap, capsys, reported):
    state = cached_bootstrap
    state.latest = PINNED
    state.reported = reported
    assert bootstrap_nextest.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == (
        f"bootstrap-nextest: cached cargo-nextest reports unexpected version: {reported.strip()}\n"
    )
    assert state.events == ["advisory", "cached version"]


def test_advisory_is_invoked_once_before_uncached_download(tmp_path, monkeypatch, capsys):
    source = tmp_path / "tools/catalog/src/jcode_catalog/bootstrap_nextest.py"
    monkeypatch.setattr(bootstrap_nextest, "__file__", str(source))
    monkeypatch.setattr(bootstrap_nextest, "target_name", lambda: "test-target")
    monkeypatch.setattr(bootstrap_nextest.platform, "system", lambda: "Linux")
    events = []

    def check(root, pinned):
        assert root == tmp_path
        assert pinned == PINNED
        events.append("advisory")

    def releases():
        assert events == ["advisory"]
        events.append("download")
        raise RuntimeError("offline test stops before pinned binary download")

    def no_network(*args, **kwargs):
        pytest.fail("test must never contact a real endpoint")

    monkeypatch.setattr(nextest_updates, "check", check)
    monkeypatch.setattr(bootstrap_nextest, "releases", releases)
    monkeypatch.setattr(bootstrap_nextest.urllib.request, "urlopen", no_network)

    assert bootstrap_nextest.main() == 1
    assert events == ["advisory", "download"]
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "bootstrap-nextest: offline test stops before pinned binary download\n"
    assert bootstrap_nextest.VERSION == PINNED
