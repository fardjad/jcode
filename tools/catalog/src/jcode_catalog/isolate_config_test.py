"""Contract tests for repository config isolation."""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from .isolate_config import isolated_environment


class IsolateConfigTests(unittest.TestCase):
    def disposable_wrapper_environment(self):
        """Contain even interpreter startup caches before the wrapper runs."""
        directory = tempfile.TemporaryDirectory(prefix="jcode-isolation-fixture-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        home = root / "sentinel-home-that-must-not-be-inherited"
        home.mkdir()
        environment = os.environ.copy()
        environment["HOME"] = str(home)
        environment["JCODE_HOME"] = str(root / "sentinel-jcode-home-that-must-not-be-inherited")
        for variable in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
            environment[variable] = str(root / variable.lower())
        environment["PYTHONPYCACHEPREFIX"] = str(root / "python-cache")
        return root, environment

    def runtime_sentinel_environment(self):
        """Sentinel runtime/socket/temp paths that must never be inherited."""
        directory = tempfile.TemporaryDirectory(prefix="jcode-runtime-sentinel-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        sentinels = {
            "JCODE_RUNTIME_DIR": str(root / "sentinel-runtime-dir"),
            "XDG_RUNTIME_DIR": str(root / "sentinel-xdg-runtime-dir"),
            "JCODE_SOCKET": str(root / "sentinel-jcode-socket.sock"),
            "JCODE_DEBUG_CONTROL": str(root / "sentinel-debug-control"),
            "TMPDIR": str(root / "sentinel-tmpdir"),
            "TMP": str(root / "sentinel-tmp"),
            "TEMP": str(root / "sentinel-temp"),
        }
        return root, sentinels

    def inject_inherited(self, values):
        """Inject inherited values into os.environ, restoring afterwards."""
        previous = {key: os.environ.get(key) for key in values}

        def restore():
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        os.environ.update(values)

    def assert_unix_socket_round_trip(self, path):
        """Bind, connect, and exchange bytes on a private AF_UNIX socket."""
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with server, client:
            server.bind(str(path))
            server.listen(1)
            client.connect(str(path))
            connection, _ = server.accept()
            with connection:
                client.sendall(b"isolated")
                self.assertEqual(connection.recv(1024), b"isolated")

    def test_environment_overrides_inherited_runtime_and_temp_paths(self):
        root, sentinels = self.runtime_sentinel_environment()
        self.inject_inherited(sentinels)
        with isolated_environment() as isolated:
            for key in ("JCODE_RUNTIME_DIR", "XDG_RUNTIME_DIR", "JCODE_SOCKET",
                        "TMPDIR", "TMP", "TEMP"):
                self.assertEqual(os.environ[key], sentinels[key])
                self.assertNotEqual(isolated[key], sentinels[key])
            self.assertNotIn("JCODE_DEBUG_CONTROL", isolated)
            runtime = Path(isolated["JCODE_RUNTIME_DIR"])
            self.assertEqual(isolated["XDG_RUNTIME_DIR"], str(runtime))
            self.assertEqual(isolated["JCODE_SOCKET"], str(runtime / "jcode.sock"))
            self.assertTrue(runtime.is_dir())
            self.assertEqual(stat.S_IMODE(os.stat(runtime).st_mode), 0o700)
            # The logical debug socket jcode derives stays private, disposable,
            # and short enough for sockaddr_un.sun_path.
            debug_socket = runtime / "jcode-debug.sock"
            self.assertTrue(debug_socket.is_relative_to(runtime))
            self.assertLessEqual(len(os.fsencode(str(debug_socket))), 104)
            temp = Path(isolated["TMPDIR"])
            self.assertEqual(isolated["TMP"], str(temp))
            self.assertEqual(isolated["TEMP"], str(temp))
            self.assertTrue(temp.is_dir())
            self.assertEqual(stat.S_IMODE(os.stat(temp).st_mode), 0o700)
            # Every alternate runtime precedence candidate jcode consults
            # (JCODE_RUNTIME_DIR, XDG_RUNTIME_DIR, then TMPDIR, then the temp
            # fallback, which follows TMPDIR) is an actual disposable directory.
            for key in ("JCODE_RUNTIME_DIR", "XDG_RUNTIME_DIR", "TMPDIR"):
                self.assertTrue(Path(isolated[key]).is_dir())
            # No sentinel path was touched or created by the isolated run.
            for value in sentinels.values():
                self.assertFalse(Path(value).exists())
        # The caller's os.environ is unchanged by the wrapper.
        for key, value in sentinels.items():
            self.assertEqual(os.environ[key], value)

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "requires AF_UNIX sockets")
    def test_environment_binds_private_normal_and_debug_sockets(self):
        with isolated_environment() as isolated:
            runtime = Path(isolated["JCODE_RUNTIME_DIR"])
            for name in ("jcode.sock", "jcode-debug.sock"):
                socket_path = runtime / name
                self.assertLessEqual(len(os.fsencode(str(socket_path))), 104)
                self.assert_unix_socket_round_trip(socket_path)
            self.assertEqual(stat.S_IMODE(os.stat(runtime).st_mode), 0o700)
        self.assertFalse(runtime.exists())

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "requires AF_UNIX sockets")
    def test_environment_moves_runtime_when_caller_temp_base_is_long(self):
        with tempfile.TemporaryDirectory(prefix="jcode-long-temp-fixture-") as fixture:
            # A real caller temp base long enough that a disposable root under
            # it cannot host 104-byte AF_UNIX socket paths.
            base = Path(fixture) / ("long-caller-temp-" + "x" * 96)
            base.mkdir()
            overflow = len(str(base)) + len(
                "jc-XXXXXXXX/runtime/jcode-debug.sock"
            )
            self.assertGreater(overflow, 104)
            previous = os.environ.get("TMPDIR")
            previous_tempdir = tempfile.tempdir
            tempfile.tempdir = None
            os.environ["TMPDIR"] = str(base)
            try:
                with isolated_environment() as isolated:
                    runtime = Path(isolated["JCODE_RUNTIME_DIR"])
                    temp = Path(isolated["TMPDIR"])
                    self.assertEqual(isolated["XDG_RUNTIME_DIR"], str(runtime))
                    generic = Path("/private/tmp" if sys.platform == "darwin" else "/tmp")
                    # The long inherited TMPDIR is ignored entirely: the whole
                    # disposable root, not just the runtime, is short.
                    self.assertTrue(runtime.is_relative_to(generic))
                    self.assertTrue(temp.is_relative_to(generic))
                    self.assertFalse(runtime.is_relative_to(base))
                    self.assertFalse(temp.is_relative_to(base))
                    self.assertEqual(
                        stat.S_IMODE(os.stat(runtime.parent).st_mode), 0o700
                    )
                    self.assertEqual(stat.S_IMODE(os.stat(temp).st_mode), 0o700)
                    for name in ("jcode.sock", "jcode-debug.sock"):
                        socket_path = runtime / name
                        self.assertFalse(socket_path.is_relative_to(base))
                        self.assertLessEqual(len(os.fsencode(str(socket_path))), 104)
                        self.assert_unix_socket_round_trip(socket_path)
            finally:
                tempfile.tempdir = previous_tempdir
                if previous is None:
                    os.environ.pop("TMPDIR", None)
                else:
                    os.environ["TMPDIR"] = previous
        self.assertFalse(base.exists())

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "requires AF_UNIX sockets")
    def test_triple_nested_environments_bind_short_sockets_in_child_temp(self):
        generic = Path("/private/tmp" if sys.platform == "darwin" else "/tmp")
        levels = []
        with isolated_environment() as outer:
            with isolated_environment() as middle:
                with isolated_environment() as inner:
                    for isolated in (outer, middle, inner):
                        runtime = Path(isolated["JCODE_RUNTIME_DIR"])
                        temp = Path(isolated["TMPDIR"])
                        root = runtime.parent
                        # Every level shares the same short root layout and a
                        # fresh root of its own: nesting never compounds path
                        # lengths and never reuses another level's paths.
                        self.assertEqual(temp.parent, root)
                        if sys.platform != "win32":
                            self.assertEqual(root.parent, generic)
                        self.assertEqual(
                            stat.S_IMODE(os.stat(root).st_mode), 0o700
                        )
                        for name in ("jcode.sock", "jcode-debug.sock"):
                            socket_path = runtime / name
                            self.assertLessEqual(
                                len(os.fsencode(str(socket_path))), 104
                            )
                            self.assert_unix_socket_round_trip(socket_path)
                        levels.append((runtime, temp))
                    for index, (runtime, temp) in enumerate(levels):
                        for earlier_runtime, earlier_temp in levels[:index]:
                            self.assertFalse(runtime.is_relative_to(earlier_temp))
                            self.assertFalse(temp.is_relative_to(earlier_temp))
                    # A child process resolves temp against the innermost
                    # TMPDIR and binds a real AF_UNIX socket inside it.
                    child = (
                        "import pathlib, socket, tempfile\n"
                        "temp = pathlib.Path(tempfile.mkdtemp())\n"
                        "sock = temp / 'sock'\n"
                        "server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
                        "server.bind(str(sock))\n"
                        "server.close()\n"
                        "print(sock)\n"
                    )
                    result = subprocess.run(
                        [sys.executable, "-c", child],
                        check=True,
                        capture_output=True,
                        text=True,
                        env=inner,
                    )
                    child_socket = Path(result.stdout.strip())
                    self.assertTrue(child_socket.is_relative_to(levels[-1][1]))
                    self.assertLessEqual(
                        len(os.fsencode(str(child_socket))), 104
                    )
        # All three levels clean up, including the child's temp socket.
        for runtime, temp in levels:
            self.assertFalse(runtime.exists())
            self.assertFalse(temp.exists())
        self.assertFalse(child_socket.exists())

    def test_environment_cleans_runtime_roots_when_body_raises(self):
        captured = {}
        with self.assertRaisesRegex(ValueError, "boom"):
            with isolated_environment() as isolated:
                captured["runtime"] = isolated["JCODE_RUNTIME_DIR"]
                captured["socket"] = isolated["JCODE_SOCKET"]
                raise ValueError("boom")
        self.assertFalse(Path(captured["runtime"]).exists())
        self.assertFalse(Path(captured["socket"]).exists())

    def test_nested_environments_use_distinct_runtime_dirs_and_sockets(self):
        with isolated_environment() as outer:
            with isolated_environment() as inner:
                self.assertNotEqual(outer["JCODE_RUNTIME_DIR"], inner["JCODE_RUNTIME_DIR"])
                self.assertNotEqual(outer["XDG_RUNTIME_DIR"], inner["XDG_RUNTIME_DIR"])
                self.assertNotEqual(outer["JCODE_SOCKET"], inner["JCODE_SOCKET"])
                outer_runtime = Path(outer["JCODE_RUNTIME_DIR"])
                inner_runtime = Path(inner["JCODE_RUNTIME_DIR"])
                self.assertTrue(outer_runtime.is_dir())
                self.assertTrue(inner_runtime.is_dir())
            self.assertTrue(outer_runtime.exists())
            self.assertFalse(inner_runtime.exists())
        self.assertFalse(outer_runtime.exists())

    def test_wrapper_replaces_inherited_runtime_and_temp_paths(self):
        wrapper = Path(__file__).resolve().parents[4] / "tools/scripts/isolate_config.py"
        root, sentinels = self.runtime_sentinel_environment()
        Path(sentinels["TMPDIR"]).mkdir()
        environment = os.environ.copy()
        environment.update(sentinels)
        result = subprocess.run(
            [
                sys.executable,
                str(wrapper),
                "--",
                sys.executable,
                "-c",
                "import os, pathlib, stat\n"
                "runtime = os.environ['JCODE_RUNTIME_DIR']\n"
                "print(runtime)\n"
                "print(os.environ['XDG_RUNTIME_DIR'])\n"
                "print(os.environ['JCODE_SOCKET'])\n"
                "print(os.environ['TMPDIR'])\n"
                "print(os.environ['TMP'])\n"
                "print(os.environ['TEMP'])\n"
                "print(os.environ.get('JCODE_DEBUG_CONTROL', '<absent>'))\n"
                "print(oct(stat.S_IMODE(os.stat(runtime).st_mode)))\n"
                "print(pathlib.Path(os.environ['JCODE_SOCKET']).is_relative_to(runtime))\n",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            cwd=root,
        )
        (runtime, xdg_runtime, jcode_socket, tmpdir, tmp, temp,
         debug_control, mode, socket_inside_runtime) = result.stdout.splitlines()
        self.assertNotEqual(runtime, sentinels["JCODE_RUNTIME_DIR"])
        self.assertNotEqual(xdg_runtime, sentinels["XDG_RUNTIME_DIR"])
        self.assertNotEqual(jcode_socket, sentinels["JCODE_SOCKET"])
        self.assertEqual(xdg_runtime, runtime)
        self.assertEqual(jcode_socket, str(Path(runtime) / "jcode.sock"))
        self.assertEqual(debug_control, "<absent>")
        self.assertEqual(mode, "0o700")
        self.assertEqual(tmpdir, tmp)
        self.assertEqual(tmpdir, temp)
        self.assertNotEqual(tmpdir, sentinels["TMPDIR"])
        self.assertNotEqual(tmp, sentinels["TMP"])
        self.assertNotEqual(temp, sentinels["TEMP"])
        self.assertEqual(socket_inside_runtime, "True")
        debug_socket = Path(runtime) / "jcode-debug.sock"
        self.assertLessEqual(len(os.fsencode(str(debug_socket))), 104)
        # The disposable runtime the child saw is cleaned up with the child.
        self.assertFalse(Path(runtime).exists())

    def test_disposable_wrapper_fixture_cleans_startup_artifacts(self):
        root, environment = self.disposable_wrapper_environment()
        for variable in ("HOME", "JCODE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                         "XDG_DATA_HOME", "XDG_STATE_HOME", "PYTHONPYCACHEPREFIX"):
            self.assertTrue(Path(environment[variable]).is_relative_to(root))
        cache = Path(environment["HOME"]) / "Library" / "Caches" / "com.apple.python"
        cache.mkdir(parents=True)
        (cache / "fixture").write_text("test cache")
        self.doCleanups()
        self.assertFalse(root.exists())

    def test_wrapper_replaces_inherited_home_and_jcode_home(self):
        wrapper = Path(__file__).resolve().parents[4] / "tools/scripts/isolate_config.py"
        root, environment = self.disposable_wrapper_environment()
        result = subprocess.run(
            [
                sys.executable,
                str(wrapper),
                "--",
                sys.executable,
                "-c",
                "import os, pathlib; print(os.environ['HOME']); print(os.environ['JCODE_HOME']); print(pathlib.Path(os.environ['HOME']).is_dir())",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            cwd=root,
        )
        home, jcode_home, home_exists = result.stdout.splitlines()
        self.assertNotEqual(home, environment["HOME"])
        self.assertNotEqual(jcode_home, environment["JCODE_HOME"])
        self.assertEqual(home_exists, "True")
        self.assertTrue(jcode_home.endswith("/.jcode"))

    def test_wrapper_preserves_explicit_rust_toolchain_locations(self):
        wrapper = Path(__file__).resolve().parents[4] / "tools/scripts/isolate_config.py"
        root, environment = self.disposable_wrapper_environment()
        environment.update({
            "CARGO_HOME": str(root / "sentinel-cargo-home-that-may-be-inherited"),
            "RUSTUP_HOME": str(root / "sentinel-rustup-home-that-may-be-inherited"),
        })
        result = subprocess.run(
            [
                sys.executable,
                str(wrapper),
                "--",
                sys.executable,
                "-c",
                "import os; print(os.environ['CARGO_HOME']); print(os.environ['RUSTUP_HOME']); print(os.environ['HOME']); print(os.environ['JCODE_HOME'])",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
            cwd=root,
        )
        cargo_home, rustup_home, home, jcode_home = result.stdout.splitlines()
        self.assertEqual(cargo_home, environment["CARGO_HOME"])
        self.assertEqual(rustup_home, environment["RUSTUP_HOME"])
        self.assertNotEqual(home, environment["HOME"])
        self.assertNotEqual(jcode_home, environment["JCODE_HOME"])

    def test_environment_uses_disposable_home_and_xdg_directories(self):
        with tempfile.TemporaryDirectory() as original_home:
            inherited = {
                "HOME": original_home,
                "JCODE_HOME": str(Path(original_home) / ".jcode"),
                "XDG_CONFIG_HOME": str(Path(original_home) / ".config"),
                "XDG_CACHE_HOME": str(Path(original_home) / ".cache"),
                "XDG_DATA_HOME": str(Path(original_home) / ".local" / "share"),
                "XDG_STATE_HOME": str(Path(original_home) / ".local" / "state"),
            }
            previous = {key: os.environ.get(key) for key in inherited}
            try:
                os.environ.update(inherited)
                with isolated_environment() as isolated:
                    for key, value in inherited.items():
                        self.assertNotEqual(isolated[key], value)
                    for key in (
                        "HOME",
                        "XDG_CONFIG_HOME",
                        "XDG_CACHE_HOME",
                        "XDG_DATA_HOME",
                        "XDG_STATE_HOME",
                    ):
                        self.assertTrue(Path(isolated[key]).is_dir())
                    self.assertEqual(isolated["GIT_CONFIG_NOSYSTEM"], "1")
                    self.assertEqual(isolated["GIT_CONFIG_GLOBAL"], os.devnull)
                    self.assertEqual(isolated["GIT_AUTHOR_EMAIL"], "jcode-validation@example.invalid")
                    self.assertEqual(isolated["GIT_COMMITTER_EMAIL"], "jcode-validation@example.invalid")
                    self.assertFalse(Path(isolated["JCODE_HOME"], "config.toml").exists())
                    result = subprocess.run(
                        [sys.executable, "-c", "import os; print(os.environ['JCODE_WORKFLOW_CONFIG_ISOLATED'])"],
                        env=isolated,
                        check=True,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.stdout.strip(), "1")
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_environment_preserves_explicit_rust_toolchain_locations(self):
        with tempfile.TemporaryDirectory() as original_home, tempfile.TemporaryDirectory() as cargo_home, tempfile.TemporaryDirectory() as rustup_home:
            inherited = {
                "HOME": original_home,
                "CARGO_HOME": cargo_home,
                "RUSTUP_HOME": rustup_home,
            }
            previous = {key: os.environ.get(key) for key in inherited}
            try:
                os.environ.update(inherited)
                with isolated_environment() as isolated:
                    self.assertEqual(isolated["CARGO_HOME"], cargo_home)
                    self.assertEqual(isolated["RUSTUP_HOME"], rustup_home)
                    self.assertNotEqual(isolated["HOME"], original_home)
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_environment_derives_rust_toolchain_defaults_from_inherited_home(self):
        with tempfile.TemporaryDirectory() as original_home:
            inherited = {
                "HOME": original_home,
                "CARGO_HOME": None,
                "RUSTUP_HOME": None,
            }
            previous = {key: os.environ.get(key) for key in inherited}
            try:
                os.environ["HOME"] = original_home
                os.environ.pop("CARGO_HOME", None)
                os.environ.pop("RUSTUP_HOME", None)
                with isolated_environment() as isolated:
                    self.assertEqual(isolated["CARGO_HOME"], str(Path(original_home) / ".cargo"))
                    self.assertEqual(isolated["RUSTUP_HOME"], str(Path(original_home) / ".rustup"))
                    self.assertNotEqual(isolated["HOME"], original_home)
            finally:
                for key, value in previous.items():
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value

    def test_wrapper_rejects_empty_command(self):
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parents[4] / "tools/scripts/isolate_config.py")],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
