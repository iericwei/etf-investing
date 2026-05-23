import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from etf_investing import universe as etf_universe
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

    def test_fetch_universe_prefers_mootdx_then_skips_later_sources(self):
        items = [{"code": "588200", "name": "测试ETF", "amount": 100_000_000, "fund_size": 1}]
        with patch.object(etf_universe, "_CACHE") as cache, \
             patch.object(etf_universe, "_fetch_universe_mootdx", return_value=items) as mootdx, \
             patch.object(etf_universe, "_fetch_universe_futu") as futu, \
             patch.object(etf_universe.requests, "Session") as session:
            cache.exists.return_value = False
            result = etf_universe.fetch_universe(min_amount=50_000_000, max_count=10, force=True)

        self.assertEqual(result, items)
        mootdx.assert_called_once()
        futu.assert_not_called()
        session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
