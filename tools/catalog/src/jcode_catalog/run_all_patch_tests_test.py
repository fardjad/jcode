"""Tests for resumable patch test bookkeeping."""

from __future__ import annotations

import unittest

from unittest.mock import patch
from pathlib import Path
from .run_all_patch_tests import batch_child, retained_worktree_path, should_skip
from .patch_catalog import CatalogError


class RunAllPatchTestsTests(unittest.TestCase):
    def test_skip_requires_matching_pass_blob_and_no_force(self):
        results = {"change.patch": {"blob": "abc123", "status": "pass", "fingerprint": "closure123"}}
        self.assertTrue(should_skip(results, "change.patch", "abc123", False, "closure123"))
        self.assertFalse(should_skip(results, "change.patch", "abc123", False))
        self.assertFalse(should_skip(results, "change.patch", "abc123", False, "changed-dependency"))
        self.assertFalse(should_skip(results, "change.patch", "different", False))
        self.assertFalse(should_skip(results, "change.patch", "abc123", True))
        self.assertFalse(should_skip({"change.patch": {"blob": "abc123", "status": "fail"}},
                                     "change.patch", "abc123", False))

    def test_batch_child_rejects_stale_preflight_before_worktree_use(self):
        with patch('jcode_catalog.run_all_patch_tests.resolve_base', return_value='base'), \
             patch('jcode_catalog.run_all_patch_tests.validation_identity', return_value='changed'), \
             patch('jcode_catalog.patch_workflow.execute') as execute:
            with self.assertRaises(CatalogError):
                batch_child(Path('/catalog'), 'sample.patch', 'previous')
        execute.assert_not_called()

    def test_batch_child_reuses_preflight_only_for_exact_fingerprint(self):
        with patch('jcode_catalog.run_all_patch_tests.resolve_base', return_value='base'), \
             patch('jcode_catalog.run_all_patch_tests.validation_identity', return_value='same'), \
             patch('jcode_catalog.patch_workflow.execute') as execute:
            batch_child(Path('/catalog'), 'sample.patch', 'same')
        execute.assert_called_once_with(Path('/catalog'), 'test', 'sample.patch', preflight=False)

    def test_retained_worktree_path_parsing(self):
        output = "patch passed\nworktree retained: /tmp/patch tree/worktree\n"
        self.assertEqual(retained_worktree_path(output), "/tmp/patch tree/worktree")
        self.assertIsNone(retained_worktree_path("no retained worktree\n"))


if __name__ == "__main__":
    unittest.main()
