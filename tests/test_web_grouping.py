import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from etf_investing import web_app
from etf_investing.web_app import _group_by_target, _target_group_name


class WebTargetGroupingTests(unittest.TestCase):
    def test_target_group_name_strips_etf_and_issuer_suffix(self):
        self.assertEqual(_target_group_name("半导体设备ETF国泰"), "半导体设备")
        self.assertEqual(_target_group_name("半导体设备ETF招商"), "半导体设备")
        self.assertEqual(_target_group_name("5G ETF"), "5G")
        self.assertEqual(_target_group_name("红利低波ETF"), "红利低波")

    def test_group_by_target_merges_same_underlying_names(self):
        rows = [
            {"code": "111111", "name": "半导体设备ETF国泰", "category": "科技", "score": 80},
            {"code": "222222", "name": "证券ETF", "category": "金融", "score": 70},
            {"code": "333333", "name": "半导体设备ETF招商", "category": "科技", "score": 90},
        ]

        grouped = _group_by_target(rows)

        headers = [r["category"] for r in grouped if r.get("_is_group_header")]
        self.assertEqual(headers, ["半导体设备", "证券"])
        first_group_codes = [r["code"] for r in grouped[1:3]]
        self.assertEqual(first_group_codes, ["333333", "111111"])
        self.assertEqual([r["rank"] for r in grouped if not r.get("_is_group_header")], [1, 2, 3])

    def test_holdings_realtime_skips_group_headers_in_cached_results(self):
        with web_app._lock:
            old_cache = dict(web_app._cache)
            web_app._cache.update(
                results=[
                    {"_is_group_header": True, "category": "半导体"},
                    {"code": "111111", "rank": 1},
                ],
                etf_map={},
            )
        try:
            with patch.object(web_app, "_load_holdings", return_value=["111111"]), \
                 patch.object(web_app, "fetch_realtime", return_value={"111111": {"name": "半导体ETF国泰", "price": 1.23, "change_pct": 0.5, "amount": 1000}}), \
                 patch.object(web_app, "_universe_meta", return_value={"111111": {"category": "科技"}}):
                res = web_app.app.test_client().get("/api/holdings/realtime")

            self.assertEqual(res.status_code, 200)
            payload = res.get_json()
            self.assertEqual(payload["data"][0]["code"], "111111")
            self.assertEqual(payload["data"][0]["rank"], 1)
        finally:
            with web_app._lock:
                web_app._cache.clear()
                web_app._cache.update(old_cache)


if __name__ == "__main__":
    unittest.main()
