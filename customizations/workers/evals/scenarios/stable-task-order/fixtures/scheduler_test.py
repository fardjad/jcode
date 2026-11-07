from __future__ import annotations

import unittest

from scheduler import order_tasks


class SchedulerTests(unittest.TestCase):
    def test_dependencies_and_stable_ready_order(self):
        tasks = {"publish": ["test"], "test": ["compile"], "lint": [], "compile": []}
        self.assertEqual(order_tasks(tasks), ["lint", "compile", "test", "publish"])

    def test_recompute_ready_queue_after_each_task(self):
        self.assertEqual(order_tasks({"a": ["b"], "b": [], "c": []}), ["b", "a", "c"])

    def test_duplicate_edges_and_no_mutation(self):
        tasks = {"a": ["b", "b"], "b": []}
        self.assertEqual(order_tasks(tasks), ["b", "a"])
        self.assertEqual(tasks, {"a": ["b", "b"], "b": []})

    def test_empty_and_disconnected_graphs(self):
        self.assertEqual(order_tasks({}), [])
        self.assertEqual(order_tasks({"z": [], "a": []}), ["z", "a"])

    def test_cycle_self_cycle_and_unknown_dependency(self):
        for tasks in ({"a": ["b"], "b": ["a"]}, {"a": ["a"]}, {"a": ["missing"]}):
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                order_tasks(tasks)


if __name__ == "__main__":
    unittest.main()
