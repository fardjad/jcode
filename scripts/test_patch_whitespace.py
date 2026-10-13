"""Regression tests for patch-aware source whitespace validation."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch as mock_patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from patch_catalog import CatalogError, Patch
from snapshot_patches import format_patch
from validate_patches import check_patch_whitespace, validate_file


ROOT = Path(__file__).resolve().parent.parent


def diff(
    target: str,
    hunks: str,
    *,
    old: str | None = None,
    new: str | None = None,
    extra: str = "",
) -> str:
    old = old if old is not None else f"a/{target}"
    new = new if new is not None else f"b/{target}"
    return (
        f"diff --git a/{target} b/{target}\n"
        f"index 1111111..2222222 100644\n"
        f"--- {old}\n+++ {new}\n"
        f"{extra}{hunks}"
    )


def catalog_text(hunks: str) -> str:
    return (
        "From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001\n"
        "Subject: [PATCH] test\n"
        "X-Jcode-Patch-Intent: test source whitespace\n"
        "X-Jcode-Patch-Kind: personal-fix\n"
        "X-Jcode-Patch-Depends-On: none\n"
        "\n"
        "Patch intent:\nCheck added source.\n\n"
        "Why it exists:\nExercise validation.\n\n"
        "Upstream integration points:\nUnified diffs.\n\n"
        "Update guidance:\nKeep checks patch-aware.\n\n"
        "Validation:\npython3 -m unittest\n"
        "---\n"
        + diff("src/example.rs", hunks)
    )


class PatchWhitespaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ROOT
        self.patch = Patch(self.root / "patches" / "synthetic.patch", (), "test", "personal-fix")

    def check(self, text: str) -> None:
        check_patch_whitespace(self.root, self.patch, text)

    def assert_bad(self, hunk: str, issue: str) -> None:
        with self.assertRaisesRegex(CatalogError, issue):
            self.check(diff("src/example.rs", hunk))

    def test_trailing_space_and_tab_in_added_payload_are_rejected(self) -> None:
        self.assert_bad("@@ -1 +1 @@\n+let value = 1; \n", "trailing whitespace")
        self.assert_bad("@@ -1 +1 @@\n+let value = 1;\t\n", "trailing whitespace")

    def test_space_before_tab_only_in_leading_indentation_is_rejected(self) -> None:
        self.assert_bad("@@ -1 +1 @@\n+ \tlet value = 1;\n", "space before tab")
        self.check(diff("src/example.rs", "@@ -1 +1 @@\n+let value = \t1;\n"))

    def test_context_removed_headers_mail_and_signature_are_ignored(self) -> None:
        text = (
            "From 0000000000000000000000000000000000000000 Mon Sep 17 00:00:00 2001\n"
            "Subject: [PATCH] trailing space \t\n\n"
            "Patch intent: trailing space \t\n"
            "Why it exists: nonempty\n"
            "Upstream integration points: nonempty\n"
            "Update guidance: nonempty\n"
            "Validation: command\n"
            + diff(
                "src/example.rs",
                "@@ -1,3 +1,2 @@\n context \t\n  \t\n-removed \t\n+clean\n",
            )
            + diff("src/deleted.rs", "@@ -1 +0,0 @@\n-deleted \t\n", new="/dev/null")
            + diff("src/new.rs", "@@ -0,0 +1 @@\n+new file\n", old="/dev/null")
            + "-- \n"
        )
        self.check(text)

    def test_plus_plus_payload_is_checked_inside_hunk(self) -> None:
        self.assert_bad("@@ -0,0 +1 @@\n+++payload \n", "trailing whitespace")

    def test_mail_signature_stops_parsing_after_exhausted_hunk(self) -> None:
        self.check(
            diff("src/example.rs", "@@ -0,0 +1 @@\n+clean\n")
            + "-- \n"
            + diff("src/fake.rs", "@@ -0,0 +1 @@\n+fake payload \n")
        )

    def test_hunk_shaped_preamble_is_ignored_until_diff_section(self) -> None:
        self.check(
            "+++ b/src/preamble.rs\n"
            "@@ -0,0 +1 @@\n"
            "+preamble payload \n"
            + diff("src/example.rs", "@@ -0,0 +1 @@\n+clean\n")
        )

    def test_quoted_target_strips_b_prefix_for_diagnostics(self) -> None:
        with self.assertRaises(CatalogError) as caught:
            self.check(
                diff(
                    "src/example.rs",
                    "@@ -1,0 +42 @@\n+bad \n",
                    new='"b/src/example.rs"',
                )
            )
        self.assertIn("src/example.rs:42", str(caught.exception))

    def test_signature_like_removed_source_is_consumed_inside_hunk(self) -> None:
        self.assert_bad("@@ -1 +1 @@\n-- \n+bad \n", "trailing whitespace")

    def test_multiple_files_hunks_counts_and_no_newline_marker(self) -> None:
        text = (
            diff("src/first.rs", "@@ -1,2 +1,2 @@\n-old\n+new\n context\n")
            + diff(
                "src/second.rs",
                "@@ -0,0 +10,2 @@\n+first\n+second\n"
                "\\ No newline at end of file\n"
                "@@ -20 +30 @@\n+last\n",
            )
        )
        self.check(text)
        with self.assertRaisesRegex(CatalogError, r"synthetic\.patch:\d+ \(src/second\.rs:11\)"):
            self.check(
                diff("src/first.rs", "@@ -0,0 +1 @@\n+clean\n")
                + diff("src/second.rs", "@@ -0,0 +11 @@\n+bad \n")
            )

    def test_empty_addition_is_allowed_but_space_only_addition_is_not(self) -> None:
        self.check(diff("src/example.rs", "@@ -0,0 +1 @@\n+\n"))
        self.assert_bad("@@ -0,0 +1 @@\n+ \n", "trailing whitespace")

    def test_unprefixed_empty_context_counts_as_source_line(self) -> None:
        self.check(diff("src/example.rs", "@@ -1,2 +1,2 @@\n-old\n+clean\n\n context\n"))
        with self.assertRaisesRegex(
            CatalogError, r"synthetic\.patch:\d+ \(src/example\.rs:3\): trailing whitespace"
        ):
            self.check(diff("src/example.rs", "@@ -1,3 +1,3 @@\n-old\n+clean\n\n+bad \n"))

    def test_unprefixed_empty_context_consumes_omitted_count_hunk(self) -> None:
        self.check(diff("src/example.rs", "@@ -1 +1 @@\n-old\n+clean\n\n"))

    def test_format_patch_uses_per_command_blank_context_option(self) -> None:
        with mock_patch(
            "snapshot_patches.subprocess.run",
            return_value=type("Completed", (), {"stdout": "generated patch"})(),
        ) as run:
            self.assertEqual(format_patch(Path("/synthetic"), "abc123", "personal-fix"), "generated patch")
        self.assertEqual(
            run.call_args.args[0],
            [
                "git", "-c", "diff.suppressBlankEmpty=true", "format-patch",
                "--no-signature", "--stdout", "-1", "abc123",
            ],
        )

    def test_binary_and_non_hunk_plus_lines_are_ignored(self) -> None:
        self.check(
            "mail + \t\n"
            "++ file header + \t\n"
            + diff("image.png", "", extra="GIT binary patch\n+binary payload \t\n")
            + diff("src/example.rs", "index lines + \t\nnot a hunk + \t\n")
        )

    def test_added_line_reports_catalog_target_line_and_repair_advice(self) -> None:
        with self.assertRaises(CatalogError) as caught:
            self.check(diff("src/example.rs", "@@ -1,0 +42 @@\n+bad \n"))
        message = str(caught.exception)
        self.assertIn("patches/synthetic.patch:", message)
        self.assertIn("src/example.rs:42", message)
        self.assertIn("amend the owning source commit in .patched-jcode", message)
        self.assertIn("just snapshot-patches", message)

    def test_validate_file_checks_whitespace_after_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "patches" / "synthetic.patch"
            path.parent.mkdir()
            patch = Patch(path, (), "test", "personal-fix")
            path.write_text(catalog_text("@@ -0,0 +1 @@\n+bad \n"), encoding="utf-8")
            with mock_patch("validate_patches.git", return_value="patches/synthetic.patch"):
                with self.assertRaisesRegex(CatalogError, "trailing whitespace"):
                    validate_file(root, patch)

    def test_validate_file_accepts_clean_valid_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "patches" / "synthetic.patch"
            path.parent.mkdir()
            patch = Patch(path, (), "test", "personal-fix")
            path.write_text(catalog_text("@@ -0,0 +1,2 @@\n+clean\n+\n"), encoding="utf-8")
            with mock_patch("validate_patches.git", return_value="patches/synthetic.patch"):
                validate_file(root, patch)


if __name__ == "__main__":
    unittest.main()
