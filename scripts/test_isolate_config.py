"""Contract tests for repository config isolation."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from isolate_config import isolated_environment


class IsolateConfigTests(unittest.TestCase):
    def test_wrapper_replaces_inherited_home_and_jcode_home(self):
        wrapper = Path(__file__).with_name("isolate_config.py")
        environment = os.environ.copy()
        environment["HOME"] = "/sentinel-home-that-must-not-be-inherited"
        environment["JCODE_HOME"] = "/sentinel-jcode-home-that-must-not-be-inherited"
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
        )
        home, jcode_home, home_exists = result.stdout.splitlines()
        self.assertNotEqual(home, environment["HOME"])
        self.assertNotEqual(jcode_home, environment["JCODE_HOME"])
        self.assertEqual(home_exists, "True")
        self.assertTrue(jcode_home.endswith("/.jcode"))

    def test_wrapper_preserves_explicit_rust_toolchain_locations(self):
        wrapper = Path(__file__).with_name("isolate_config.py")
        environment = os.environ.copy()
        environment.update({
            "HOME": "/sentinel-home-that-must-not-be-inherited",
            "JCODE_HOME": "/sentinel-jcode-home-that-must-not-be-inherited",
            "CARGO_HOME": "/sentinel-cargo-home-that-may-be-inherited",
            "RUSTUP_HOME": "/sentinel-rustup-home-that-may-be-inherited",
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
            [sys.executable, str(Path(__file__).with_name("isolate_config.py"))],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
