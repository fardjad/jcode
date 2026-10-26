"""Real compatibility runner smoke test using a disposable tiny Rust workspace."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from .validate_patch_commands import validate
from .run_nextest import run


class CompatibilityCacheTests(unittest.TestCase):
    def test_real_nextest_preserves_public_report_with_separated_cache(self):
        catalog = Path(__file__).resolve().parents[4]
        binaries = list((catalog / ".tools/nextest/0.9.143").glob("*/cargo-nextest"))
        if not binaries:
            self.skipTest("pinned nextest not cached, full workflow bootstraps it")
        with tempfile.TemporaryDirectory(prefix="catalog-nextest-smoke-") as temporary:
            root = Path(temporary)
            source = root / "source"
            (source / "src").mkdir(parents=True)
            (source / "Cargo.toml").write_text('[package]\nname="jcode"\nversion="0.0.0"\nedition="2021"\n')
            (source / "src/main.rs").write_text('fn main() {}\n')
            (source / "src/lib.rs").write_text('#[test] fn smoke() { assert_eq!(2 + 2, 4); }\n')
            (root / "nextest.toml").write_bytes((catalog / "nextest.toml").read_bytes())
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run(["git", "add", "."], cwd=source, check=True)
            subprocess.run(["git", "commit", "-qm", "disposable fixture"], cwd=source, check=True)
            (root / "patches").mkdir()
            (root / "patches/0001-smoke.patch").write_text(
                "X-Jcode-Patch-Depends-On: none\nX-Jcode-Patch-Intent: smoke\n"
                "X-Jcode-Patch-Kind: upstream-candidate\n\nValidation:\n"
                "cargo test --lib smoke\n---\n")
            validate(root, source, "0001-smoke.patch", "fixture-base")
            declared = list((root / ".tools/cargo-target/patch-validation").glob("*/debug/deps/*"))
            self.assertTrue(declared)
            self.assertEqual(run(str(binaries[0]), root, source, []), 0)
            report = source / "target/nextest/compat/junit.xml"
            self.assertTrue(report.is_file())
            self.assertIn("smoke", report.read_text())
            caches = list((root / ".tools/cargo-target/compatibility").glob("*/debug/deps/libjcode*.rlib"))
            self.assertEqual(len(caches), 1)
            self.assertFalse((source / "target/debug").exists())


if __name__ == "__main__":
    unittest.main()
