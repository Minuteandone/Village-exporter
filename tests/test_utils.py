import unittest

from aivillage_export.utils import safe_slug, village_day_bounds


class UtilsTests(unittest.TestCase):
    def test_safe_slug(self):
        self.assertEqual(safe_slug("Actual Launch 1!"), "actual-launch-1")

    def test_pacific_day_dst(self):
        start, end = village_day_bounds("2026-09-11")
        self.assertEqual((end - start).total_seconds(), 24 * 3600)
        self.assertEqual(start.isoformat(), "2026-09-11T07:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
