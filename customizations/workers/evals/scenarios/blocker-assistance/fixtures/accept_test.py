import unittest
from parser import parse


class Acceptance(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse(" temp = 12 "), ("temp", 12))
        self.assertEqual(parse("x=-4"), ("x", -4))

    def test_invalid(self):
        for line in ("=3", "x", "x=no", "x=1=2"):
            with self.subTest(line=line), self.assertRaises(ValueError):
                parse(line)
