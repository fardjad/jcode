"""Focused tests for the current-catalog worker-evaluation build gate."""

from __future__ import annotations

from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from .patch_catalog import CatalogError
from .prepare_worker_eval_build import (
    BUILD_HELPER_CONTRACT_VERSION,
    BUILD_TARGETS,
    attempt_directory,
    binary_paths,
    build_log_path,
    build_status_path,
    current_inputs,
    evaluation_directory,
    evaluation_worktree,
    expected_binary_paths,
    fingerprint_inputs,
    hash_file,
    prepare,
    read_valid_stamp,
    success_stamp_path,
)
from .validation_state import exclusive_lock


PATCH_NAMES = ("1000-a.patch", "1001-b.patch")


def patch_text(name: str) -> str:
    return (
        "X-Jcode-Patch-Depends-On: none\n"
        f"X-Jcode-Patch-Intent: test {name}\n"
        "X-Jcode-Patch-Kind: personal-feature\n"
    )


class FakeChild:
    def __init__(self, owner: "FakeEvaluation", command, **options):
        self.owner = owner
        self.command = command
        self.options = options
        self.pid = 42000 + len(owner.launch_calls)
        self.returncode = None
        self.waits = 0

    def wait(self, timeout=None):
        self.waits += 1
        if self.owner.interrupted:
            if self.waits == 1:
                raise KeyboardInterrupt
            self.returncode = -signal.SIGTERM
            return self.returncode
        if self.waits > 1:
            return self.returncode
        if self.owner.build_hook is not None:
            self.owner.build_hook()
        self.options["stdout"].write("mock cargo build output\n")
        self.returncode = self.owner.exit_code
        if self.returncode == 0:
            target = Path(self.options["env"]["CARGO_TARGET_DIR"])
            for name, path in binary_paths(target).items():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"{self.owner.binary_content}:{name}".encode())
        return self.returncode

    def terminate(self):
        self.owner.terminations.append((self.pid, signal.SIGTERM))

    def kill(self):
        self.owner.terminations.append((self.pid, signal.SIGKILL))


class FakeEvaluation:
    """In-memory materialization/source state and a no-Cargo child process."""

    def __init__(self, root: Path):
        self.root = root
        self.base = "a" * 40
        self.toolchain = {
            "rustc_vv": "rustc 1.0.0 (mock)\nhost: mock-host",
            "host": "mock-host",
        }
        self.head = "b" * 40
        self.tree = "c" * 40
        self.source_dirty = ""
        self.worktree = None
        self.exit_code = 0
        self.interrupted = False
        self.binary_content = "mock-native-binary"
        self.build_hook = None
        self.created = []
        self.applied = []
        self.cache_calls = []
        self.launch_calls = []
        self.source_calls = []
        self.terminations = []

    def create(self, root, name, revision, prefix="patch-worktree-", destination=None):
        if name != "worker-evaluation" or revision != self.base:
            raise AssertionError("materialization did not use the resolved base")
        if destination != evaluation_worktree(root):
            raise AssertionError("materialization escaped the dedicated worktree")
        self.worktree = destination.resolve()
        self.worktree.mkdir(parents=True, exist_ok=True)
        self.created.append((revision, destination))
        return destination

    def apply(self, root, worktree, target, required_kind=None):
        if target is not None:
            raise AssertionError("only a dependency subset was applied")
        self.applied.append({
            "worktree": worktree,
            "target": target,
            "patches": sorted(path.name for path in (root / "patches").glob("*.patch")),
        })

    def source_state(self, worktree):
        worktree = worktree.resolve()
        self.source_calls.append(worktree)
        if self.source_dirty:
            raise CatalogError(f"worker evaluation source is dirty: {worktree}\n{self.source_dirty}")
        return {"worktree": str(worktree), "head": self.head, "tree": self.tree}

    def source_cache(self, root, worktree, purpose):
        self.cache_calls.append((root, worktree, purpose))
        if purpose != "worker-evaluation":
            raise AssertionError("wrong source cache purpose")
        destination = root / ".tools" / "cargo-target" / purpose / self.tree
        destination.mkdir(parents=True, exist_ok=True)
        return destination

    def launch(self, command, **options):
        self.launch_calls.append((command, options))
        return FakeChild(self, command, **options)

    def inputs(self):
        with patch("jcode_catalog.prepare_worker_eval_build.resolve_base", side_effect=lambda root: self.base), \
                patch("jcode_catalog.prepare_worker_eval_build.read_toolchain",
                      side_effect=lambda: deepcopy(self.toolchain)):
            return current_inputs(self.root)

    def fingerprint(self):
        return fingerprint_inputs(self.inputs())

    def prepare(self, held_lock=None):
        with ExitStack() as stack:
            stack.enter_context(patch(
                "jcode_catalog.prepare_worker_eval_build.resolve_base",
                side_effect=lambda root: self.base,
            ))
            stack.enter_context(patch(
                "jcode_catalog.prepare_worker_eval_build.read_toolchain",
                side_effect=lambda: deepcopy(self.toolchain),
            ))
            stack.enter_context(patch(
                "jcode_catalog.prepare_worker_eval_build.patch_worktree.create",
                side_effect=self.create,
            ))
            stack.enter_context(patch(
                "jcode_catalog.prepare_worker_eval_build.apply_patches.apply",
                side_effect=self.apply,
            ))
            stack.enter_context(patch(
                "jcode_catalog.prepare_worker_eval_build.source_state",
                side_effect=self.source_state,
            ))
            stack.enter_context(patch(
                "jcode_catalog.prepare_worker_eval_build.source_cache",
                side_effect=self.source_cache,
            ))
            stack.enter_context(patch(
                "jcode_catalog.prepare_worker_eval_build.subprocess.Popen",
                side_effect=self.launch,
            ))
            if os.name != "nt":
                stack.enter_context(patch(
                    "jcode_catalog.prepare_worker_eval_build.os.killpg",
                    side_effect=lambda pid, sig: self.terminations.append((pid, sig)),
                ))
            return prepare(self.root, held_lock=held_lock)

    def valid_stamp(self):
        inputs = self.inputs()
        with patch("jcode_catalog.prepare_worker_eval_build.source_state", side_effect=self.source_state):
            return read_valid_stamp(self.root, fingerprint_inputs(inputs), inputs)


class WorkerEvaluationBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="worker-eval-build-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        (self.root / "patches").mkdir()
        for name in PATCH_NAMES:
            (self.root / "patches" / name).write_text(patch_text(name), encoding="utf-8")
        (self.root / "VERSION.txt").write_text("v1.0.0\n", encoding="utf-8")
        self.fake = FakeEvaluation(self.root)

    def test_fingerprint_covers_base_every_patch_and_add_remove(self):
        baseline_inputs = self.fake.inputs()
        baseline = fingerprint_inputs(baseline_inputs)
        self.assertEqual(
            [record["name"] for record in baseline_inputs["patches"]],
            sorted(PATCH_NAMES),
        )
        self.assertTrue(all(len(record["sha256"]) == 64
                            for record in baseline_inputs["patches"]))

        self.fake.base = "d" * 40
        self.assertNotEqual(baseline, self.fake.fingerprint())
        self.fake.base = "a" * 40
        self.assertEqual(baseline, self.fake.fingerprint())

        for name in PATCH_NAMES:
            with self.subTest(changed_patch=name):
                path = self.root / "patches" / name
                original = path.read_bytes()
                path.write_bytes(original + b"\nuncommitted change\n")
                self.assertNotEqual(baseline, self.fake.fingerprint())
                path.write_bytes(original)
                self.assertEqual(baseline, self.fake.fingerprint())

        added = self.root / "patches" / "1002-added.patch"
        added.write_text(patch_text(added.name), encoding="utf-8")
        self.assertNotEqual(baseline, self.fake.fingerprint())
        added.unlink()
        self.assertEqual(baseline, self.fake.fingerprint())

        removed = self.root / "patches" / PATCH_NAMES[1]
        contents = removed.read_bytes()
        removed.unlink()
        self.assertNotEqual(baseline, self.fake.fingerprint())
        removed.write_bytes(contents)
        self.assertEqual(baseline, self.fake.fingerprint())

    def test_fingerprint_covers_reorder_toolchain_and_helper_contract(self):
        inputs = self.fake.inputs()
        baseline = fingerprint_inputs(inputs)
        reordered = deepcopy(inputs)
        reordered["patches"].reverse()
        self.assertNotEqual(baseline, fingerprint_inputs(reordered))

        changed_profile = deepcopy(inputs)
        changed_profile["build"]["profile"] = "release"
        self.assertNotEqual(baseline, fingerprint_inputs(changed_profile))
        changed_targets = deepcopy(inputs)
        changed_targets["build"]["targets"].pop()
        self.assertNotEqual(baseline, fingerprint_inputs(changed_targets))

        self.fake.toolchain["rustc_vv"] += "\ncommit-hash: different"
        self.assertNotEqual(baseline, self.fake.fingerprint())
        self.fake.toolchain["rustc_vv"] = inputs["toolchain"]["rustc_vv"]
        self.fake.toolchain["host"] = "other-host"
        self.assertNotEqual(baseline, self.fake.fingerprint())
        self.fake.toolchain["host"] = inputs["toolchain"]["host"]
        self.assertEqual(baseline, self.fake.fingerprint())

        with patch("jcode_catalog.prepare_worker_eval_build.BUILD_HELPER_CONTRACT_VERSION",
                   BUILD_HELPER_CONTRACT_VERSION + 1):
            self.assertNotEqual(baseline, self.fake.fingerprint())

    def test_build_uses_full_dedicated_catalog_and_returns_provenance(self):
        decoy = self.root / ".patched-jcode"
        decoy.mkdir()
        (decoy / "must-not-be-used").write_text("source-only edit", encoding="utf-8")

        result = self.fake.prepare()

        self.assertEqual(result["action"], "built")
        self.assertTrue(result["built"])
        self.assertFalse(result["reused"])
        self.assertEqual(self.fake.created, [(self.fake.base, evaluation_worktree(self.root))])
        self.assertEqual(len(self.fake.applied), 1)
        self.assertEqual(self.fake.applied[0]["patches"], sorted(PATCH_NAMES))
        self.assertIsNone(self.fake.applied[0]["target"])
        self.assertEqual(self.fake.cache_calls[0][2], "worker-evaluation")
        self.assertEqual(result["source"]["tree"], self.fake.tree)
        self.assertEqual(result["base"], self.fake.base)
        self.assertEqual(result["build"]["profile"], "debug")
        self.assertEqual(
            [(target["package"], target["binary"]) for target in result["build"]["targets"]],
            list(BUILD_TARGETS),
        )
        self.assertEqual(set(result["binary_paths"]),
                         {"jcode", "jcode-harness-api-bridge"})
        for name, recorded_path in result["binary_paths"].items():
            path = Path(recorded_path)
            self.assertEqual(path, expected_binary_paths(self.root, self.fake.tree)[name])
            self.assertEqual(result["binaries"][name]["sha256"], hash_file(path))
        self.assertNotIn(decoy, self.fake.source_calls)
        command = self.fake.launch_calls[0][0]
        self.assertTrue(command[1].startswith(str(evaluation_worktree(self.root))))
        self.assertNotIn(str(decoy), " ".join(command))

        status = json.loads(Path(result["build_status"]).read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "success")
        self.assertEqual(status["exit_code"], 0)
        self.assertIn("worker-eval-exit-status: 0", Path(result["build_log"]).read_text())
        self.assertTrue(Path(result["stamp"]).is_file())
        self.assertEqual(list(attempt_directory(self.root, result["fingerprint"]).glob(".success.json.*")), [])

    def test_valid_stamp_reuses_without_materialization_cache_or_cargo(self):
        first = self.fake.prepare()
        create_count = len(self.fake.created)
        apply_count = len(self.fake.applied)
        cache_count = len(self.fake.cache_calls)
        launch_count = len(self.fake.launch_calls)

        second = self.fake.prepare()

        self.assertEqual(second["action"], "reused")
        self.assertFalse(second["built"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["fingerprint"], first["fingerprint"])
        self.assertEqual(second["binaries"], first["binaries"])
        self.assertEqual(len(self.fake.created), create_count)
        self.assertEqual(len(self.fake.applied), apply_count)
        self.assertEqual(len(self.fake.cache_calls), cache_count)
        self.assertEqual(len(self.fake.launch_calls), launch_count)

    def test_invalid_stamp_identity_source_or_contract_is_rejected(self):
        result = self.fake.prepare()
        path = Path(result["stamp"])
        original = json.loads(path.read_text(encoding="utf-8"))

        def change(field, value):
            def mutate(stamp):
                stamp[field] = value
            return mutate

        cases = (
            ("fingerprint", change("fingerprint", "0" * 64)),
            ("base", change("base", "e" * 40)),
            ("patch-order", lambda stamp: stamp["patches"].reverse()),
            ("patch-hash", lambda stamp: stamp["patches"][0].update(sha256="0" * 64)),
            ("toolchain", lambda stamp: stamp["toolchain"].update(host="other-host")),
            ("source-tree", lambda stamp: stamp["source"].update(tree="f" * 40)),
            ("helper-contract", lambda stamp: stamp["build"]["helper"].update(contract_version=999)),
            ("success-state", change("status", "running")),
        )
        for name, mutate in cases:
            with self.subTest(invalid_stamp=name):
                candidate = deepcopy(original)
                mutate(candidate)
                path.write_text(json.dumps(candidate), encoding="utf-8")
                self.assertIsNone(self.fake.valid_stamp())
        path.write_text("{", encoding="utf-8")
        self.assertIsNone(self.fake.valid_stamp())

    def test_missing_changed_or_mislabeled_binaries_invalidate_stamp(self):
        result = self.fake.prepare()
        for name, record in result["binaries"].items():
            with self.subTest(binary=name):
                path = Path(record["path"])
                original = path.read_bytes()
                path.write_bytes(b"tampered")
                self.assertIsNone(self.fake.valid_stamp())
                path.write_bytes(original)
                self.assertIsNotNone(self.fake.valid_stamp())
                path.unlink()
                self.assertIsNone(self.fake.valid_stamp())
                path.write_bytes(original)
                self.assertIsNotNone(self.fake.valid_stamp())

        stamp_path = Path(result["stamp"])
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
        stamp["binaries"]["jcode"]["path"] = str(self.root / ".patched-jcode" / "jcode")
        stamp_path.write_text(json.dumps(stamp), encoding="utf-8")
        self.assertIsNone(self.fake.valid_stamp())

        changed = Path(result["binaries"]["jcode"]["path"])
        changed.write_bytes(b"needs-a-new-build")
        launches = len(self.fake.launch_calls)
        rebuilt = self.fake.prepare()
        self.assertEqual(rebuilt["action"], "built")
        self.assertEqual(len(self.fake.launch_calls), launches + 1)

    def test_failed_build_has_persistent_exit_status_and_is_retried(self):
        fingerprint = self.fake.fingerprint()
        self.fake.exit_code = 19
        with self.assertRaisesRegex(CatalogError, "exit status 19"):
            self.fake.prepare()

        self.assertFalse(success_stamp_path(self.root, fingerprint).exists())
        status_path = build_status_path(self.root, fingerprint)
        status = json.loads(status_path.read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["exit_code"], 19)
        self.assertIn(
            "worker-eval-exit-status: 19",
            build_log_path(self.root, fingerprint).read_text(encoding="utf-8"),
        )

        self.fake.exit_code = 0
        result = self.fake.prepare()
        self.assertEqual(result["action"], "built")
        self.assertEqual(len(self.fake.launch_calls), 2)

    @unittest.skipIf(os.name == "nt", "Unix process-group interruption")
    def test_interrupted_build_terminates_child_and_leaves_no_stamp(self):
        fingerprint = self.fake.fingerprint()
        self.fake.interrupted = True
        with self.assertRaises(KeyboardInterrupt):
            self.fake.prepare()

        self.assertFalse(success_stamp_path(self.root, fingerprint).exists())
        status = json.loads(build_status_path(self.root, fingerprint).read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "interrupted")
        self.assertEqual(status["exit_code"], -signal.SIGTERM)
        self.assertIn((status["pid"], signal.SIGTERM), self.fake.terminations)
        self.assertIn(
            "worker-eval-build-status: interrupted",
            build_log_path(self.root, fingerprint).read_text(encoding="utf-8"),
        )

        self.fake.interrupted = False
        result = self.fake.prepare()
        self.assertEqual(result["action"], "built")
        self.assertEqual(len(self.fake.launch_calls), 2)

    def test_catalog_edit_during_build_is_rejected_without_stamp(self):
        fingerprint = self.fake.fingerprint()

        def edit_catalog():
            path = self.root / "patches" / PATCH_NAMES[1]
            path.write_text(path.read_text(encoding="utf-8") + "\nmid-build edit\n", encoding="utf-8")

        self.fake.build_hook = edit_catalog
        with self.assertRaisesRegex(CatalogError, "changed during worker evaluation build"):
            self.fake.prepare()

        self.assertFalse(success_stamp_path(self.root, fingerprint).exists())
        status = json.loads(build_status_path(self.root, fingerprint).read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "rejected")
        self.assertEqual(status["exit_code"], 0)
        self.assertIn("changed during worker evaluation build", status["rejection"])
        self.assertFalse(success_stamp_path(self.root, self.fake.fingerprint()).exists())

    def test_source_edit_during_build_is_rejected_without_stamp(self):
        fingerprint = self.fake.fingerprint()
        self.fake.build_hook = lambda: setattr(self.fake, "source_dirty", " M src/lib.rs")
        with self.assertRaisesRegex(CatalogError, "source is dirty"):
            self.fake.prepare()

        self.assertFalse(success_stamp_path(self.root, fingerprint).exists())
        status = json.loads(build_status_path(self.root, fingerprint).read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "rejected")
        self.assertIn("source is dirty", status["rejection"])

    def test_builder_holds_exclusive_lock_while_child_runs(self):
        conflicts = []

        def check_lock():
            try:
                with exclusive_lock(evaluation_directory(self.root) / "build.lock", "competing builder"):
                    pass
            except CatalogError:
                conflicts.append(True)

        self.fake.build_hook = check_lock
        self.fake.prepare()
        self.assertEqual(conflicts, [True])

    def test_recipe_can_hold_build_lock_across_evaluation(self):
        lock_path = evaluation_directory(self.root) / "build.lock"
        with exclusive_lock(lock_path, "build and live evaluation") as lock:
            result = self.fake.prepare(held_lock=lock)
            self.assertEqual(result["action"], "built")
            with self.assertRaises(CatalogError):
                with exclusive_lock(lock_path, "concurrent evaluation"):
                    pass

    def test_main_stdout_is_one_json_result_with_helper_noise_on_stderr(self):
        scripts = Path(__file__).resolve().parents[1]
        program = """
import os
from pathlib import Path
import sys
from jcode_catalog import prepare_worker_eval_build as gate

def fake_prepare(root):
    os.write(1, b"native helper noise\\n")
    print("python helper noise")
    return {"action": "reused", "binary_paths": {"jcode": "/mock/jcode"}}

gate.repository_root = lambda: Path("/catalog")
gate.prepare = fake_prepare
sys.argv = ["jcode_catalog.prepare_worker_eval_build.py"]
raise SystemExit(gate.main())
"""
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(scripts)
        result = subprocess.run(
            [sys.executable, "-c", program],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "action": "reused",
            "binary_paths": {"jcode": "/mock/jcode"},
        })
        self.assertIn("native helper noise", result.stderr)
        self.assertIn("python helper noise", result.stderr)


if __name__ == "__main__":
    unittest.main()
