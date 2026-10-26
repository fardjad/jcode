"""Shared metadata and command classification contracts."""
import unittest
from .metadata import is_rust_test, parse
from .snapshot_patches_test import message


class MetadataTests(unittest.TestCase):
    def test_source_and_mail_message_have_identical_metadata(self):
        source = message(name="1001-personal-feature-example.patch")
        mail = "From " + "a" * 40 + " date\nSubject: [PATCH] example\n\n" + source + "\n---\ndiff --git a/a b/a\n"
        self.assertEqual(parse(source), parse(mail))

    def test_empty_sections_and_duplicate_headers_rejected(self):
        source = message(name="1001-personal-feature-example.patch")
        with self.assertRaises(ValueError):
            parse(source + "\nX-Jcode-Patch-Kind: personal-feature\n")
        with self.assertRaises(ValueError):
            parse(source.replace("Validation:\necho test\n", "Validation:\n"))

    def test_both_rust_test_launch_forms_are_recognized(self):
        for command in ["cargo test -p base", "scripts/cargo_exec.sh test -p base", "  cargo   test --lib"]:
            self.assertTrue(is_rust_test(command), command)
        for command in ["cargo test-extra", "echo cargo test", "cargo build", "bash -c 'cargo test'"]:
            self.assertFalse(is_rust_test(command), command)


if __name__ == "__main__":
    unittest.main()
