"""Tests for resumable patch test bookkeeping."""

from __future__ import annotations

import unittest

from run_all_patch_tests import retained_worktree_path, should_skip


class RunAllPatchTestsTests(unittest.TestCase):
    def test_skip_requires_matching_pass_blob_and_no_force(self):
        results = {"change.patch": {"blob": "abc123", "status": "pass"}}
        self.assertTrue(should_skip(results, "change.patch", "abc123", False))
        self.assertFalse(should_skip(results, "change.patch", "different", False))
        self.assertFalse(should_skip(results, "change.patch", "abc123", True))
        self.assertFalse(should_skip({"change.patch": {"blob": "abc123", "status": "fail"}},
                                     "change.patch", "abc123", False))

    def test_retained_worktree_path_parsing(self):
        output = "patch passed\nworktree retained: /tmp/patch tree/worktree\n"
        self.assertEqual(retained_worktree_path(output), "/tmp/patch tree/worktree")
        self.assertIsNone(retained_worktree_path("no retained worktree\n"))


if __name__ == "__main__":
    unittest.main()
