"""Tests for tools/scripts/setup_assets.py.

The suite exercises symlink installs, copy installs, metadata tracking,
refusals, and rollback using disposable fake repositories and jcode homes.
Best-effort Windows path behavior is asserted via os-agnostic relative path
keys; this suite does not claim native Windows validation.
"""

from __future__ import annotations

import contextlib
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock as mock
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import setup_assets  # noqa: E402

SCRIPT = SCRIPT_DIR / "setup_assets.py"

BLUEPRINTS = ("coordinator.md", "fixer.md", "investigator.md", "research.md")

# Exact copy of the real catalog plugins/.gitignore (asserted by
# IgnoreRuleTests.test_fixture_uses_exact_real_gitignore_form).
GITIGNORE_TEXT = (
    "# Python bytecode generated when validating or running plugin adapters.\n"
    "__pycache__/\n"
    "*.py[cod]\n"
    "\n"
    "# Editor and macOS metadata.\n"
    ".DS_Store\n"
    "*.swp\n"
    "\n"
    "# Plugin allowlist: every plugin directory needs a matching !/name/ entry\n"
    "# here, otherwise its contents stay ignored and are never copied by\n"
    "# tools/scripts/setup_assets.py.\n"
    "/*/\n"
    "!/rtk/\n"
    "!/delegation-guard/\n"
)

# Exact copy of the real catalog skills/.gitignore (asserted by
# IgnoreRuleTests.test_skills_fixture_gitignore_matches_real).
SKILLS_GITIGNORE_TEXT = (
    "# Python bytecode generated when validating or running skills.\n"
    "__pycache__/\n"
    "*.py[cod]\n"
    "\n"
    "# Editor and macOS metadata.\n"
    ".DS_Store\n"
    "*.swp\n"
    "\n"
    "# Skill allowlist: add a matching !/name/ entry for each shipped skill.\n"
    "/*\n"
    "!/.gitignore\n"
    "!/author-plan/\n"
)


def make_executable(path: Path) -> None:
    if os.name == "posix":
        os.chmod(path, 0o755)


def make_repo(root: Path) -> Path:
    """Build a fake catalog with shipped plugins and skills, un-allowlisted
    local plugin and skill directories, and junk."""
    plugins = root / "plugins"
    (plugins / "rtk").mkdir(parents=True)
    (plugins / "rtk" / "rtk-transform").write_text("#!/bin/sh\necho rtk\n")
    make_executable(plugins / "rtk" / "rtk-transform")
    (plugins / "rtk" / "test_rtk_transform.py").write_text("rtk tests\n")
    (plugins / "rtk" / "nested").mkdir()
    (plugins / "rtk" / "nested" / "deep.txt").write_text("nested code\n")
    (plugins / "rtk" / "__pycache__").mkdir()
    (plugins / "rtk" / "__pycache__" / "junk.cpython-311.pyc").write_text("junk\n")
    (plugins / "delegation-guard").mkdir()
    (plugins / "delegation-guard" / "delegation-guard-transform").write_text(
        "#!/bin/sh\necho guard\n"
    )
    make_executable(plugins / "delegation-guard" / "delegation-guard-transform")
    (plugins / "delegation-guard" / "test_transform.py").write_text("guard tests\n")
    (plugins / "README.md").write_text("plugin docs\n")
    (plugins / ".gitignore").write_text(GITIGNORE_TEXT)
    # Local plugin directory without an allowlist entry: ignored, never copied.
    (plugins / "local-tool").mkdir()
    (plugins / "local-tool" / "local-tool-transform").write_text("ignored plugin\n")
    (plugins / "__pycache__").mkdir()
    (plugins / "__pycache__" / "junk.cpython-311.pyc").write_text("junk\n")
    blueprints = root / "workers" / "blueprints"
    blueprints.mkdir(parents=True)
    for name in BLUEPRINTS:
        (blueprints / name).write_text(f"{name} blueprint\n")
    skills = root / "skills"
    skills.mkdir()
    (skills / "author-plan").mkdir()
    (skills / "author-plan" / "SKILL.md").write_text("author-plan skill\n")
    (skills / ".gitignore").write_text(SKILLS_GITIGNORE_TEXT)
    # Local skill directory without an allowlist entry: ignored, never copied.
    (skills / "local-skill").mkdir()
    (skills / "local-skill" / "SKILL.md").write_text("ignored skill\n")
    (skills / "__pycache__").mkdir()
    (skills / "__pycache__" / "junk.cpython-311.pyc").write_text("junk\n")
    return root


class SetupAssetsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="jcode-setup-assets-test-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = make_repo(self.root / "repo")
        self.home = self.root / "home"
        self.home.mkdir(parents=True)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def run_setup(self, argv: list[str], home: Path | None = None) -> int:
        """Run setup_assets.main in-process against the fake repo."""
        environment = os.environ.copy()
        environment["JCODE_HOME"] = str(home or self.home)
        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(setup_assets, "REPO_ROOT", self.repo):
                return setup_assets.main(argv)

    def copy_mode_metadata(self) -> dict:
        return json.loads((self.home / setup_assets.METADATA_NAME).read_text())

    def reset_capture(self) -> None:
        self.out = io.StringIO()
        self.err = io.StringIO()

    def assert_plugins_payload(self, root: Path) -> None:
        self.assertTrue((root / "rtk" / "rtk-transform").is_file())
        self.assertTrue((root / "delegation-guard" / "delegation-guard-transform").is_file())
        self.assertTrue((root / "README.md").is_file())

    def assert_plugins_copy_excludes_ignored(self, root: Path) -> None:
        # Only copied trees can exclude ignored content; whole-directory
        # symlinks intentionally expose the repository exactly as it is.
        self.assertFalse((root / "agent-sudo").exists())
        self.assertFalse((root / "local-tool").exists())
        self.assertFalse((root / "__pycache__").exists())
        self.assertFalse((root / "rtk" / "__pycache__").exists())
        self.assertFalse((root / ".gitignore").exists())

    def assert_blueprints_payload(self, root: Path) -> None:
        for name in BLUEPRINTS:
            self.assertTrue((root / name).is_file())

    def assert_skills_payload(self, root: Path) -> None:
        self.assertTrue((root / "author-plan" / "SKILL.md").is_file())
        self.assertEqual(
            "author-plan skill\n",
            (root / "author-plan" / "SKILL.md").read_text(),
        )


class SymlinkInstallTests(SetupAssetsTestCase):
    def test_initial_symlinks_and_idempotence(self):
        self.assertEqual(0, self.run_setup([]))
        plugins = self.home / "plugins"
        blueprints = self.home / "worker-blueprints"
        self.assertTrue(plugins.is_symlink())
        self.assertTrue(blueprints.is_symlink())
        self.assertEqual((self.repo / "plugins").resolve(), plugins.resolve())
        self.assertEqual(
            (self.repo / "workers" / "blueprints").resolve(), blueprints.resolve()
        )
        self.assert_plugins_payload(plugins)
        self.assert_blueprints_payload(blueprints)
        first_target = os.readlink(plugins)
        # Rerun is idempotent and leaves the same symlinks in place.
        self.assertEqual(0, self.run_setup([]))
        self.assertTrue(plugins.is_symlink())
        self.assertEqual(first_target, os.readlink(plugins))
        self.assertIn("already linked", self.out.getvalue())

    def test_symlink_unsupported_falls_back_to_copy_for_all_assets(self):
        unsupported = OSError(errno.EPERM, "Operation not permitted")
        with mock.patch.object(setup_assets.os, "symlink", side_effect=unsupported):
            code = self.run_setup([])
        self.assertEqual(0, code)
        self.assertIn("not supported", self.out.getvalue())
        self.assertIn("copying", self.out.getvalue())
        for name in ("plugins", "worker-blueprints", "skills"):
            dest = self.home / name
            self.assertTrue(dest.is_dir())
            self.assertFalse(dest.is_symlink())
        self.assert_plugins_payload(self.home / "plugins")
        self.assert_blueprints_payload(self.home / "worker-blueprints")
        self.assert_skills_payload(self.home / "skills")
        self.assertTrue((self.home / setup_assets.METADATA_NAME).is_file())
        self.assert_plugins_copy_excludes_ignored(self.home / "plugins")
        self.assertFalse((self.home / "skills" / "local-skill").exists())

    def test_copy_mode_stays_copy_and_preserves_unmanaged_local_plugin(self):
        self.assertEqual(0, self.run_setup(["--copy"]))
        local = self.home / "plugins" / "my-local-plugin"
        local.mkdir()
        (local / "mine.txt").write_text("user content")
        self.assertEqual(0, self.run_setup([]))
        for name in ("plugins", "worker-blueprints"):
            self.assertFalse((self.home / name).is_symlink())
        self.assertEqual("user content", (local / "mine.txt").read_text())
        self.assertTrue((self.home / setup_assets.METADATA_NAME).exists())

    def test_unrelated_and_broken_symlinks_refused_without_partial_mutation(self):
        unrelated = self.root / "unrelated-plugins"
        unrelated.mkdir()
        (self.home / "plugins").symlink_to(unrelated)
        broken = self.root / "nowhere"
        (self.home / "worker-blueprints").symlink_to(broken)
        code = self.run_setup([])
        self.assertNotEqual(0, code)
        self.assertIn("unrelated symlink", self.err.getvalue())
        self.assertIn("broken symlink", self.err.getvalue())
        # Preflight refuses both assets before mutating either.
        self.assertTrue((self.home / "plugins").is_symlink())
        self.assertTrue((self.home / "worker-blueprints").is_symlink())
        self.assertEqual(unrelated.resolve(), (self.home / "plugins").resolve())


class CopyInstallTests(SetupAssetsTestCase):
    def test_forced_copies_record_metadata_outside_trees(self):
        self.assertEqual(0, self.run_setup(["--copy"]))
        plugins = self.home / "plugins"
        blueprints = self.home / "worker-blueprints"
        for dest in (plugins, blueprints):
            self.assertTrue(dest.is_dir())
            self.assertFalse(dest.is_symlink())
        self.assert_plugins_payload(plugins)
        self.assert_blueprints_payload(blueprints)
        if os.name == "posix":
            self.assertTrue(os.access(plugins / "rtk" / "rtk-transform", os.X_OK))
        metadata = self.copy_mode_metadata()
        self.assertEqual(
            {"plugins", "worker-blueprints", "skills"}, set(metadata["assets"])
        )
        expected_sources = {
            "plugins": "plugins",
            "worker-blueprints": "workers/blueprints",
            "skills": "skills",
        }
        for name, asset in metadata["assets"].items():
            self.assertEqual(str(self.repo / expected_sources[name]), asset["source"])
            self.assertTrue(asset["files"])
            for rel, digest in asset["files"].items():
                self.assertNotIn("\\", rel)
                self.assertEqual(64, len(digest))
        self.assertEqual(
            plugins / "rtk" / "rtk-transform",
            self.home / "plugins" / "rtk" / "rtk-transform",
        )
        self.assertFalse((plugins / setup_assets.METADATA_NAME).exists())
        self.assertFalse((blueprints / setup_assets.METADATA_NAME).exists())
        self.assertFalse((self.home / "skills" / setup_assets.METADATA_NAME).exists())

    def test_copy_upgrades_additions_and_removals(self):
        self.assertEqual(0, self.run_setup(["--copy"]))
        # Upstream adds a plugin file and a nested file (with the required
        # allowlist entry), removes a tracked (non-required) file, and changes
        # a managed file's content.
        new_plugin = self.repo / "plugins" / "newplugin"
        (new_plugin / "sub").mkdir(parents=True)
        (new_plugin / "tool.sh").write_text("new tool\n")
        (new_plugin / "sub" / "inner.txt").write_text("inner\n")
        gitignore = self.repo / "plugins" / ".gitignore"
        gitignore.write_text(gitignore.read_text() + "!/newplugin/\n")
        (self.repo / "plugins" / "rtk" / "test_rtk_transform.py").unlink()
        (self.repo / "plugins" / "rtk" / "rtk-transform").write_text("rtk v2\n")
        self.assertEqual(0, self.run_setup(["--copy"]))
        plugins = self.home / "plugins"
        self.assertTrue((plugins / "newplugin" / "tool.sh").is_file())
        self.assertTrue((plugins / "newplugin" / "sub" / "inner.txt").is_file())
        self.assertEqual("rtk v2\n", (plugins / "rtk" / "rtk-transform").read_text())
        self.assertFalse((plugins / "rtk" / "test_rtk_transform.py").exists())
        files = self.copy_mode_metadata()["assets"]["plugins"]["files"]
        self.assertIn("newplugin/tool.sh", files)
        self.assertIn("newplugin/sub/inner.txt", files)
        self.assertNotIn("rtk/test_rtk_transform.py", files)

    def test_copy_refuses_edited_managed_file_and_leaves_other_asset_untouched(self):
        self.assertEqual(0, self.run_setup(["--copy"]))
        edited = self.home / "plugins" / "rtk" / "rtk-transform"
        edited.write_text("local edit\n")
        before = (self.home / "worker-blueprints" / "fixer.md").read_text()
        self.reset_capture()
        code = self.run_setup(["--copy"])
        self.assertNotEqual(0, code)
        self.assertIn("file changed since it was copied", self.err.getvalue())
        self.assertIn("Back up or move", self.err.getvalue())
        self.assertEqual("local edit\n", edited.read_text())
        # Preflight covers both assets, so nothing was mutated at all.
        self.assertEqual(before, (self.home / "worker-blueprints" / "fixer.md").read_text())

    def test_copy_refuses_extra_file_inside_managed_directory(self):
        self.assertEqual(0, self.run_setup(["--copy"]))
        extra = self.home / "plugins" / "rtk" / "local-extra.txt"
        extra.write_text("user note\n")
        self.reset_capture()
        code = self.run_setup(["--copy"])
        self.assertNotEqual(0, code)
        self.assertIn("extra file inside managed directory", self.err.getvalue())
        self.assertTrue(extra.exists())

    def test_copy_refuses_ignored_extra_files_inside_managed_directory(self):
        """Extras with excluded names are invisible to the payload scan but
        an update would delete them with the backup tree, so they must block
        the update in preflight like any other local file."""
        self.assertEqual(0, self.run_setup(["--copy"]))
        swp = self.home / "plugins" / "rtk" / "notes.swp"
        swp.write_text("editor scratch\n")
        pycache = self.home / "plugins" / "rtk" / "__pycache__"
        pycache.mkdir()
        pyc = pycache / "adapter.cpython-311.pyc"
        pyc.write_text("bytecode\n")
        (self.home / "plugins" / "rtk" / ".DS_Store").write_text("mac meta\n")
        before = self.copy_mode_metadata()
        blueprints_before = (self.home / "worker-blueprints" / "fixer.md").read_text()
        (self.repo / "plugins" / "rtk" / "rtk-transform").write_text("rtk v2\n")
        self.reset_capture()
        code = self.run_setup(["--copy"])
        self.assertNotEqual(0, code)
        err = self.err.getvalue()
        self.assertIn("rtk/notes.swp", err)
        self.assertIn("rtk/__pycache__/adapter.cpython-311.pyc", err)
        self.assertIn("rtk/.DS_Store", err)
        self.assertIn("extra file inside managed directory", err)
        self.assertIn("Back up or move", err)
        # Preflight refusal: neither asset was mutated, no local file deleted.
        self.assertEqual("editor scratch\n", swp.read_text())
        self.assertEqual("bytecode\n", pyc.read_text())
        self.assertEqual(
            "#!/bin/sh\necho rtk\n",
            (self.home / "plugins" / "rtk" / "rtk-transform").read_text(),
        )
        self.assertEqual(
            blueprints_before,
            (self.home / "worker-blueprints" / "fixer.md").read_text(),
        )
        self.assertEqual(before, self.copy_mode_metadata())

    def test_copy_preserves_unmanaged_entries_with_ignored_names(self):
        """Ignored-named local content under an unmanaged top-level entry is
        still preserved (and never blocks) during an update."""
        self.assertEqual(0, self.run_setup(["--copy"]))
        unmanaged = self.home / "plugins" / "myplugin"
        unmanaged.mkdir()
        (unmanaged / "notes.swp").write_text("scratch\n")
        (unmanaged / "__pycache__").mkdir()
        (unmanaged / "__pycache__" / "x.pyc").write_text("bytecode\n")
        (self.repo / "plugins" / "README.md").write_text("updated docs\n")
        self.reset_capture()
        self.assertEqual(0, self.run_setup(["--copy"]))
        self.assertEqual("scratch\n", (unmanaged / "notes.swp").read_text())
        self.assertEqual(
            "bytecode\n", (unmanaged / "__pycache__" / "x.pyc").read_text()
        )

    def test_metadata_write_failure_rolls_back_existing_copy_update(self):
        self.assertEqual(0, self.run_setup(["--copy"]))
        metadata_path = self.home / setup_assets.METADATA_NAME
        before_text = metadata_path.read_text()
        (self.repo / "plugins" / "rtk" / "rtk-transform").write_text("rtk v2\n")
        self.reset_capture()
        with mock.patch.object(
            setup_assets,
            "write_metadata",
            side_effect=OSError("simulated metadata disk full"),
        ):
            code = self.run_setup(["--copy"])
        self.assertNotEqual(0, code)
        self.assertIn("simulated metadata disk full", self.err.getvalue())
        # The staged swap was rolled back: the destination holds the previous
        # copy and the metadata file still records the previous installation.
        self.assertEqual(
            "#!/bin/sh\necho rtk\n",
            (self.home / "plugins" / "rtk" / "rtk-transform").read_text(),
        )
        self.assertEqual(before_text, metadata_path.read_text())
        for name in ("plugins", "worker-blueprints", "skills"):
            self.assertTrue((self.home / name).is_dir())
        self.assertFalse(any(
            p.name.startswith(("plugins.staging-", "plugins.backup-"))
            for p in self.home.iterdir()
        ))

    def test_second_asset_failure_keeps_first_asset_updated_rolls_back_second(self):
        """The whole run is not atomic by design: each asset is one staged,
        rolled-back unit. A failure during the second asset's update leaves
        the first asset refreshed and restores the second exactly."""
        self.assertEqual(0, self.run_setup(["--copy"]))
        before = self.copy_mode_metadata()
        (self.repo / "plugins" / "rtk" / "rtk-transform").write_text("rtk v2\n")
        (self.repo / "workers" / "blueprints" / "fixer.md").write_text("fixer v2\n")
        real_write = setup_assets.write_metadata
        calls = []

        def flaky_write(path, metadata):
            calls.append(path)
            if len(calls) >= 2:
                raise OSError("simulated second-asset failure")
            real_write(path, metadata)

        self.reset_capture()
        with mock.patch.object(
            setup_assets, "write_metadata", side_effect=flaky_write
        ):
            code = self.run_setup(["--copy"])
        self.assertNotEqual(0, code)
        self.assertIn("simulated second-asset failure", self.err.getvalue())
        # First asset stays refreshed.
        self.assertEqual(
            "rtk v2\n",
            (self.home / "plugins" / "rtk" / "rtk-transform").read_text(),
        )
        # Second asset rolled back to its prior copy and metadata entry.
        self.assertEqual(
            "fixer.md blueprint\n",
            (self.home / "worker-blueprints" / "fixer.md").read_text(),
        )
        metadata = self.copy_mode_metadata()
        self.assertEqual(
            before["assets"]["worker-blueprints"],
            metadata["assets"]["worker-blueprints"],
        )
        self.assertEqual(
            setup_assets.content_hash(
                self.repo / "plugins" / "rtk" / "rtk-transform"
            ),
            metadata["assets"]["plugins"]["files"]["rtk/rtk-transform"],
        )
        self.assertFalse(any(
            p.name.startswith((
                "worker-blueprints.staging-",
                "worker-blueprints.backup-",
            ))
            for p in self.home.iterdir()
        ))

    def test_copy_refuses_symlink_in_source_payload_and_changes_nothing(self):
        """Staging a copy would follow a source symlink and record the
        target's content hash under the link path; preflight rejects the
        whole run instead, leaving both installed assets untouched."""
        self.assertEqual(0, self.run_setup(["--copy"]))
        before = self.copy_mode_metadata()
        external = self.root / "outside-catalog" / "secret.txt"
        external.parent.mkdir(parents=True)
        external.write_text("external payload\n")
        link = self.repo / "plugins" / "rtk" / "linked-config"
        link.symlink_to(external)
        (self.repo / "workers" / "blueprints" / "fixer.md").write_text("fixer v2\n")
        self.reset_capture()
        code = self.run_setup(["--copy"])
        self.assertNotEqual(0, code)
        err = self.err.getvalue()
        self.assertIn("source payload contains symlinks", err)
        self.assertIn("rtk/linked-config", err)
        # Nothing followed the symlink and neither asset was mutated.
        self.assertFalse((self.home / "plugins" / "linked-config").exists())
        self.assertEqual(
            "#!/bin/sh\necho rtk\n",
            (self.home / "plugins" / "rtk" / "rtk-transform").read_text(),
        )
        self.assertEqual(
            "fixer.md blueprint\n",
            (self.home / "worker-blueprints" / "fixer.md").read_text(),
        )
        self.assertEqual(before, self.copy_mode_metadata())
        self.assertFalse(any(
            p.name.startswith("plugins.staging-") for p in self.home.iterdir()
        ))

    def test_copy_preserves_unmanaged_entries(self):
        self.assertEqual(0, self.run_setup(["--copy"]))
        unmanaged_dir = self.home / "plugins" / "myplugin"
        unmanaged_dir.mkdir()
        (unmanaged_dir / "x.py").write_text("user plugin\n")
        (self.home / "plugins" / "notes.txt").write_text("user notes\n")
        (self.repo / "plugins" / "README.md").write_text("updated docs\n")
        self.reset_capture()
        code = self.run_setup(["--copy"])
        self.assertEqual(0, code)
        self.assertEqual("user plugin\n", (unmanaged_dir / "x.py").read_text())
        self.assertEqual("user notes\n", (self.home / "plugins" / "notes.txt").read_text())
        self.assertEqual(
            "updated docs\n", (self.home / "plugins" / "README.md").read_text()
        )

    def test_copy_refuses_existing_symlink_and_unmanaged_path(self):
        unrelated = self.root / "somewhere"
        unrelated.mkdir()
        (self.home / "plugins").symlink_to(unrelated)
        self.reset_capture()
        code = self.run_setup(["--copy"])
        self.assertNotEqual(0, code)
        self.assertIn("refusing to replace an existing symlink", self.err.getvalue())
        self.assertTrue((self.home / "plugins").is_symlink())
        self.assertFalse((self.home / "worker-blueprints").exists())

    def test_staged_copy_failure_preserves_old_assets(self):
        self.assertEqual(0, self.run_setup(["--copy"]))
        before = self.copy_mode_metadata()
        (self.repo / "workers" / "blueprints" / "fixer.md").write_text("fixer v2\n")
        real_copy2 = shutil.copy2

        def flaky_copy2(src, dst, **kwargs):
            if Path(dst).name == "fixer.md":
                raise OSError("simulated disk full")
            return real_copy2(src, dst, **kwargs)

        self.reset_capture()
        with mock.patch.object(setup_assets.shutil, "copy2", side_effect=flaky_copy2):
            code = self.run_setup(["--copy"])
        self.assertNotEqual(0, code)
        self.assertIn("simulated disk full", self.err.getvalue())
        # The prior installation is untouched and the metadata still matches.
        self.assertEqual(
            "fixer.md blueprint\n",
            (self.home / "worker-blueprints" / "fixer.md").read_text(),
        )
        self.assertEqual(before, self.copy_mode_metadata())
        for name in ("plugins", "worker-blueprints", "skills"):
            dest = self.home / name
            self.assertTrue(dest.is_dir())
            self.assertFalse(dest.is_symlink())
        self.assertFalse(
            any(dest.name.startswith("worker-blueprints.staging-")
                for dest in self.home.iterdir())
        )

    def test_excluded_local_plugin_content_is_never_copied(self):
        self.assertEqual(0, self.run_setup(["--copy"]))
        plugins = self.home / "plugins"
        for excluded in ("agent-sudo", "local-tool", "__pycache__", ".gitignore"):
            self.assertFalse((plugins / excluded).exists())
        self.assertFalse(
            (plugins / "agent-sudo" / "agent-sudo-pre-tool").exists()
        )
        self.assertFalse((plugins / "local-tool" / "local-tool-transform").exists())
        # Nested code inside an allowlisted plugin is copied.
        self.assertEqual(
            "nested code\n", (plugins / "rtk" / "nested" / "deep.txt").read_text()
        )

    def test_allowlisted_plugin_added_upstream_is_copied_on_upgrade(self):
        self.assertEqual(0, self.run_setup(["--copy"]))
        future = self.repo / "plugins" / "future-plugin"
        future.mkdir()
        (future / "future-transform").write_text("#!/bin/sh\necho future\n")
        (future / "lib").mkdir(parents=True)
        (future / "lib" / "helper.py").write_text("helper\n")
        # An arbitrary local directory added upstream without an allowlist
        # entry stays excluded even on upgrade.
        stray = self.repo / "plugins" / "stray-local"
        stray.mkdir()
        (stray / "x.py").write_text("stray\n")
        gitignore = self.repo / "plugins" / ".gitignore"
        gitignore.write_text(gitignore.read_text() + "!/future-plugin/\n")
        self.reset_capture()
        self.assertEqual(0, self.run_setup(["--copy"]))
        plugins = self.home / "plugins"
        self.assertEqual("#!/bin/sh\necho future\n",
                         (plugins / "future-plugin" / "future-transform").read_text())
        self.assertEqual("helper\n",
                         (plugins / "future-plugin" / "lib" / "helper.py").read_text())
        files = self.copy_mode_metadata()["assets"]["plugins"]["files"]
        self.assertIn("future-plugin/future-transform", files)
        self.assertIn("future-plugin/lib/helper.py", files)
        self.assertFalse((plugins / "stray-local").exists())
        self.assertNotIn("stray-local/x.py", files)

    def test_unlisted_plugin_dir_is_never_copied_even_upstream(self):
        self.assertEqual(0, self.run_setup(["--copy"]))
        unlisted = self.repo / "plugins" / "unlisted"
        unlisted.mkdir()
        (unlisted / "tool").write_text("no allowlist entry\n")
        self.reset_capture()
        self.assertEqual(0, self.run_setup(["--copy"]))
        self.assertFalse((self.home / "plugins" / "unlisted").exists())
        self.assertNotIn(
            "unlisted/tool",
            self.copy_mode_metadata()["assets"]["plugins"]["files"],
        )

    def test_de_allowlisted_plugin_dir_is_preserved_not_deleted_on_upgrade(self):
        gitignore = self.repo / "plugins" / ".gitignore"
        gitignore.write_text(gitignore.read_text() + "!/temporary/\n")
        shipped = self.repo / "plugins" / "temporary"
        shipped.mkdir()
        (shipped / "temporary-transform").write_text("v1\n")
        self.assertEqual(0, self.run_setup(["--copy"]))
        plugins = self.home / "plugins"
        self.assertTrue((plugins / "temporary" / "temporary-transform").is_file())
        # Upstream drops the allowlist entry (plugin renamed or removed), so
        # the recorded files are no longer desired.
        text = gitignore.read_text().replace("!/temporary/\n", "")
        gitignore.write_text(text)
        self.reset_capture()
        self.assertEqual(0, self.run_setup(["--copy"]))
        # The formerly managed directory is preserved untouched, not silently
        # deleted, and the update says so.
        self.assertTrue((plugins / "temporary" / "temporary-transform").is_file())
        self.assertEqual("v1\n", (plugins / "temporary" / "temporary-transform").read_text())
        self.assertIn("preserved unmanaged top-level entries", self.out.getvalue())
        self.assertIn("temporary", self.out.getvalue())
        files = self.copy_mode_metadata()["assets"]["plugins"]["files"]
        self.assertNotIn("temporary/temporary-transform", files)

    def test_locally_edited_de_allowlisted_plugin_still_blocks_update(self):
        gitignore = self.repo / "plugins" / ".gitignore"
        gitignore.write_text(gitignore.read_text() + "!/temporary/\n")
        shipped = self.repo / "plugins" / "temporary"
        shipped.mkdir()
        (shipped / "temporary-transform").write_text("v1\n")
        self.assertEqual(0, self.run_setup(["--copy"]))
        gitignore.write_text(gitignore.read_text().replace("!/temporary/\n", ""))
        edited = self.home / "plugins" / "temporary" / "temporary-transform"
        edited.write_text("local edit\n")
        self.reset_capture()
        code = self.run_setup(["--copy"])
        self.assertNotEqual(0, code)
        self.assertIn("file changed and upstream no longer ships it", self.err.getvalue())
        self.assertEqual("local edit\n", edited.read_text())

    def test_copy_mode_refuses_valid_symlink_at_destination(self):
        self.assertEqual(0, self.run_setup([]))
        self.reset_capture()
        code = self.run_setup(["--copy"])
        self.assertNotEqual(0, code)
        self.assertIn("refusing to replace an existing symlink", self.err.getvalue())
        self.assertTrue((self.home / "plugins").is_symlink())
        self.assertTrue((self.home / "worker-blueprints").is_symlink())


class SkillsAssetTests(SetupAssetsTestCase):
    """Focused coverage for the skills asset using the shared mechanics."""

    def test_skills_symlink_install_default_path(self):
        self.assertEqual(0, self.run_setup([]))
        skills = self.home / "skills"
        self.assertTrue(skills.is_symlink())
        self.assertEqual((self.repo / "skills").resolve(), skills.resolve())
        self.assert_skills_payload(skills)

    def test_skills_copy_installs_author_plan_and_records_manifest(self):
        self.assertEqual(0, self.run_setup(["--copy"]))
        skills = self.home / "skills"
        self.assertTrue(skills.is_dir())
        self.assertFalse(skills.is_symlink())
        self.assert_skills_payload(skills)
        self.assertFalse((skills / ".gitignore").exists())
        self.assertFalse((skills / "__pycache__").exists())
        entry = self.copy_mode_metadata()["assets"]["skills"]
        self.assertIn("author-plan/SKILL.md", entry["files"])
        self.assertNotIn("local-skill/SKILL.md", entry["files"])

    def test_unlisted_local_skill_is_never_copied(self):
        self.assertEqual(0, self.run_setup(["--copy"]))
        self.assertFalse((self.home / "skills" / "local-skill").exists())
        # The source fixture still has it; only the allowlist keeps it out.
        self.assertEqual(
            "ignored skill\n",
            (self.repo / "skills" / "local-skill" / "SKILL.md").read_text(),
        )

    def test_copy_preserves_unrelated_local_skills_on_repeat_update(self):
        self.assertEqual(0, self.run_setup(["--copy"]))
        local = self.home / "skills" / "my-local-workflow"
        local.mkdir()
        (local / "SKILL.md").write_text("local workflow\n")
        (self.repo / "skills" / "author-plan" / "SKILL.md").write_text(
            "author-plan v2\n"
        )
        self.reset_capture()
        self.assertEqual(0, self.run_setup(["--copy"]))
        self.assertEqual("local workflow\n", (local / "SKILL.md").read_text())
        self.assertEqual(
            "author-plan v2\n",
            (self.home / "skills" / "author-plan" / "SKILL.md").read_text(),
        )
        self.assertNotIn(
            "my-local-workflow/SKILL.md",
            self.copy_mode_metadata()["assets"]["skills"]["files"],
        )
        self.assertIn("preserved unmanaged top-level entries", self.out.getvalue())


class BehaviorTests(SetupAssetsTestCase):
    def test_default_uses_home_relative_jcode_directory(self):
        environment = os.environ.copy()
        environment.pop("JCODE_HOME", None)
        environment["HOME"] = str(self.root / "posix-home")
        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            with mock.patch.dict(os.environ, environment, clear=True), \
                    mock.patch.object(setup_assets, "REPO_ROOT", self.repo):
                code = setup_assets.main([])
        self.assertEqual(0, code)
        self.assertTrue((self.root / "posix-home" / ".jcode" / "plugins").is_symlink())

    def test_help_describes_copy_limitations(self):
        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            with self.assertRaises(SystemExit) as caught:
                setup_assets.main(["--help"])
        self.assertEqual(0, caught.exception.code)
        text = self.out.getvalue()
        for phrase in ("--copy", "snapshots", "back up or move", "config.toml",
                       "elevation", "agent-sudo", "allowlist", "python3", "rtk"):
            self.assertIn(phrase, text)

    def test_missing_required_source_fails_before_writes(self):
        (self.repo / "plugins" / "rtk" / "rtk-transform").unlink()
        code = self.run_setup([])
        self.assertNotEqual(0, code)
        self.assertIn("missing required asset file", self.err.getvalue())
        self.assertFalse((self.home / "plugins").exists())
        self.assertFalse((self.home / "worker-blueprints").exists())

    def test_windows_portable_path_behavior_best_effort(self):
        """Best-effort check; this suite does not claim native Windows validation."""
        (self.repo / "plugins" / "rtk" / "nested" / "deep").mkdir(parents=True)
        (self.repo / "plugins" / "rtk" / "nested" / "deep" / "inner.txt").write_text("x\n")
        self.assertEqual(0, self.run_setup(["--copy"]))
        files = self.copy_mode_metadata()["assets"]["plugins"]["files"]
        self.assertIn("rtk/nested/deep/inner.txt", files)
        for rel in files:
            self.assertNotIn("\\", rel)
        # Native os-agnostic join still resolves the nested payload path.
        self.assertTrue((self.home / "plugins" / "rtk" / "nested" / "deep" / "inner.txt").is_file())


class IgnoreRuleTests(unittest.TestCase):
    def rules(self) -> list:
        return setup_assets.parse_gitignore_rules(GITIGNORE_TEXT)

    def test_fixture_uses_exact_real_gitignore_form(self):
        real = (
            Path(setup_assets.__file__).resolve().parents[2]
            / "plugins"
            / ".gitignore"
        )
        if not real.is_file():
            self.skipTest("real catalog plugins/.gitignore not present")
        self.assertEqual(real.read_text(encoding="utf-8"), GITIGNORE_TEXT)

    def test_skills_fixture_gitignore_matches_real(self):
        real = (
            Path(setup_assets.__file__).resolve().parents[2]
            / "skills"
            / ".gitignore"
        )
        if not real.is_file():
            self.skipTest("real catalog skills/.gitignore not present")
        self.assertEqual(real.read_text(encoding="utf-8"), SKILLS_GITIGNORE_TEXT)

    def test_parses_negation_anchoring_and_directory_rules(self):
        rules = self.rules()
        self.assertIn(
            setup_assets.IgnoreRule(negated=False, anchored=True, dir_only=True,
                                    pattern="*"),
            rules,
        )
        self.assertIn(
            setup_assets.IgnoreRule(negated=True, anchored=True, dir_only=True,
                                    pattern="rtk"),
            rules,
        )
        self.assertIn(
            setup_assets.IgnoreRule(negated=True, anchored=True, dir_only=True,
                                    pattern="delegation-guard"),
            rules,
        )
        self.assertIn(
            setup_assets.IgnoreRule(negated=False, anchored=False, dir_only=True,
                                    pattern="__pycache__"),
            rules,
        )
        self.assertIn(
            setup_assets.IgnoreRule(negated=False, anchored=False, dir_only=False,
                                    pattern="*.py[cod]"),
            rules,
        )
        # Comments and blank lines produce no rules.
        self.assertEqual([], setup_assets.parse_gitignore_rules("\n# only a comment\n"))

    def test_allowlist_matching_semantics(self):
        rules = self.rules()
        # Shipped plugins and their nested code are permitted.
        self.assertFalse(setup_assets.ignored("rtk", True, rules))
        self.assertFalse(setup_assets.ignored("rtk/rtk-transform", False, rules))
        self.assertFalse(
            setup_assets.ignored("rtk/nested/deep/inner.txt", False, rules)
        )
        self.assertFalse(setup_assets.ignored("delegation-guard", True, rules))
        # Arbitrary unlisted top-level plugin directories are ignored.
        self.assertTrue(setup_assets.ignored("agent-sudo", True, rules))
        self.assertTrue(setup_assets.ignored("local-tool", True, rules))
        self.assertTrue(setup_assets.ignored("some-new-plugin", True, rules))
        # Files at the plugin root are not indiscriminately ignored.
        self.assertFalse(setup_assets.ignored("README.md", False, rules))
        # Recursive exclusions still apply inside allowed plugins.
        self.assertTrue(setup_assets.ignored("rtk/__pycache__", True, rules))
        self.assertTrue(setup_assets.ignored("rtk/adapter.pyc", False, rules))
        self.assertFalse(setup_assets.ignored("rtk/adapter.py", False, rules))
        self.assertTrue(setup_assets.ignored("delegation-guard/x.swp", False, rules))

    def test_anchored_wildcard_does_not_cross_slashes(self):
        rules = setup_assets.parse_gitignore_rules("/*/\n")
        self.assertTrue(setup_assets.ignored("top", True, rules))
        self.assertFalse(setup_assets.ignored("top/sub", True, rules))
        self.assertFalse(setup_assets.ignored("top/file.txt", False, rules))

    def test_skills_gitignore_allowlist_semantics(self):
        rules = setup_assets.parse_gitignore_rules(SKILLS_GITIGNORE_TEXT)
        # The shipped skill and its files are permitted.
        self.assertFalse(setup_assets.ignored("author-plan", True, rules))
        self.assertFalse(setup_assets.ignored("author-plan/SKILL.md", False, rules))
        # Arbitrary unlisted top-level skill directories are ignored; their
        # contents are excluded by directory pruning during the walk.
        self.assertTrue(setup_assets.ignored("local-skill", True, rules))
        self.assertTrue(setup_assets.ignored("some-new-skill", True, rules))
        # Recursive exclusions still apply inside allowed skills.
        self.assertTrue(setup_assets.ignored("author-plan/__pycache__", True, rules))
        self.assertTrue(setup_assets.ignored("author-plan/notes.swp", False, rules))

    def test_last_matching_rule_wins_in_order(self):
        allow_last = setup_assets.parse_gitignore_rules("/*/\n!/keep/\n")
        self.assertFalse(setup_assets.ignored("keep", True, allow_last))
        ignore_last = setup_assets.parse_gitignore_rules("!/keep/\n/*/\n")
        self.assertTrue(setup_assets.ignored("keep", True, ignore_last))
        # Negation of a directory rule does not affect a plain file path.
        self.assertFalse(setup_assets.ignored("keep", False, ignore_last))


class GitCheckIgnoreEquivalenceTests(unittest.TestCase):
    """Compare parser decisions with real git in a disposable fixture.

    This covers the gitignore subset the catalog uses; it does not claim a
    full gitignore engine.
    """

    PROBE_PATHS = (
        "README.md",
        ".DS_Store",
        "rtk/rtk-transform",
        "rtk/nested/deep/inner.txt",
        "rtk/__pycache__/a.pyc",
        "rtk/adapter.pyc",
        "rtk/notes.swp",
        "delegation-guard/delegation-guard-transform",
        "agent-sudo/agent-sudo-pre-tool",
        "localnew/x.py",
    )

    @staticmethod
    def git_environment(root: Path) -> dict:
        empty_config = root / "empty.gitconfig"
        empty_config.write_text("")
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(root),
            "GIT_CONFIG_GLOBAL": str(empty_config),
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "LC_ALL": "C",
        }

    @unittest.skipUnless(shutil.which("git"), "git not available")
    def test_parser_matches_git_check_ignore(self):
        with tempfile.TemporaryDirectory(
            prefix="jcode-gitignore-equivalence-"
        ) as tmp:
            root = Path(tmp)
            (root / ".gitignore").write_text(GITIGNORE_TEXT)
            for rel in self.PROBE_PATHS:
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("probe\n")
            environment = self.git_environment(root)
            init = subprocess.run(
                ["git", "init", "-q"], cwd=root, env=environment,
                capture_output=True, text=True,
            )
            if init.returncode != 0:
                self.skipTest(f"git init failed: {init.stderr.strip()}")
            proc = subprocess.run(
                ["git", "check-ignore", "--stdin"],
                cwd=root, env=environment, input="\n".join(self.PROBE_PATHS) + "\n",
                capture_output=True, text=True,
            )
            self.assertIn(proc.returncode, (0, 1), proc.stderr)
            reported = set(proc.stdout.split())
            rules = setup_assets.parse_gitignore_rules(GITIGNORE_TEXT)

            def effectively_ignored(rel: str) -> bool:
                # Mirror traversal: a path under an ignored directory is
                # ignored even if no rule matches the path itself.
                parts = rel.split("/")
                for index in range(1, len(parts)):
                    if setup_assets.ignored("/".join(parts[:index]), True, rules):
                        return True
                return setup_assets.ignored(rel, False, rules)

            for rel in self.PROBE_PATHS:
                self.assertEqual(
                    effectively_ignored(rel), rel in reported,
                    f"mismatch for {rel}: git reported {rel in reported}",
                )


class IsolationTests(unittest.TestCase):

    def test_subprocess_never_touches_config(self):
        """Run the real script with only disposable directories reachable."""
        with tempfile.TemporaryDirectory(prefix="jcode-setup-assets-isolated-") as tmp:
            root = Path(tmp)
            home = root / "home"
            jcode_home = home / ".jcode"
            jcode_home.mkdir(parents=True)
            sentinel = jcode_home / "config.toml"
            sentinel.write_text("# user config sentinel\n")
            for name in ("xdg-config", "xdg-cache", "xdg-data", "xdg-state"):
                (root / name).mkdir()
            environment = {
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "HOME": str(home),
                "JCODE_HOME": str(jcode_home),
                "XDG_CONFIG_HOME": str(root / "xdg-config"),
                "XDG_CACHE_HOME": str(root / "xdg-cache"),
                "XDG_DATA_HOME": str(root / "xdg-data"),
                "XDG_STATE_HOME": str(root / "xdg-state"),
            }
            proc = subprocess.run(
                [sys.executable, str(SCRIPT), "--copy"],
                capture_output=True, text=True, env=environment,
            )
            self.assertEqual(0, proc.returncode, proc.stderr)
            # The user's config.toml was not read, modified, or recreated.
            self.assertEqual("# user config sentinel\n", sentinel.read_text())
            self.assertEqual([], [p.name for p in (root / "xdg-config").iterdir()])
            installed = jcode_home / "plugins"
            self.assertTrue((installed / "rtk" / "rtk-transform").is_file())
            self.assertFalse((installed / "agent-sudo").exists())
            self.assertTrue(
                (jcode_home / "skills" / "author-plan" / "SKILL.md").is_file()
            )
            self.assertFalse((jcode_home / "skills" / ".gitignore").exists())


if __name__ == "__main__":
    unittest.main()
