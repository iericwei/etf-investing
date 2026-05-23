import tempfile
import unittest
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from etf_investing import market_data
from etf_investing.market_data import MarketDataStore
from etf_investing import market_data_service
import backfill_intraday
import backfill_intraday_date


class MarketDataStoreTests(unittest.TestCase):
    def make_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return MarketDataStore(Path(tmp.name) / "market.sqlite3")

    def test_save_and_load_intraday_bars(self):
        store = self.make_store()
        df = pd.DataFrame(
            {
                "datetime": [pd.Timestamp("2026-05-19 14:45:00")],
                "date": [pd.Timestamp("2026-05-19")],
                "time": ["14:45"],
                "open": [1.0],
                "close": [1.1],
                "high": [1.2],
                "low": [0.9],
                "volume": [100],
                "amount": [1100],
            }
        )

        saved = store.save_intraday("513180", "15", df, "test_source")
        loaded = store.load_intraday("513180", "15", date(2026, 5, 19), date(2026, 5, 19))

        self.assertEqual(saved, 1)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded.loc[0, "close"], 1.1)
        self.assertEqual(loaded.loc[0, "source"], "test_source")

    def test_fetch_current_intraday_falls_back_to_futu_when_current_source_empty(self):
        futu_df = pd.DataFrame(
            {
                "datetime": [pd.Timestamp("2026-05-23 14:45:00")],
                "time": ["14:45"],
                "open": [1.0],
                "close": [1.1],
                "high": [1.2],
                "low": [0.9],
                "volume": [100],
                "amount": [1100],
            }
        )
        with patch.object(market_data, "fetch_eastmoney_intraday_history", return_value=pd.DataFrame()), \
             patch.object(market_data, "_today_rows", side_effect=lambda df: df), \
             patch.object(market_data, "fetch_futu_intraday_history", return_value=market_data.IntradayFetchResult(futu_df, "futu")):
            result = market_data.fetch_current_intraday("513180", "15", days=3)

        self.assertEqual(result.source, "futu")
        self.assertEqual(len(result.df), 1)

    def test_get_intraday_from_store_or_fetch_saves_fetched_rows(self):
        store = self.make_store()
        fetched = pd.DataFrame(
            {
                "datetime": [pd.Timestamp("2026-05-23 14:45:00")],
                "time": ["14:45"],
                "open": [1.0],
                "close": [1.1],
                "high": [1.2],
                "low": [0.9],
                "volume": [100],
                "amount": [1100],
            }
        )
        with patch.object(market_data, "fetch_current_intraday", return_value=market_data.IntradayFetchResult(fetched, "futu")), \
             patch.object(market_data, "_today_rows", side_effect=lambda df: df):
            df, source, error = market_data.get_intraday_from_store_or_fetch("513180", "15", 3, refresh=True, store=store)

        self.assertIsNone(error)
        self.assertIn("futu", source)
        self.assertEqual(len(df), 1)
        self.assertEqual(store.recent_logs(), [])

    def test_market_data_service_logs_intraday_request(self):
        store = self.make_store()
        df = pd.DataFrame(
            {
                "datetime": [pd.Timestamp("2026-05-19 14:45:00")],
                "time": ["14:45"],
                "open": [1.0],
                "close": [1.1],
                "high": [1.2],
                "low": [0.9],
                "volume": [100],
                "amount": [1100],
                "source": ["futu"],
            }
        )
        old_store = market_data_service._STORE
        market_data_service._STORE = store
        try:
            with patch.object(market_data_service, "get_intraday_from_store_or_fetch", return_value=(df, "local_store+futu", None)):
                res = market_data_service.app.test_client().get("/intraday?code=513180&period=15&days=3")
        finally:
            market_data_service._STORE = old_store

        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["source"], "local_store+futu")
        logs = store.recent_logs(limit=1)
        self.assertEqual(logs[0]["endpoint"], "intraday")
        self.assertEqual(logs[0]["rows"], 1)

    def test_market_data_service_intraday_defaults_to_5_minute_period(self):
        store = self.make_store()
        df = pd.DataFrame(
            {
                "datetime": [pd.Timestamp("2026-05-19 14:55:00")],
                "time": ["14:55"],
                "open": [1.0],
                "close": [1.1],
                "high": [1.2],
                "low": [0.9],
                "volume": [100],
                "amount": [1100],
                "source": ["futu"],
            }
        )
        old_store = market_data_service._STORE
        market_data_service._STORE = store
        try:
            with patch.object(market_data_service, "get_intraday_from_store_or_fetch", return_value=(df, "local_store+futu", None)) as fetch:
                res = market_data_service.app.test_client().get("/intraday?code=513180&days=3")
        finally:
            market_data_service._STORE = old_store

        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertEqual(payload["period"], "5")
        fetch.assert_called_once_with("513180", "5", 3, refresh=False, store=store)
        logs = store.recent_logs(limit=1)
        self.assertEqual(logs[0]["period"], "5")

    def test_backfill_cli_uses_combined_pool_and_one_month_futu_history_by_default(self):
        selected = [{"code": "513180", "name": "恒生科技ETF"}, {"code": "159915", "name": "创业板ETF"}]
        with patch.object(backfill_intraday, "build_backfill_pool", return_value=selected) as build_pool, \
             patch.object(backfill_intraday, "backfill_intraday_history", return_value={"success": True}) as backfill, \
             patch.object(sys, "argv", ["backfill_intraday.py"]):
            exit_code = backfill_intraday.main()

        self.assertEqual(exit_code, 0)
        build_pool.assert_called_once()
        backfill.assert_called_once_with(["513180", "159915"], period="5", days=30, start_date=None, end_date=None)

    def test_backfill_cli_accepts_date_range(self):
        with patch.object(backfill_intraday, "backfill_intraday_history", return_value={"success": True}) as backfill, \
             patch.object(sys, "argv", [
                 "backfill_intraday.py",
                 "--codes", "513180,159915",
                 "--start-date", "2026-05-18",
                 "--end-date", "2026-05-20",
             ]):
            exit_code = backfill_intraday.main()

        self.assertEqual(exit_code, 0)
        backfill.assert_called_once_with(
            ["513180", "159915"],
            period="5",
            days=30,
            start_date=date(2026, 5, 18),
            end_date=date(2026, 5, 20),
        )

    def test_build_model_selected_pool_uses_full_market_model_results(self):
        universe = [{"code": "513180", "name": "A"}, {"code": "159915", "name": "B"}]
        etf_map = {"513180": pd.DataFrame({"close": [1, 2]}), "159915": pd.DataFrame({"close": [1, 3]})}
        realtime = {"513180": {"price": 2.0}, "159915": {"price": 3.0}}
        model_results = [
            {"code": "159915", "name": "创业板ETF", "category": "创业板"},
            {"code": "513180", "name": "恒生科技ETF", "category": "港股"},
        ]
        with patch.object(backfill_intraday, "fetch_universe", return_value=universe) as fetch_universe, \
             patch.object(backfill_intraday, "fetch_all_history", return_value=etf_map) as fetch_history, \
             patch.object(backfill_intraday, "fetch_realtime", return_value=realtime) as fetch_rt, \
             patch.object(backfill_intraday, "select_top", return_value=model_results) as select:
            selected = backfill_intraday.build_model_selected_pool(top=2)

        fetch_universe.assert_called_once()
        fetch_history.assert_called_once()
        fetch_rt.assert_called_once_with(["513180", "159915"])
        select.assert_called_once()
        self.assertEqual([item["code"] for item in selected], ["159915", "513180"])

    def test_build_backfill_pool_merges_leaderboard_watchlist_holdings_and_hard_filtered_universe(self):
        hard_filtered = [
            {"code": "510300", "name": "沪深300ETF", "category": "宽基"},
            {"code": "159915", "name": "创业板ETF", "category": "创业板"},
        ]
        model_selected = [
            {"code": "513180", "name": "恒生科技ETF", "category": "港股"},
            {"code": "159915", "name": "创业板ETF", "category": "创业板"},
        ]
        with patch.object(backfill_intraday, "fetch_universe", return_value=hard_filtered), \
             patch.object(backfill_intraday, "fetch_all_history", return_value={"513180": pd.DataFrame(), "159915": pd.DataFrame()}), \
             patch.object(backfill_intraday, "fetch_realtime", return_value={}), \
             patch.object(backfill_intraday, "select_top", return_value=model_selected), \
             patch.object(backfill_intraday, "_load_watchlist_codes", return_value=["588000", "513180"]), \
             patch.object(backfill_intraday, "_load_holding_codes", return_value=["512480"]):
            pool = backfill_intraday.build_backfill_pool(top=50)

        by_code = {item["code"]: item for item in pool}
        self.assertEqual(list(by_code), ["513180", "159915", "588000", "512480", "510300"])
        self.assertIn("leaderboard", by_code["513180"]["sources"])
        self.assertIn("watchlist", by_code["513180"]["sources"])
        self.assertEqual(by_code["588000"]["sources"], ["watchlist"])
        self.assertEqual(by_code["512480"]["sources"], ["holdings"])
        self.assertEqual(by_code["510300"]["sources"], ["hard_filter"])

    def test_backfill_intraday_history_saves_one_month_futu_rows_and_logs_days(self):
        store = self.make_store()
        futu_df = pd.DataFrame(
            {
                "datetime": [pd.Timestamp("2026-04-20 14:45:00"), pd.Timestamp("2026-05-19 14:45:00")],
                "time": ["14:45", "14:45"],
                "open": [1.0, 1.1],
                "close": [1.05, 1.15],
                "high": [1.1, 1.2],
                "low": [0.9, 1.0],
                "volume": [100, 200],
                "amount": [1000, 2000],
            }
        )
        with patch.object(market_data, "fetch_futu_intraday_history", return_value=market_data.IntradayFetchResult(futu_df, "futu")) as fetch_futu, \
             patch.object(market_data, "fetch_current_intraday") as fetch_current:
            result = market_data.backfill_intraday_history(["513180"], period="5", days=30, store=store)

        fetch_futu.assert_called_once_with("513180", period="5", days=30)
        self.assertFalse(fetch_current.called)
        self.assertTrue(result["success"])
        self.assertEqual(result["total_rows"], 2)
        loaded = store.load_intraday("513180", "5", date(2026, 4, 1), date(2026, 5, 31))
        self.assertEqual(len(loaded), 2)
        self.assertEqual(set(loaded["source"]), {"futu"})
        logs = store.recent_logs(limit=1)
        self.assertEqual(logs[0]["endpoint"], "backfill_history")
        self.assertEqual(logs[0]["days"], 30)

    def test_backfill_intraday_history_throttles_futu_batches(self):
        store = self.make_store()
        with patch.object(market_data, "fetch_futu_intraday_history", return_value=market_data.IntradayFetchResult(pd.DataFrame(), "none")), \
             patch.object(market_data, "fetch_current_intraday", return_value=market_data.IntradayFetchResult(pd.DataFrame(), "none")), \
             patch.object(market_data.time, "sleep") as sleep:
            market_data.backfill_intraday_history(["513180", "159915", "510300"], period="5", days=30, store=store, futu_batch_size=2, futu_pause_seconds=12.5)

        sleep.assert_called_once_with(12.5)

    def test_backfill_intraday_history_accepts_date_range(self):
        store = self.make_store()
        futu_df = pd.DataFrame(
            {
                "datetime": [
                    pd.Timestamp("2026-05-17 14:45:00"),
                    pd.Timestamp("2026-05-18 14:45:00"),
                    pd.Timestamp("2026-05-20 14:45:00"),
                    pd.Timestamp("2026-05-21 14:45:00"),
                ],
                "time": ["14:45", "14:45", "14:45", "14:45"],
                "open": [1.0, 1.1, 1.2, 1.3],
                "close": [1.05, 1.15, 1.25, 1.35],
                "high": [1.1, 1.2, 1.3, 1.4],
                "low": [0.9, 1.0, 1.1, 1.2],
                "volume": [100, 200, 300, 400],
                "amount": [1000, 2000, 3000, 4000],
            }
        )
        with patch.object(market_data, "fetch_futu_intraday_history", return_value=market_data.IntradayFetchResult(futu_df, "futu")) as fetch_futu, \
             patch.object(market_data, "fetch_eastmoney_intraday_history") as fetch_em:
            result = market_data.backfill_intraday_history(
                ["513180"],
                period="5",
                start_date=date(2026, 5, 18),
                end_date=date(2026, 5, 20),
                store=store,
            )

        fetch_futu.assert_called_once_with("513180", period="5", start_date=date(2026, 5, 18), end_date=date(2026, 5, 20))
        fetch_em.assert_not_called()
        self.assertTrue(result["success"])
        self.assertEqual(result["start_date"], "2026-05-18")
        self.assertEqual(result["end_date"], "2026-05-20")
        self.assertEqual(result["total_rows"], 2)
        loaded = store.load_intraday("513180", "5", date(2026, 5, 1), date(2026, 5, 31))
        self.assertEqual(len(loaded), 2)
        self.assertEqual({row.date() for row in loaded["date"]}, {date(2026, 5, 18), date(2026, 5, 20)})
        logs = store.recent_logs(limit=1)
        self.assertEqual(logs[0]["endpoint"], "backfill_range")
        self.assertEqual(logs[0]["days"], 3)

    def test_manual_date_backfill_saves_only_requested_date_from_futu(self):
        store = self.make_store()
        futu_df = pd.DataFrame(
            {
                "datetime": [pd.Timestamp("2026-05-19 14:45:00"), pd.Timestamp("2026-05-20 14:45:00")],
                "date": [pd.Timestamp("2026-05-19"), pd.Timestamp("2026-05-20")],
                "time": ["14:45", "14:45"],
                "open": [1.0, 1.1],
                "close": [1.05, 1.15],
                "high": [1.1, 1.2],
                "low": [0.9, 1.0],
                "volume": [100, 200],
                "amount": [1000, 2000],
            }
        )
        with patch.object(backfill_intraday_date, "fetch_futu_intraday_history", return_value=market_data.IntradayFetchResult(futu_df, "futu")) as fetch_futu:
            result = backfill_intraday_date.backfill_intraday_for_date(["513180"], target_date=date(2026, 5, 19), period="5", store=store)

        fetch_futu.assert_called_once_with("513180", period="5", start_date=date(2026, 5, 19), end_date=date(2026, 5, 19))
        self.assertTrue(result["success"])
        self.assertEqual(result["target_date"], "2026-05-19")
        self.assertEqual(result["total_rows"], 1)
        loaded = store.load_intraday("513180", "5", date(2026, 5, 19), date(2026, 5, 20))
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded.loc[0, "date"].date(), date(2026, 5, 19))
        logs = store.recent_logs(limit=1)
        self.assertEqual(logs[0]["endpoint"], "manual_backfill_date")

    def test_manual_date_cli_defaults_to_today_and_uses_combined_pool(self):
        selected = [{"code": "513180", "name": "恒生科技ETF"}, {"code": "159915", "name": "创业板ETF"}]
        with patch.object(backfill_intraday_date, "build_backfill_pool", return_value=selected) as build_pool, \
             patch.object(backfill_intraday_date, "backfill_intraday_for_date", return_value={"success": True}) as backfill, \
             patch.object(backfill_intraday_date, "date") as mock_date, \
             patch.object(sys, "argv", ["backfill_intraday_date.py"]):
            mock_date.today.return_value = date(2026, 5, 20)
            mock_date.fromisoformat.side_effect = date.fromisoformat
            exit_code = backfill_intraday_date.main()

        self.assertEqual(exit_code, 0)
        build_pool.assert_called_once()
        backfill.assert_called_once_with(["513180", "159915"], target_date=date(2026, 5, 20), period="5")


if __name__ == "__main__":
    unittest.main()
