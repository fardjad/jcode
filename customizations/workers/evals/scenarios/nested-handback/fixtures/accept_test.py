import unittest
from stages import clean_record, split_record


class Acceptance(unittest.TestCase):
    def test_stages(self):
        self.assertEqual(split_record(" pear | 4 "), [" pear ", " 4 "])
        self.assertEqual(clean_record(split_record(" pear | 4 ")), ("pear", 4))
        self.assertEqual(clean_record(split_record("x|-2")), ("x", -2))
        for line in ("x", "x|1|2"):
            with self.subTest(line=line), self.assertRaises(ValueError):
                split_record(line)
        for parts in ((" ", "3"), ("x", "bad")):
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                clean_record(parts)
