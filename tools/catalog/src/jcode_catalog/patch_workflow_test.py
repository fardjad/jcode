"""Contracts for shared patch workflow ordering and failure recovery."""
from contextlib import ExitStack
from pathlib import Path
import io
import unittest
from unittest.mock import patch

from . import patch_workflow as workflow
from .patch_catalog import CatalogError


class PatchWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path('/catalog')
        self.tree = Path('/catalog/worktree')
        self.calls = []
        targets = {
            'resolve_base': 'base',
            'validate_catalog': None,
            'patch_worktree.create': self.tree,
            'apply_patches.apply': None,
            'validate_patch_commands.validate': None,
            'validate_patch_commands.learn': None,
            'patch_worktree.cleanup': None,
            'run_nextest.execute': 0,
            'ensure_branch': None,
        }
        self.mocks = {}
        for name, result in targets.items():
            def called(*args, _name=name, _result=result, **kwargs):
                self.calls.append((_name, args, kwargs))
                return _result
            self.mocks[name] = self.stack.enter_context(
                patch('jcode_catalog.patch_workflow.' + name, side_effect=called))
        self.output = self.stack.enter_context(patch('sys.stdout', new_callable=io.StringIO))
        self.errors = self.stack.enter_context(patch('sys.stderr', new_callable=io.StringIO))

    def names(self):
        return [name for name, _, _ in self.calls]

    def test_patch_test_runs_declared_validation_then_compatibility_and_retains(self):
        workflow.execute(self.root, 'test', 'patches/0005-candidate-example.patch')
        self.assertEqual(self.names(), [
            'resolve_base', 'validate_catalog', 'patch_worktree.create',
            'apply_patches.apply', 'validate_patch_commands.validate', 'run_nextest.execute'])
        self.mocks['apply_patches.apply'].assert_called_once_with(
            self.root, self.tree, '0005-candidate-example.patch', None)
        self.mocks['run_nextest.execute'].assert_called_once_with(
            self.root, 'test', self.tree, 'base')
        self.assertIn('patch passed; worktree retained:', self.output.getvalue())

    def test_candidate_checks_kind_validates_then_branches_and_cleans_without_compat(self):
        workflow.execute(self.root, 'candidate', '0005-candidate-example.patch')
        self.mocks['apply_patches.apply'].assert_called_once_with(
            self.root, self.tree, '0005-candidate-example.patch', 'upstream-candidate')
        self.mocks['ensure_branch'].assert_called_once_with(
            self.root, self.tree, 'upstream-candidate/example')
        self.assertEqual(self.names()[-3:], [
            'validate_patch_commands.validate', 'ensure_branch', 'patch_worktree.cleanup'])
        self.mocks['run_nextest.execute'].assert_not_called()

    def test_validation_failure_retains_tree_and_prevents_later_stages(self):
        self.mocks['validate_patch_commands.validate'].side_effect = CatalogError('failure')
        with self.assertRaises(CatalogError):
            workflow.execute(self.root, 'candidate', '0005-candidate-example.patch')
        self.mocks['ensure_branch'].assert_not_called()
        self.mocks['patch_worktree.cleanup'].assert_not_called()
        self.assertIn('workflow failed; worktree retained:', self.errors.getvalue())

    def test_apply_failure_retains_tree_and_does_not_validate(self):
        self.mocks['apply_patches.apply'].side_effect = CatalogError('conflict')
        with self.assertRaises(CatalogError):
            workflow.execute(self.root, 'test', '0005-candidate-example.patch')
        self.mocks['validate_patch_commands.validate'].assert_not_called()
        self.assertIn('cleanup:', self.errors.getvalue())

    def test_compatibility_failure_is_failure_not_a_pass(self):
        self.mocks['run_nextest.execute'].side_effect = lambda *args: 1
        with self.assertRaises(CatalogError):
            workflow.execute(self.root, 'test', '0005-candidate-example.patch')
        self.assertNotIn('patch passed', self.output.getvalue())
        self.mocks['patch_worktree.cleanup'].assert_not_called()

    def test_learning_never_applies_patches_and_cleans_only_after_both_checks(self):
        workflow.execute(self.root, 'learn')
        self.assertEqual(self.names(), ['resolve_base', 'patch_worktree.create',
            'run_nextest.execute', 'validate_patch_commands.learn', 'patch_worktree.cleanup'])
        self.mocks['run_nextest.execute'].assert_called_once_with(
            self.root, 'learn', self.tree, 'base')

    def test_learning_failure_preserves_clean_upstream_worktree(self):
        self.mocks['validate_patch_commands.learn'].side_effect = CatalogError('failure')
        with self.assertRaises(CatalogError):
            workflow.execute(self.root, 'learn')
        self.mocks['patch_worktree.cleanup'].assert_not_called()
        self.assertIn('clean-upstream learning failed', self.errors.getvalue())

    def test_create_uses_persistent_path_and_skips_validation_commands(self):
        workflow.execute(self.root, 'create')
        self.assertEqual(self.mocks['patch_worktree.create'].call_args.args[-1],
                         self.root / '.patched-jcode')
        self.mocks['validate_patch_commands.validate'].assert_not_called()
        self.mocks['run_nextest.execute'].assert_not_called()

    def test_compile_failure_prevents_compatibility_and_preserves_tree(self):
        with patch('jcode_catalog.patch_workflow.subprocess.run', side_effect=OSError('compile failed')):
            with self.assertRaises(OSError):
                workflow.execute(self.root, 'validate')
        self.mocks['run_nextest.execute'].assert_not_called()
        self.assertIn('worktree retained:', self.errors.getvalue())


class CandidateBranchTests(unittest.TestCase):
    def test_divergent_candidate_is_never_overwritten(self):
        with patch('jcode_catalog.patch_workflow.git', side_effect=['expected', 'other']) as git:
            with self.assertRaises(CatalogError):
                workflow.ensure_branch(Path('/catalog'), Path('/worktree'), 'candidate/name')
        self.assertEqual(git.call_count, 2)

    def test_missing_candidate_is_created_at_exact_head(self):
        with patch('jcode_catalog.patch_workflow.git', side_effect=['expected', CatalogError('missing'), '']) as git:
            workflow.ensure_branch(Path('/catalog'), Path('/worktree'), 'candidate/name')
        git.assert_called_with('branch', 'candidate/name', 'expected', cwd=Path('/catalog'))


if __name__ == '__main__':
    unittest.main()
