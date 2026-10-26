"""CLI boundary contracts, including isolation and thin aliases."""
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from typer.testing import CliRunner
from . import cli
from .patch_catalog import CatalogError


class CliTests(unittest.TestCase):
    def test_workflow_environment_replaces_personal_paths_and_restores(self):
        original = os.environ.copy()
        with cli.workflow_environment():
            self.assertNotEqual(os.environ.get("HOME"), original.get("HOME"))
            self.assertNotEqual(os.environ.get("JCODE_HOME"), original.get("JCODE_HOME"))
            self.assertEqual(os.environ["JCODE_WORKFLOW_CONFIG_ISOLATED"], "1")
            self.assertTrue(Path(os.environ["HOME"]).is_dir())
        self.assertEqual(dict(os.environ), original)

    def test_patch_test_routes_without_invoking_paid_evaluation(self):
        with patch.object(cli, "workflow") as workflow:
            result = CliRunner().invoke(cli.app, ["patches", "test", "sample.patch"])
        self.assertEqual(result.exit_code, 0, result.output)
        workflow.assert_called_once_with("test", "sample.patch")

    def test_missing_patch_is_usage_error(self):
        result = CliRunner().invoke(cli.app, ["patches", "test"])
        self.assertNotEqual(result.exit_code, 0)

    def test_macos_install_sets_strip_only_for_subprocess_and_preserves_target(self):
        with patch.object(cli, "repository_root", return_value=Path('/catalog')), \
             patch.object(cli, "workflow") as workflow, \
             patch.object(cli.sys, "platform", "darwin"), \
             patch.dict(os.environ, {"JCODE_INSTALL_DIR": "/disposable/bin"}), \
             patch.object(cli.subprocess, "run") as run:
            cli.install()
        workflow.assert_called_once_with("create")
        self.assertEqual(run.call_args.kwargs["env"]["CARGO_PROFILE_RELEASE_STRIP"], "none")
        self.assertEqual(run.call_args.kwargs["env"]["JCODE_INSTALL_DIR"], "/disposable/bin")
        self.assertEqual(run.call_args.args[0], ["bash", "scripts/install_release.sh", "--fast"])

    def test_pin_refuses_dirty_checkout_without_fetch_or_write(self):
        with patch.object(cli, "repository_root", return_value=Path('/catalog')), \
             patch.object(cli, "git", side_effect=["personalized", "dirty"]), \
             patch("jcode_catalog.version_base.resolve_release") as resolve:
            with self.assertRaises(CatalogError):
                cli.set_version("v1.2.3")
        resolve.assert_not_called()

    def test_pin_hash_mismatch_never_writes(self):
        with patch.object(cli, "repository_root", return_value=Path('/catalog')), \
             patch.object(cli, "git", side_effect=["personalized", ""]), \
             patch("jcode_catalog.version_base.resolve_release", return_value="a" * 40), \
             patch("jcode_catalog.version_base.write_version") as write:
            with self.assertRaises(CatalogError):
                cli.set_version("v1.2.3", "b" * 7)
        write.assert_not_called()

    def test_catalog_parser_and_cli_aliases_preserve_boundary_contracts(self):
        root = Path(__file__).resolve().parents[4]
        self.assertIn('UPSTREAM_URL = "https://github.com/1jehuang/jcode.git"',
                      (root / 'tools/catalog/src/jcode_catalog/version_base.py').read_text())
        self.assertIn('"fetch", "--no-tags", UPSTREAM_URL',
                      (root / 'tools/catalog/src/jcode_catalog/version_base.py').read_text())
        self.assertNotIn('master', (root / 'justfile').read_text())

    def test_non_macos_install_preserves_caller_strip_setting(self):
        with patch.object(cli, 'repository_root', return_value=Path('/catalog')), \
             patch.object(cli, 'workflow'), \
             patch.object(cli.sys, 'platform', 'linux'), \
             patch.dict(os.environ, {'JCODE_INSTALL_DIR': '/disposable/bin',
                                     'CARGO_PROFILE_RELEASE_STRIP': 'symbols'}), \
             patch.object(cli.subprocess, 'run') as run:
            cli.install()
            self.assertEqual(os.environ['CARGO_PROFILE_RELEASE_STRIP'], 'symbols')
        self.assertEqual(run.call_args.kwargs['env']['CARGO_PROFILE_RELEASE_STRIP'], 'symbols')

    def test_aliases_delegate_all_commands_to_uv(self):
        root = Path(__file__).resolve().parents[4]
        lines = (root / "justfile").read_text().splitlines()
        commands = [line.strip() for line in lines if line.startswith("    ")]
        self.assertTrue(commands)
        self.assertTrue(all(
            line.startswith("uv run --project tools/catalog jcode-catalog ")
            or line == "@just --list"
            for line in commands
        ))
        self.assertNotIn("master", (root / ".github/workflows/release-patched.yml").read_text())


if __name__ == "__main__":
    unittest.main()
