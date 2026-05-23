import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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
        bt = etf_strategy.backtest_model(df, window=22, scheme_name="before_close_15m")

        self.assertEqual(bt["window_days"], 22)
        self.assertIn("return_pct", bt)
        self.assertIn("trades", bt)
        self.assertGreater(bt["return_pct"], 0)

    def test_backtest_model_exposes_curve_and_trade_points_for_tooltip(self):
        closes = [10 + i * 0.08 for i in range(45)]
        df = make_history(closes)
        bt = etf_strategy.backtest_model(df, window=22, scheme_name="before_close_15m")

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

    def test_backtest_model_uses_before_close_15m_scheme(self):
        closes = [10 + i * 0.08 for i in range(45)]
        df = make_history(closes)
        bt = etf_strategy.backtest_model(df, scheme_name="before_close_15m")

        self.assertEqual(bt["scheme"], "before_close_15m")
        self.assertEqual(bt["scheme_display_name"], "收盘前15分钟")
        self.assertEqual(bt["trade_time"], "14:45")
        self.assertEqual(bt["trade_timing_label"], "收盘前15分钟")
        self.assertGreater(len(bt["trade_points"]), 0)
        self.assertEqual(bt["trade_points"][0]["time"], "14:45")
        self.assertIn("收盘前15分钟", bt["trade_points"][0]["label"])

    def test_backtest_model_defaults_to_eric_c3_four_window_scheme(self):
        closes = [10 + i * 0.04 + ((i % 6) - 2) * 0.02 for i in range(65)]
        df = make_history(closes)
        bt = etf_strategy.backtest_model(df, selection_score=90)

        self.assertEqual(bt["scheme"], "eric_c3_four_window")
        self.assertEqual(bt["scheme_display_name"], "Eric C3 四窗口回测")
        self.assertEqual(bt["trade_windows"], ["09:35", "11:30", "13:05", "14:45"])
        self.assertEqual(bt["trade_timing_label"], "四窗口")

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

    def test_backtest_model_uses_local_store_price_before_futu_and_daily_k(self):
        closes = [10 + i * 0.08 for i in range(45)]
        df = make_history(closes)
        trade_date = pd.to_datetime(df.iloc[-22]["date"]).normalize()
        local_intraday = pd.DataFrame({
            "datetime": [trade_date + pd.Timedelta(hours=14, minutes=45)],
            "time": ["14:45"],
            "close": [88.88],
        })

        class DummyStore:
            def __init__(self):
                self.saved = []
            def load_intraday(self, code, period, start_date, end_date):
                self.loaded = (code, period, start_date, end_date)
                return local_intraday
            def save_intraday(self, code, period, data, source):
                self.saved.append((code, period, source, len(data)))
                return len(data)

        store = DummyStore()
        with patch.object(etf_strategy, "fetch_futu_intraday_history") as futu:
            bt = etf_strategy.backtest_model(
                df,
                window=22,
                scheme_name="before_close_15m",
                code="513180",
                market_data_store=store,
            )

        self.assertFalse(futu.called)
        self.assertEqual(bt["execution_price"], "local")
        self.assertEqual(bt["price_source_label"], "local")
        first_trade = bt["trade_points"][0]
        self.assertEqual(first_trade["price"], 88.88)
        self.assertEqual(first_trade["price_source"], "local")
        self.assertEqual(first_trade["price_source_label"], "local")

    def test_backtest_model_fetches_futu_when_local_missing_and_saves_to_store(self):
        closes = [10 + i * 0.08 for i in range(45)]
        df = make_history(closes)
        trade_date = pd.to_datetime(df.iloc[-22]["date"]).normalize()
        futu_intraday = pd.DataFrame({
            "datetime": [trade_date + pd.Timedelta(hours=14, minutes=45)],
            "time": ["14:45"],
            "close": [77.77],
        })

        class DummyStore:
            def __init__(self):
                self.saved = []
            def load_intraday(self, code, period, start_date, end_date):
                return pd.DataFrame()
            def save_intraday(self, code, period, data, source):
                self.saved.append((code, period, source, len(data)))
                return len(data)

        store = DummyStore()
        result = type("Result", (), {"df": futu_intraday, "source": "futu", "error": None})()
        with patch.object(etf_strategy, "fetch_futu_intraday_history", return_value=result) as futu:
            bt = etf_strategy.backtest_model(
                df,
                window=22,
                scheme_name="before_close_15m",
                code="513180",
                market_data_store=store,
            )

        futu.assert_called_once()
        self.assertEqual(store.saved, [("513180", "5", "futu", 1)])
        self.assertEqual(bt["execution_price"], "futu")
        first_trade = bt["trade_points"][0]
        self.assertEqual(first_trade["price"], 77.77)
        self.assertEqual(first_trade["price_source"], "futu")
        self.assertEqual(first_trade["price_source_label"], "futu")

    def test_backtest_model_falls_back_to_daily_k_price_source_label(self):
        closes = [10 + i * 0.08 for i in range(45)]
        df = make_history(closes)

        class DummyStore:
            def load_intraday(self, code, period, start_date, end_date):
                return pd.DataFrame()
            def save_intraday(self, code, period, data, source):
                raise AssertionError("empty futu data should not be saved")

        empty_result = type("Result", (), {"df": pd.DataFrame(), "source": "futu", "error": None})()
        with patch.object(etf_strategy, "fetch_futu_intraday_history", return_value=empty_result):
            bt = etf_strategy.backtest_model(
                df,
                window=22,
                scheme_name="before_close_15m",
                code="513180",
                market_data_store=DummyStore(),
            )

        self.assertEqual(bt["execution_price"], "日k")
        first_trade = bt["trade_points"][0]
        self.assertEqual(first_trade["price_source"], "日k")
        self.assertEqual(first_trade["price_source_label"], "日k")

    def test_backtest_model_ignores_legacy_intraday_when_code_forces_daily_k_after_futu_empty(self):
        closes = [10 + i * 0.08 for i in range(45)]
        df = make_history(closes)
        trade_date = pd.to_datetime(df.iloc[-22]["date"]).normalize()
        intraday = pd.DataFrame({
            "datetime": [trade_date + pd.Timedelta(hours=14, minutes=45)],
            "date": [trade_date],
            "time": ["14:45"],
            "close": [88.88],
        })
        class DummyStore:
            def load_intraday(self, code, period, start_date, end_date):
                return pd.DataFrame()
            def save_intraday(self, code, period, data, source):
                raise AssertionError("empty futu data should not be saved")

        store = DummyStore()
        empty_result = type("Result", (), {"df": pd.DataFrame(), "source": "futu", "error": None})()

        with patch("etf_investing.strategy.fetch_futu_intraday_history", return_value=empty_result):
            bt = etf_strategy.backtest_model(
                df,
                window=22,
                scheme_name="before_close_15m",
                code="513180",
                intraday=intraday,
                market_data_store=store,
            )

        first_trade = bt["trade_points"][0]
        self.assertEqual(first_trade["price_source"], "日k")
        self.assertNotEqual(first_trade["price"], 88.88)


if __name__ == "__main__":
    unittest.main()
