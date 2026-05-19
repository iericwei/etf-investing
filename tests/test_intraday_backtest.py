import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from etf_investing import data as etf_data
from etf_investing import strategy as etf_strategy
from etf_investing import web_app


def make_history(closes):
    return pd.DataFrame({
        "date": pd.date_range("2026-01-01", periods=len(closes), freq="B"),
        "open": closes,
        "close": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "volume": [1000 + i * 10 for i in range(len(closes))],
    })


class AkshareIntradayBacktestTests(unittest.TestCase):
    def test_fetch_etf_15m_history_normalizes_akshare_columns(self):
        raw = pd.DataFrame({
            "时间": ["2026-05-18 14:30:00", "2026-05-18 14:45:00"],
            "开盘": [1.00, 1.10],
            "收盘": [1.05, 1.15],
            "最高": [1.06, 1.16],
            "最低": [0.99, 1.09],
            "成交量": [100, 200],
            "成交额": [1000, 2300],
        })
        fake_ak = Mock()
        fake_ak.fund_etf_hist_min_em.return_value = raw

        with patch.dict(sys.modules, {"akshare": fake_ak}):
            df = etf_data.fetch_etf_15m_history("513180", days=3)

        fake_ak.fund_etf_hist_min_em.assert_called_once()
        kwargs = fake_ak.fund_etf_hist_min_em.call_args.kwargs
        self.assertEqual(kwargs["symbol"], "513180")
        self.assertEqual(kwargs["period"], "15")
        self.assertEqual(kwargs["adjust"], "")
        self.assertEqual(list(df.columns), ["datetime", "date", "time", "open", "close", "high", "low", "volume", "amount"])
        self.assertEqual(str(df.loc[1, "date"].date()), "2026-05-18")
        self.assertEqual(df.loc[1, "time"], "14:45")
        self.assertEqual(df.loc[1, "close"], 1.15)

    def test_backtest_model_uses_akshare_15m_price_for_trade_points(self):
        closes = [10 + i * 0.08 for i in range(45)]
        daily = make_history(closes)
        trade_day = daily.iloc[-22]["date"].normalize()
        intraday = pd.DataFrame({
            "datetime": [trade_day + pd.Timedelta(hours=14, minutes=30), trade_day + pd.Timedelta(hours=14, minutes=45)],
            "date": [trade_day, trade_day],
            "time": ["14:30", "14:45"],
            "open": [99.0, 100.0],
            "close": [99.5, 123.45],
            "high": [100.0, 124.0],
            "low": [98.0, 100.0],
            "volume": [1000, 1000],
            "amount": [100000, 123450],
        })

        bt = etf_strategy.backtest_model(daily, window=22, intraday=intraday)

        first_trade = bt["trade_points"][0]
        self.assertEqual(first_trade["time"], "14:45")
        self.assertEqual(first_trade["price"], 123.45)
        self.assertEqual(first_trade["price_source"], "akshare_15m")
        self.assertEqual(first_trade["price_source_label"], "akshare 15分钟分时行情价")
        self.assertEqual(bt["execution_price"], "akshare_15m")
        self.assertIn("akshare", bt["price_note"].lower())

    def test_backtest_model_describes_daily_kline_price_when_intraday_missing(self):
        closes = [10 + i * 0.08 for i in range(45)]
        daily = make_history(closes)

        bt = etf_strategy.backtest_model(daily, window=22, intraday=pd.DataFrame())

        first_trade = bt["trade_points"][0]
        self.assertEqual(first_trade["price_source"], "close")
        self.assertEqual(first_trade["price_source_label"], "日K收盘价")

    def test_web_backtest_fetches_akshare_15m_before_running_model(self):
        daily = make_history([10 + i * 0.08 for i in range(45)])
        intraday = pd.DataFrame({
            "datetime": [pd.Timestamp("2026-05-18 14:45:00")],
            "date": [pd.Timestamp("2026-05-18")],
            "time": ["14:45"],
            "open": [1.0],
            "close": [1.1],
            "high": [1.1],
            "low": [1.0],
            "volume": [100],
            "amount": [110],
        })
        with web_app._lock:
            old_cache = dict(web_app._cache)
            old_backtest = dict(web_app._backtest_state)
            web_app._cache.update(status="ready", results=[{"code": "513180"}], etf_map={"513180": daily})
            web_app._backtest_state.update(status="idle", date=None)
        try:
            with patch.object(web_app, "fetch_etf_15m_history", return_value=intraday) as fetch_15m, \
                 patch.object(web_app, "backtest_model", return_value={"return_pct": 1.23}) as run_model:
                web_app._run_backtest_async(force=True)

            fetch_15m.assert_called_once_with("513180", days=44)
            self.assertIs(run_model.call_args.kwargs["intraday"], intraday)
            with web_app._lock:
                self.assertEqual(web_app._cache["results"][0]["backtest_return_pct"], 1.23)
        finally:
            with web_app._lock:
                web_app._cache.clear()
                web_app._cache.update(old_cache)
                web_app._backtest_state.clear()
                web_app._backtest_state.update(old_backtest)


if __name__ == "__main__":
    unittest.main()
