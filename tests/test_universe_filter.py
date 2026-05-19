import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from etf_investing.universe import _apply_filter


class UniverseFilterTests(unittest.TestCase):
    def test_apply_filter_falls_back_to_top_amount_when_early_volume_below_threshold(self):
        items = [
            {"code": "588200", "amount": 25_000_000},
            {"code": "513090", "amount": 24_000_000},
            {"code": "562590", "amount": 19_000_000},
        ]

        result = _apply_filter(items, min_amount=50_000_000, max_count=2)

        self.assertEqual([item["code"] for item in result], ["588200", "513090"])


if __name__ == "__main__":
    unittest.main()
