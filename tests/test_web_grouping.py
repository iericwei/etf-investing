import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd

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
                 patch.object(web_app, "_load_watchlist", return_value=[]), \
                 patch.object(web_app, "_refresh_cached_rows_for_codes", return_value={}), \
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

    def test_holdings_realtime_uses_same_trade_signal_as_cached_list_row(self):
        cached_signal = {"action": "buy", "label": "买入/持有"}
        with web_app._lock:
            old_cache = dict(web_app._cache)
            web_app._cache.update(
                results=[{"code": "111111", "rank": 1, "trade_signal": cached_signal}],
                etf_map={},
            )
        try:
            with patch.object(web_app, "_load_holdings", return_value=["111111"]), \
                 patch.object(web_app, "_load_watchlist", return_value=[]), \
                 patch.object(web_app, "_refresh_cached_rows_for_codes", return_value={"111111": {"code": "111111", "rank": 1, "trade_signal": cached_signal}}), \
                 patch.object(web_app, "fetch_realtime", return_value={"111111": {"name": "测试ETF", "price": 1.23, "change_pct": 0.5, "amount": 1000}}), \
                 patch.object(web_app, "_universe_meta", return_value={"111111": {"category": "科技"}}):
                res = web_app.app.test_client().get("/api/holdings/realtime")

            self.assertEqual(res.status_code, 200)
            payload = res.get_json()
            self.assertEqual(payload["data"][0]["trade_signal"], cached_signal)
        finally:
            with web_app._lock:
                web_app._cache.clear()
                web_app._cache.update(old_cache)

    def test_holdings_realtime_refreshes_holdings_and_watchlist_rows_in_cache(self):
        old_df = pd.DataFrame({
            "date": pd.date_range("2026-01-01", periods=45, freq="B"),
            "open": [1.0] * 45,
            "close": [1.0] * 45,
            "high": [1.1] * 45,
            "low": [0.9] * 45,
            "volume": [1000] * 45,
        })
        with web_app._lock:
            old_cache = dict(web_app._cache)
            web_app._cache.update(
                status="ready",
                results=[
                    {"code": "111111", "rank": 1, "name": "持仓ETF", "category": "持仓", "trade_signal": {"action": "sell"}},
                    {"code": "222222", "rank": 2, "name": "自选ETF", "category": "自选", "is_custom": True, "trade_signal": {"action": "sell"}},
                ],
                etf_map={"111111": old_df, "222222": old_df},
            )
        try:
            with patch.object(web_app, "_load_holdings", return_value=["111111"]), \
                 patch.object(web_app, "_load_watchlist", return_value=["222222"]), \
                 patch.object(web_app, "fetch_realtime", return_value={
                     "111111": {"name": "持仓ETF", "price": 2.0, "change_pct": 1.0, "amount": 1000},
                     "222222": {"name": "自选ETF", "price": 2.0, "change_pct": 1.0, "amount": 1000},
                 }), \
                 patch.object(web_app, "_universe_meta", return_value={"111111": {"category": "持仓"}, "222222": {"category": "自选"}}):
                res = web_app.app.test_client().get("/api/holdings/realtime")

            self.assertEqual(res.status_code, 200)
            with web_app._lock:
                rows = {r.get("code"): r for r in web_app._cache["results"] if not r.get("_is_group_header")}
            self.assertEqual(rows["111111"]["price"], 2.0)
            self.assertEqual(rows["222222"]["price"], 2.0)
        finally:
            with web_app._lock:
                web_app._cache.clear()
                web_app._cache.update(old_cache)

    def test_market_status_pauses_after_close_and_on_holidays(self):
        with patch.object(web_app, "_is_china_trading_day", return_value=True):
            after_close = web_app._market_status(datetime(2026, 5, 19, 15, 30))
            intraday = web_app._market_status(datetime(2026, 5, 19, 10, 0))
        with patch.object(web_app, "_is_china_trading_day", return_value=False):
            holiday = web_app._market_status(datetime(2026, 5, 20, 10, 0))

        self.assertFalse(after_close["auto_refresh_allowed"])
        self.assertTrue(after_close["after_close"])
        self.assertEqual(after_close["reason"], "已收盘")
        self.assertTrue(intraday["auto_refresh_allowed"])
        self.assertFalse(holiday["auto_refresh_allowed"])
        self.assertEqual(holiday["reason"], "节假日/非交易日")

    def test_market_indices_endpoint_returns_fixed_four_indices(self):
        def quote_line(prefix, code, name, price, prev, change, change_pct):
            fields = [""] * 40
            fields[1] = name
            fields[2] = code
            fields[3] = str(price)
            fields[4] = str(prev)
            fields[31] = str(change)
            fields[32] = str(change_pct)
            return f'v_{prefix}{code}="' + "~".join(fields) + '";'

        class Response:
            encoding = "gbk"
            text = "\n".join([
                quote_line("sh", "000001", "上证指数", 3120.42, 3108.60, 11.82, 0.38),
                quote_line("sz", "399001", "深证成指", 9820.30, 9769.50, 50.80, 0.52),
                quote_line("sz", "399006", "创业板指", 1910.10, 1914.12, -4.02, -0.21),
                quote_line("sh", "000688", "科创板指数", 980.50, 976.21, 4.29, 0.44),
            ])

        with patch.object(web_app.requests, "get", return_value=Response()):
            res = web_app.app.test_client().get("/api/market/indices")

        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual([row["code"] for row in payload["data"]], ["000001", "399001", "399006", "000688"])
        self.assertEqual(payload["data"][0]["short_name"], "上证")
        self.assertEqual(payload["data"][2]["change_pct"], -0.21)

    def test_holdings_realtime_marks_signal_changes_and_notifies_feishu(self):
        cached_signal = {"action": "sell", "label": "卖出"}
        old_state = {
            "111111": {
                "trade_signal_label": "观望",
                "sell_signal_label": "低风险",
            }
        }
        with web_app._lock:
            old_cache = dict(web_app._cache)
            web_app._cache.update(
                results=[{"code": "111111", "rank": 1, "trade_signal": cached_signal}],
                etf_map={"111111": pd.DataFrame({"close": [1, 2, 3]})},
            )
        try:
            with patch.object(web_app, "_load_holdings", return_value=["111111"]), \
                 patch.object(web_app, "_load_watchlist", return_value=[]), \
                 patch.object(web_app, "_refresh_cached_rows_for_codes", return_value={"111111": {"code": "111111", "rank": 1, "trade_signal": cached_signal}}), \
                 patch.object(web_app, "fetch_realtime", return_value={"111111": {"name": "测试ETF", "price": 1.23, "change_pct": 0.5, "amount": 1000}}), \
                 patch.object(web_app, "_universe_meta", return_value={"111111": {"category": "科技"}}), \
                 patch.object(web_app, "compute_sell_signals", return_value={"signals": [{"name": "跌破均线", "level": 3}], "urgency": "高风险", "urgency_level": 3}), \
                 patch.object(web_app, "_load_signal_state", return_value=old_state), \
                 patch.object(web_app, "_save_signal_state") as save_state, \
                 patch.object(web_app, "send_feishu_text", return_value=True) as notify:
                res = web_app.app.test_client().get("/api/holdings/realtime")

            self.assertEqual(res.status_code, 200)
            row = res.get_json()["data"][0]
            self.assertEqual(row["signal_changes"][0], {"field": "模型信号", "from": "观望", "to": "卖出"})
            self.assertEqual(row["signal_changes"][1], {"field": "卖出信号", "from": "低风险", "to": "高风险"})
            notify.assert_called_once()
            self.assertIn("模型信号：由「观望」变为「卖出」", notify.call_args.args[0])
            self.assertIn("卖出信号：由「低风险」变为「高风险」", notify.call_args.args[0])
            saved = save_state.call_args.args[0]
            self.assertEqual(saved["111111"]["trade_signal_label"], "卖出")
            self.assertEqual(saved["111111"]["sell_signal_label"], "高风险")
        finally:
            with web_app._lock:
                web_app._cache.clear()
                web_app._cache.update(old_cache)

    def test_holding_signal_change_ignores_sell_reason_detail_changes(self):
        rows = [{
            "code": "111111",
            "trade_signal": {"action": "hold", "label": "观望"},
            "sell_signals": {"urgency": "高风险", "signals": [{"name": "新原因"}]},
        }]
        old_state = {
            "111111": {
                "trade_signal_label": "观望",
                "sell_signal_label": "高风险（旧原因）",
            }
        }

        annotated, next_state, changed_rows = web_app._annotate_holding_signal_changes(rows, old_state)

        self.assertEqual(annotated[0]["signal_changes"], [])
        self.assertEqual(changed_rows, [])
        self.assertEqual(next_state["111111"]["sell_signal_label"], "高风险")


if __name__ == "__main__":
    unittest.main()
