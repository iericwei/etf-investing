import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from etf_investing import data as etf_data


class FundMetricsTests(unittest.TestCase):
    def test_parse_tencent_etf_quote_metrics(self):
        fields = [""] * 88
        fields[1] = "恒生科技ETF华夏"
        fields[2] = "513180"
        fields[3] = "0.614"
        fields[4] = "0.621"
        fields[6] = "56667839"
        fields[30] = "20260521141629"
        fields[32] = "-1.13"
        fields[37] = "351756"
        fields[44] = "511.39"
        fields[77] = "0.10"
        fields[78] = "0.6134"

        parsed = etf_data._parse_tencent_realtime_fields(fields)

        quote = parsed["513180"]
        self.assertEqual(quote["name"], "恒生科技ETF华夏")
        self.assertEqual(quote["source"], "tencent")
        self.assertAlmostEqual(quote["fund_size"], 511.39 * 1e8)
        self.assertEqual(quote["premium_rate_pct"], 0.10)
        self.assertEqual(quote["estimate_nav"], 0.6134)
        self.assertEqual(quote["nav_date"], "2026-05-21 14:16:29")
        self.assertEqual(quote["metric_source"], "tencent")

    def test_fetch_fund_quote_metrics_prefers_tencent(self):
        with patch.object(etf_data, "_realtime_tencent", return_value={
            "513180": {
                "name": "恒生科技ETF华夏",
                "fund_size": 511.39 * 1e8,
                "premium_rate_pct": 0.10,
                "estimate_nav": 0.6134,
                "nav_date": "2026-05-21 14:16:29",
                "metric_source": "tencent",
            }
        }) as realtime, patch.object(etf_data.requests, "Session") as session:
            metrics = etf_data.fetch_fund_quote_metrics(["513180"])

        realtime.assert_called_once_with(["513180"])
        session.assert_not_called()
        self.assertEqual(metrics["513180"]["metric_source"], "tencent")
        self.assertEqual(metrics["513180"]["premium_rate_pct"], 0.10)


if __name__ == "__main__":
    unittest.main()
