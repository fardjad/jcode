"""Launch an isolated real jcode daemon, bridge, relay, and fake completion API."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jcode", type=Path, required=True)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--port", type=int, default=18765)
    parser.add_argument("--mock-port", type=int, default=18766)
    return parser.parse_args()


def _wait_for_path(path: Path, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"{process.args!r} exited with status {process.returncode}"
            )
        if path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.connect(str(path))
                return
            except OSError:
                pass
            finally:
                probe.close()
        time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for socket {path}")


def _wait_for_tcp(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"{process.args!r} exited with status {process.returncode}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"timed out waiting for TCP relay on port {port}")


def _capture_stderr(
    process: subprocess.Popen[bytes],
) -> tuple[list[str], threading.Thread]:
    """Capture child diagnostics until exit for success-aware reporting."""
    assert process.stderr is not None
    lines: list[str] = []

    def collect() -> None:
        for raw_line in process.stderr:
            lines.append(raw_line.decode(errors="replace").rstrip("\r\n"))

    thread = threading.Thread(target=collect, daemon=True)
    thread.start()
    return lines, thread


def _report_diagnostics(
    lines: list[str], *, success: bool, expected: tuple[str, ...]
) -> None:
    for line in lines:
        if success and any(fragment in line for fragment in expected):
            continue
        print(line, file=sys.stderr, flush=True)


def main() -> int:
    args = _arguments()
    jcode = args.jcode.resolve(strict=True)
    bridge = args.bridge.resolve(strict=True)
    socat = shutil.which("socat")
    if socat is None:
        raise SystemExit("socat is required for the optional TCP relay smoke test")

    root = Path(os.environ["JCODE_HOME"])
    root.mkdir(parents=True, exist_ok=True)
    runtime = root / "smoke-runtime"
    config_dir = root / "config" / "jcode"
    data_dir = root / "data"
    state_dir = root / "state"
    cache_dir = root / "cache"
    workspace = root / "workspace"
    for directory in (runtime, config_dir, data_dir, state_dir, cache_dir, workspace):
        directory.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.update(
        {
            "JCODE_HOME": str(root),
            "JCODE_RUNTIME_DIR": str(runtime),
            "XDG_RUNTIME_DIR": str(runtime),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(data_dir),
            "XDG_STATE_HOME": str(state_dir),
            "XDG_CACHE_HOME": str(cache_dir),
            "JCODE_MOCK_PORT": str(args.mock_port),
            "JCODE_SMOKE_WORKING_DIR": str(workspace),
            "JCODE_API_HOST": "127.0.0.1",
            "JCODE_API_PORT": str(args.port),
            "PYTHONPATH": os.pathsep.join(
                filter(
                    None,
                    (
                        str(Path(__file__).resolve().parents[1]),
                        os.environ.get("PYTHONPATH"),
                    ),
                )
            ),
        }
    )
    config_path = root / "config.toml"
    config_path.write_text(
        f"""[tools]
enabled = ["read"]

[providers.smoke]
type = "openai-compatible"
base_url = "http://127.0.0.1:{args.mock_port}/v1"
auth = "none"
default_model = "smoke-model"
""",
        encoding="utf-8",
    )
    config_dir.joinpath("config.toml").write_text(
        config_path.read_text(encoding="utf-8"), encoding="utf-8"
    )

    daemon_socket = runtime / "daemon.sock"
    api_socket = runtime / "api.sock"
    processes: list[subprocess.Popen[bytes]] = []
    captured_stderr: list[tuple[list[str], threading.Thread, tuple[str, ...]]] = []
    smoke_return_code = 1
    try:
        processes.append(
            subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("mock_completions.py"))],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=None,
            )
        )
        _wait_for_tcp(args.mock_port, processes[-1])
        processes.append(
            subprocess.Popen(
                [
                    str(jcode),
                    "--no-update",
                    "--provider-profile",
                    "smoke",
                    "--model",
                    "smoke-model",
                    "--socket",
                    str(daemon_socket),
                    "serve",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=None,
            )
        )
        _wait_for_path(daemon_socket, processes[-1])
        processes.append(
            subprocess.Popen(
                [str(bridge), str(api_socket), str(daemon_socket)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        )
        bridge_stderr, bridge_thread = _capture_stderr(processes[-1])
        captured_stderr.append(
            (
                bridge_stderr,
                bridge_thread,
                ("client ended: Broken pipe (os error 32)",),
            )
        )
        _wait_for_path(api_socket, processes[-1])
        processes.append(
            subprocess.Popen(
                [
                    socat,
                    f"TCP-LISTEN:{args.port},bind=127.0.0.1,reuseaddr,fork",
                    f"UNIX-CONNECT:{api_socket}",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        )
        socat_stderr, socat_thread = _capture_stderr(processes[-1])
        captured_stderr.append(
            (socat_stderr, socat_thread, ("W exiting on signal 15",))
        )
        _wait_for_tcp(args.port, processes[-1])
        smoke_client = Path(__file__).with_name("client_smoke.py")
        result = subprocess.run(
            [sys.executable, str(smoke_client)], env=env, check=False
        )
        smoke_return_code = result.returncode
        if smoke_return_code == 0:
            print("Smoke test passed: received the expected reply.", flush=True)
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
        for process in reversed(processes):
            if process.poll() is None:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        for lines, thread, expected in captured_stderr:
            thread.join(timeout=1)
            _report_diagnostics(
                lines,
                success=smoke_return_code == 0,
                expected=expected,
            )
    return smoke_return_code


if __name__ == "__main__":
    raise SystemExit(main())
