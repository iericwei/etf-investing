import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from etf_investing import strategy as etf_strategy


def make_history(closes, volumes=None):
    volumes = volumes or [1000 + i * 10 for i in range(len(closes))]
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=len(closes), freq="B"),
        "open": closes,
        "close": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "volume": volumes,
    })


class TradeSignalBacktestTests(unittest.TestCase):
    def test_model_trade_signal_exposes_buy_and_sell_fields(self):
        bullish = make_history([10 + i * 0.12 for i in range(45)])
        signal = etf_strategy.compute_trade_signal(bullish)

        self.assertIn("action", signal)
        self.assertIn(signal["action"], {"buy", "hold", "sell"})
        self.assertIn("buy_signals", signal)
        self.assertIn("sell_signals", signal)
        self.assertIn("买", signal["label"])
        self.assertEqual(signal["action"], "buy")

    def test_backtest_model_returns_one_month_strategy_return(self):
        closes = [10 + i * 0.08 for i in range(45)]
        df = make_history(closes)
        bt = etf_strategy.backtest_model(df, window=22)

        self.assertEqual(bt["window_days"], 22)
        self.assertIn("return_pct", bt)
        self.assertIn("trades", bt)
        self.assertGreater(bt["return_pct"], 0)

    def test_backtest_model_exposes_curve_and_trade_points_for_tooltip(self):
        closes = [10 + i * 0.08 for i in range(45)]
        df = make_history(closes)
        bt = etf_strategy.backtest_model(df, window=22)

        self.assertIn("curve", bt)
        self.assertIn("trade_points", bt)
        self.assertGreater(len(bt["curve"]), 0)
        self.assertGreater(len(bt["trade_points"]), 0)
        self.assertEqual({"date", "close", "return_pct"}, set(bt["curve"][0].keys()))
        first_trade = bt["trade_points"][0]
        self.assertIn(first_trade["action"], {"buy", "sell"})
        self.assertIn("date", first_trade)
        self.assertIn("price", first_trade)
        self.assertIn("reason", first_trade)
        self.assertIn("return_pct", first_trade)

    def test_select_top_adds_backtest_return_to_every_listed_symbol(self):
        pool = [{"code": f"AAA{i:02d}", "name": f"ETF{i}", "category": "测试"} for i in range(12)]
        etf_map = {
            item["code"]: make_history([10 + i * 0.05 + n * 0.01 for n in range(45)])
            for i, item in enumerate(pool)
        }
        results = etf_strategy.select_top(pool, etf_map, {}, top_n=12)

        self.assertEqual(len(results), 12)
        for row in results:
            self.assertIn("trade_signal", row)
            self.assertIn("backtest_return_pct", row)
            self.assertIsInstance(row["backtest_return_pct"], float)
            self.assertIsNotNone(row["backtest"])
            self.assertIn("curve", row["backtest"])
            self.assertIn("trade_points", row["backtest"])
            self.assertGreater(len(row["backtest"]["curve"]), 0)


if __name__ == "__main__":
    unittest.main()
