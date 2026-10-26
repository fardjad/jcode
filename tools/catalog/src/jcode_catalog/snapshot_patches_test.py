"""Unit tests for snapshot planning and explicit patch filename metadata."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import snapshot_patches, validate_patches
from .patch_catalog import CatalogError, Patch


def message(
    subject: str = "feat: sample change",
    name: str = "1001-personal-feature-sample-change.patch",
    kind: str = "personal-feature",
    dependency: str = "none",
    include_file: bool = True,
    valid_sections: bool = True,
) -> str:
    headers = [
        f"X-Jcode-Patch-Intent: {subject}",
        f"X-Jcode-Patch-Kind: {kind}",
        f"X-Jcode-Patch-Depends-On: {dependency}",
    ]
    if include_file:
        headers.append(f"X-Jcode-Patch-File: {name}")
    sections = [
        "Patch intent:\nDoes the sample change.",
        "Why it exists:\nThe sample needs this.",
        "Upstream integration points:\nThe sample module.",
        "Update guidance:\nKeep this behavior.",
    ]
    if valid_sections:
        sections.append("Validation:\necho test")
    return subject + "\n\n" + "\n".join(headers) + "\n\n" + "\n\n".join(sections) + "\n"


def entry(commit: str, msg: str, text: str | None = None) -> dict[str, str]:
    return {"commit_hash": commit, "message": msg, "patch_text": text or f"patch {commit}\n"}


class SnapshotPlanTests(unittest.TestCase):
    def test_unchanged_regeneration(self) -> None:
        name = "1001-personal-feature-sample-change.patch"
        text = "same bytes\n"
        plan = snapshot_patches.plan_snapshot(
            [entry("a" * 40, message(name=name), text)], {name: text}, False
        )
        self.assertEqual(plan, ([(name, text, "unchanged")], []))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / name
            path.write_bytes(text.encode())
            snapshot_patches.apply_plan(Path(directory), plan)
            self.assertEqual(path.read_bytes(), text.encode())

    def test_new_patch_with_header(self) -> None:
        name = "1001-personal-feature-sample-change.patch"
        plan = snapshot_patches.plan_snapshot(
            [entry("a" * 40, message(name=name))], set(), False
        )
        self.assertEqual(plan[0], [(name, "patch " + "a" * 40 + "\n", "new")])

    def test_missing_header_suggests_filename_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "1001-personal-feature-sample-change.patch"
            original = b"original patch bytes\n"
            path.write_bytes(original)
            with self.assertRaisesRegex(CatalogError, 'add "X-Jcode-Patch-File: 1002-personal-feature-sample-change.patch"'):
                snapshot_patches.plan_snapshot(
                    [entry("a" * 40, message(include_file=False))],
                    {path.name: original.decode()}, False,
                )
            self.assertEqual(path.read_bytes(), original)

    def test_reorder_requires_renumbering(self) -> None:
        first = entry("a" * 40, message(name="1002-personal-feature-first.patch"))
        second = entry("b" * 40, message(name="1001-personal-feature-second.patch"))
        with self.assertRaisesRegex(CatalogError, "out of order.*renumber"):
            snapshot_patches.plan_snapshot([first, second], set(), False)

    def test_correctly_renumbered_reorder_is_accepted(self) -> None:
        first = entry("a" * 40, message(name="1001-personal-feature-first.patch"))
        second = entry("b" * 40, message(name="1002-personal-feature-second.patch"))
        plan = snapshot_patches.plan_snapshot([first, second], set(), False)
        self.assertEqual([item[0] for item in plan[0]], [
            "1001-personal-feature-first.patch", "1002-personal-feature-second.patch"
        ])

    def test_stale_patch_requires_prune_and_prune_deletes(self) -> None:
        name = "1001-personal-feature-sample-change.patch"
        stale = "1002-personal-feature-old.patch"
        current = {name: "patch aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n", stale: "stale\n"}
        with self.assertRaisesRegex(CatalogError, f"stale patch files: {stale}.*--prune"):
            snapshot_patches.plan_snapshot(
                [entry("a" * 40, message(name=name))], current, False
            )
        plan = snapshot_patches.plan_snapshot(
            [entry("a" * 40, message(name=name))], current, True
        )
        self.assertEqual(plan[1], [stale])
        with tempfile.TemporaryDirectory() as directory:
            for filename, text in current.items():
                (Path(directory) / filename).write_text(text)
            snapshot_patches.apply_plan(Path(directory), plan)
            self.assertFalse((Path(directory) / stale).exists())

    def test_split_middle_commit_is_accepted(self) -> None:
        entries = [
            entry("a" * 40, message(name="1010-personal-feature-first.patch")),
            entry("b" * 40, message(name="1011-personal-feature-middle.patch")),
            entry("c" * 40, message(name="1012-personal-feature-last.patch")),
        ]
        self.assertEqual(len(snapshot_patches.plan_snapshot(entries, set(), False)[0]), 3)

    def test_invalid_later_message_leaves_existing_files_untouched(self) -> None:
        first_name = "1001-personal-feature-first.patch"
        second_name = "1002-personal-feature-second.patch"
        existing = {first_name: b"first\x00", second_name: b"second\x00"}
        with tempfile.TemporaryDirectory() as directory:
            for name, text in existing.items():
                (Path(directory) / name).write_bytes(text)
            entries = [
                entry("a" * 40, message(name=first_name)),
                entry("b" * 40, message(name=second_name, valid_sections=False)),
            ]
            with self.assertRaises(CatalogError):
                snapshot_patches.plan_snapshot(
                    entries,
                    {name: (Path(directory) / name).read_bytes().decode("latin1") for name in existing},
                    False,
                )
            self.assertEqual(
                {name: (Path(directory) / name).read_bytes() for name in existing}, existing
            )

    def test_dependency_on_later_or_missing_patch_is_rejected(self) -> None:
        first = entry("a" * 40, message(name="1001-personal-feature-first.patch"))
        later_name = "1002-personal-feature-later.patch"
        later = entry("b" * 40, message(name=later_name))
        depends_later = entry(
            "c" * 40,
            message(name="1003-personal-feature-dependent.patch", dependency=later_name),
        )
        for dependency in (later_name, "1008-personal-feature-absent.patch"):
            dependent = entry(
                "c" * 40,
                message(name="1003-personal-feature-dependent.patch", dependency=dependency),
            )
            with self.subTest(dependency=dependency), self.assertRaisesRegex(CatalogError, "earlier commit"):
                snapshot_patches.plan_snapshot([first, dependent, later], set(), False)

    def test_kind_number_range_mismatch_is_rejected(self) -> None:
        bad = entry("a" * 40, message(name="1001-upstream-candidate-wrong-range.patch", kind="upstream-candidate"))
        with self.assertRaisesRegex(CatalogError, "0000-0999"):
            snapshot_patches.plan_snapshot([bad], set(), False)

    def test_duplicate_names_are_rejected(self) -> None:
        name = "1001-personal-feature-sample-change.patch"
        with self.assertRaisesRegex(CatalogError, "duplicate X-Jcode-Patch-File"):
            snapshot_patches.plan_snapshot(
                [entry("a" * 40, message(name=name)), entry("b" * 40, message(name=name))],
                set(), False,
            )

    def test_snapshot_with_bad_later_commit_writes_nothing(self) -> None:
        good = "1001-personal-feature-sample-change.patch"
        later = "1002-personal-feature-later-change.patch"
        messages = {
            "a" * 40: message(name=good),
            "b" * 40: message(name=later, valid_sections=False),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".patched-jcode" / ".git").mkdir(parents=True)
            patches_dir = root / "patches"
            patches_dir.mkdir()
            original = b"old bytes that differ from regenerated text\n"
            (patches_dir / good).write_bytes(original)
            with patch("jcode_catalog.version_base.resolve_base", return_value="base"), \
                    patch.object(snapshot_patches, "commits_above_base",
                                 return_value=list(messages)), \
                    patch.object(snapshot_patches, "commit_message",
                                 side_effect=lambda _w, c: messages[c]), \
                    patch.object(snapshot_patches, "format_patch",
                                 side_effect=lambda _w, c, _k: f"new {c}\n"):
                with self.assertRaises(CatalogError):
                    snapshot_patches.snapshot(root, prune=True)
            self.assertEqual((patches_dir / good).read_bytes(), original)
            self.assertEqual(sorted(p.name for p in patches_dir.iterdir()), [good])

    def test_format_patch_normalizes_only_personal_top_level_hash(self):
        original = (
            "From " + "a" * 40 + " Mon Sep 17 00:00:00 2001\n"
            "Subject: sample\n\nFrom " + "b" * 40 + " body text\n"
        )
        with patch.object(snapshot_patches.subprocess, "run") as run:
            run.return_value.stdout = original
            personal = snapshot_patches.format_patch(Path('/worktree'), 'commit', 'personal-feature')
            candidate = snapshot_patches.format_patch(Path('/worktree'), 'commit', 'upstream-candidate')
        self.assertTrue(personal.startswith('From ' + '0' * 40 + ' '))
        self.assertIn('From ' + 'b' * 40 + ' body text', personal)
        self.assertEqual(candidate, original)

    def test_validate_file_requires_matching_filename_header(self) -> None:
        name = "1001-personal-feature-sample-change.patch"
        content = (
            "From 0000000000000000000000000000000000000000 commit\n"
            "Subject: [PATCH] sample\n\n"
            "X-Jcode-Patch-Intent: sample\n"
            "X-Jcode-Patch-Kind: personal-feature\n"
            "X-Jcode-Patch-Depends-On: none\n"
            "X-Jcode-Patch-File: 1002-personal-feature-wrong.patch\n\n"
            "Patch intent:\nDetails\nWhy it exists:\nDetails\n"
            "Upstream integration points:\nDetails\nUpdate guidance:\nDetails\n"
            "Validation:\necho test\n---\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "patches" / name
            path.parent.mkdir()
            path.write_text(content)
            patch_obj = Patch(path, (), "sample", "personal-feature")
            with patch.object(validate_patches, "git", return_value=f"patches/{name}"):
                with self.assertRaisesRegex(CatalogError, "does not match filename"):
                    validate_patches.validate_file(root, patch_obj)


if __name__ == "__main__":
    unittest.main()
