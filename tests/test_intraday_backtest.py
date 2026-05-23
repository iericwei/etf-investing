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
from etf_investing import server as etf_server
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
    def test_fetch_eastmoney_intraday_history_normalizes_kline_payload(self):
        response = Mock()
        response.json.return_value = {
            "data": {
                "klines": [
                    "2026-05-18 14:30,1.000,1.050,1.060,0.990,100,1000.5",
                    "2026-05-18 14:45,1.100,1.150,1.160,1.090,200,2300.5",
                ]
            }
        }
        response.raise_for_status.return_value = None
        profile = {"User-Agent": "Mozilla/5.0 TestChrome/148", "Accept": "text/html"}

        with patch.object(etf_data, "_eastmoney_intraday_header_candidates", return_value=[profile]), \
             patch.object(etf_data, "_log_eastmoney_intraday_curl") as log_curl, \
             patch.object(etf_data, "_eastmoney_direct_get", return_value=response) as get:
            df = etf_data.fetch_eastmoney_intraday_history("513180", period="15", days=3)

        get.assert_called_once()
        url = get.call_args.args[0]
        params = get.call_args.args[1]
        headers = get.call_args.args[2]
        self.assertIn("push2his.eastmoney.com", url)
        self.assertEqual(params["secid"], "1.513180")
        self.assertEqual(params["klt"], "15")
        self.assertEqual(params["fqt"], "1")
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertIn("Accept", headers)
        self.assertGreaterEqual(log_curl.call_count, 2)
        self.assertEqual(list(df.columns), ["datetime", "date", "time", "open", "close", "high", "low", "volume", "amount"])
        self.assertEqual(str(df.loc[1, "date"].date()), "2026-05-18")
        self.assertEqual(df.loc[1, "time"], "14:45")
        self.assertEqual(df.loc[1, "close"], 1.15)
        self.assertEqual(df.loc[1, "amount"], 2300.5)

    def test_eastmoney_intraday_headers_pick_random_full_browser_profile(self):
        profile = {
            "User-Agent": "Mozilla/5.0 TestChrome/148",
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Sec-Fetch-Dest": "document",
            "sec-ch-ua": '"Chromium";v="148"',
            "Cookie": "qgqp_b_id=test; st_si=123",
        }
        with patch.object(etf_data, "_load_eastmoney_header_profiles", return_value=[profile]) as load, \
             patch.object(etf_data.random, "choice", return_value=profile) as choice:
            headers = etf_data._eastmoney_intraday_headers()

        load.assert_called_once()
        choice.assert_called_once_with([profile])
        self.assertEqual(headers["User-Agent"], profile["User-Agent"])
        self.assertEqual(headers["Referer"], "https://fund.eastmoney.com/")
        self.assertIn("text/html", headers["Accept"])
        self.assertIn("zh-CN", headers["Accept-Language"])
        self.assertEqual(headers["Sec-Fetch-Dest"], "document")
        self.assertIn("qgqp_b_id", headers["Cookie"])

    def test_parse_curl_headers_file_loads_headers_and_cookie(self):
        header_file = ROOT / "src" / "etf_investing" / "headers.txt"
        headers = etf_data._parse_curl_headers_file(header_file)

        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertEqual(headers["Sec-Fetch-Mode"], "navigate")
        self.assertIn("sec-ch-ua", headers)
        self.assertIn("qgqp_b_id", headers["Cookie"])

    def test_build_curl_command_includes_no_proxy_url_and_headers(self):
        curl = etf_data._build_curl_command(
            "GET",
            "https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.513180",
            {"User-Agent": "Mozilla/5.0 Test", "Cookie": "qgqp_b_id=test; st_si=123"},
        )

        self.assertIn("--noproxy '*'", curl)
        self.assertIn("push2his.eastmoney.com", curl)
        self.assertIn("User-Agent: Mozilla/5.0 Test", curl)
        self.assertIn("Cookie: qgqp_b_id=test; st_si=123", curl)

    def test_eastmoney_direct_get_disables_environment_proxy(self):
        fake_response = Mock()
        fake_session = Mock()
        fake_session.get.return_value = fake_response

        with patch.object(etf_data.requests, "Session", return_value=fake_session):
            response = etf_data._eastmoney_direct_get(
                "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                {"secid": "1.513180"},
                {"User-Agent": "Mozilla/5.0 Test"},
                8,
            )

        self.assertIs(response, fake_response)
        self.assertFalse(fake_session.trust_env)
        fake_session.get.assert_called_once()
        fake_session.close.assert_called_once()

    def test_fetch_etf_15m_history_uses_eastmoney_without_akshare(self):
        expected = pd.DataFrame({"close": [1.23]})
        with patch.object(etf_data, "fetch_eastmoney_intraday_history", return_value=expected) as fetch_em:
            df = etf_data.fetch_etf_15m_history("513180", days=3)

        fetch_em.assert_called_once_with("513180", period="15", days=3)
        self.assertIs(df, expected)

    def test_quote_service_intraday_futu_endpoint_returns_today_rows(self):
        intraday = pd.DataFrame({
            "datetime": [pd.Timestamp("2026-05-19 14:45:00")],
            "date": [pd.Timestamp("2026-05-19")],
            "time": ["14:45"],
            "open": [1.0],
            "close": [1.1],
            "high": [1.1],
            "low": [1.0],
            "volume": [100],
            "amount": [110],
        })
        result = etf_server.IntradayFetchResult(intraday, "futu")
        with patch.object(etf_server, "fetch_futu_today_intraday_history", return_value=result) as fetch_futu:
            res = etf_server.app.test_client().get("/intraday/futu?code=513180&period=15")

        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        fetch_futu.assert_called_once_with("513180", period="15")
        self.assertTrue(payload["success"])
        self.assertEqual(payload["source"], "futu")
        self.assertEqual(payload["days"], 1)
        self.assertEqual(payload["data"][0]["time"], "14:45")

    def test_quote_service_intraday_endpoint_can_force_futu_source(self):
        intraday = pd.DataFrame({
            "datetime": [pd.Timestamp("2026-05-19 14:45:00")],
            "date": [pd.Timestamp("2026-05-19")],
            "time": ["14:45"],
            "open": [1.0],
            "close": [1.1],
            "high": [1.1],
            "low": [1.0],
            "volume": [100],
            "amount": [110],
        })
        result = etf_server.IntradayFetchResult(intraday, "futu")
        with patch.object(etf_server, "fetch_futu_today_intraday_history", return_value=result) as fetch_futu:
            res = etf_server.app.test_client().get("/intraday?code=513180&period=15&source=futu")

        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        fetch_futu.assert_called_once_with("513180", period="15")
        self.assertTrue(payload["success"])
        self.assertEqual(payload["source"], "futu")
        self.assertEqual(payload["days"], 1)

    def test_quote_service_intraday_endpoint_uses_eastmoney_wrapper(self):
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
        with patch.object(etf_server, "fetch_eastmoney_intraday_history", return_value=intraday) as fetch_em:
            res = etf_server.app.test_client().get("/intraday?code=513180&period=15&days=3")

        self.assertEqual(res.status_code, 200)
        payload = res.get_json()
        fetch_em.assert_called_once_with("513180", period="15", days=3)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["source"], "eastmoney")
        self.assertEqual(payload["data"][0]["time"], "14:45")
        self.assertEqual(payload["data"][0]["close"], 1.1)

    def test_fetch_etf_15m_history_akshare_normalizes_columns(self):
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
            df = etf_data.fetch_etf_15m_history_akshare("513180", days=3)

        fake_ak.fund_etf_hist_min_em.assert_called_once()
        kwargs = fake_ak.fund_etf_hist_min_em.call_args.kwargs
        self.assertEqual(kwargs["symbol"], "513180")
        self.assertEqual(kwargs["period"], "15")
        self.assertEqual(kwargs["adjust"], "")
        self.assertEqual(list(df.columns), ["datetime", "date", "time", "open", "close", "high", "low", "volume", "amount"])
        self.assertEqual(str(df.loc[1, "date"].date()), "2026-05-18")
        self.assertEqual(df.loc[1, "time"], "14:45")
        self.assertEqual(df.loc[1, "close"], 1.15)

    def test_backtest_model_uses_passed_intraday_as_local_price_for_trade_points(self):
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

        bt = etf_strategy.backtest_model(daily, window=22, intraday=intraday, scheme_name="before_close_15m")

        first_trade = bt["trade_points"][0]
        self.assertEqual(first_trade["time"], "14:45")
        self.assertEqual(first_trade["price"], 123.45)
        self.assertEqual(first_trade["price_source"], "local")
        self.assertEqual(first_trade["price_source_label"], "local")
        self.assertEqual(bt["execution_price"], "local")
        self.assertIn("本地行情库", bt["price_note"])

    def test_backtest_model_describes_daily_kline_price_when_intraday_missing(self):
        closes = [10 + i * 0.08 for i in range(45)]
        daily = make_history(closes)

        bt = etf_strategy.backtest_model(daily, window=22, intraday=pd.DataFrame(), scheme_name="before_close_15m")

        first_trade = bt["trade_points"][0]
        self.assertEqual(first_trade["price_source"], "日k")
        self.assertEqual(first_trade["price_source_label"], "日k")

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

            fetch_15m.assert_called_once_with("513180", days=88)
            self.assertIs(run_model.call_args.kwargs["intraday"], intraday)
            self.assertEqual(run_model.call_args.kwargs["code"], "513180")
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
