import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from etf_investing import config as etf_config
from etf_investing import notifications


class NotificationConfigTests(unittest.TestCase):
    def test_load_config_merges_private_local_config_after_public_config(self):
        with tempfile.TemporaryDirectory() as td:
            public = Path(td) / "config.json"
            private = Path(td) / "config.local.json"
            public.write_text(json.dumps({"futu": {"host": "public", "port": 11111}}), encoding="utf-8")
            private.write_text(json.dumps({"futu": {"host": "private", "port": 22222}, "notifications": {"feishu_webhook_url": "https://example.test/hook"}}), encoding="utf-8")
            with patch.object(etf_config, "CONFIG_FILE", public), patch.object(etf_config, "LOCAL_CONFIG_FILE", private):
                cfg = etf_config.load_config()

        self.assertEqual(cfg["futu"], {"host": "private", "port": 22222})
        self.assertEqual(cfg["notifications"]["feishu_webhook_url"], "https://example.test/hook")

    def test_send_feishu_text_posts_quant_prefixed_message(self):
        calls = []
        with patch.object(notifications, "_post_json", side_effect=lambda url, payload, timeout=8: calls.append((url, payload)) or True):
            ok = notifications.send_feishu_text("信号变动", webhook_url="https://example.test/hook")

        self.assertTrue(ok)
        self.assertEqual(calls[0][0], "https://example.test/hook")
        self.assertEqual(calls[0][1]["msg_type"], "text")
        self.assertTrue(calls[0][1]["content"]["text"].startswith("QUANT"))

    def test_active_trade_windows_use_default_portfolio_strategy(self):
        windows = notifications.active_trade_windows({
            "models": {
                "active_portfolio_strategy": "demo",
                "active_backtest_scheme": "fallback",
                "portfolio": {"demo": {"trade_windows": ["14:45", "09:35"]}},
                "backtest": {"fallback": {"trade_time": "14:50"}},
            }
        })

        self.assertEqual(windows, ["09:35", "14:45"])

    def test_due_strategy_windows_fire_once_inside_lead_window(self):
        cfg = {
            "notifications": {"strategy_signal_enabled": True, "strategy_signal_lead_minutes": 8},
            "models": {
                "active_portfolio_strategy": "demo",
                "portfolio": {"demo": {"trade_windows": ["09:35", "14:45"]}},
                "backtest": {},
            },
        }

        due = notifications.due_strategy_windows(
            now=datetime(2026, 5, 20, 9, 30),
            state={},
            is_trading_day=True,
            config=cfg,
        )
        skipped = notifications.due_strategy_windows(
            now=datetime(2026, 5, 20, 9, 30),
            state={"strategy_signal_slots": {"2026-05-20 09:35": {"sent": True}}},
            is_trading_day=True,
            config=cfg,
        )

        self.assertEqual(due, ["09:35"])
        self.assertEqual(skipped, [])

    def test_format_strategy_signal_message_contains_action_guidance(self):
        text = notifications.format_strategy_signal_message(
            window="14:45",
            generated_at=datetime(2026, 5, 20, 14, 37),
            holdings_count=2,
            buy_rows=[{
                "code": "111111",
                "name": "测试ETF",
                "rank": 1,
                "score": 88,
                "price": 1.234,
                "ret5": 3.21,
                "rsi": 61.2,
                "trade_signal": {"buy_signals": [{"name": "均线多头"}]},
            }],
            sell_rows=[{
                "code": "222222",
                "name": "风险ETF",
                "rank": 2,
                "price": 2.345,
                "ret5": -4.56,
                "rsi": 78.5,
                "sell_signals": {"urgency": "高风险", "signals": [{"name": "跌破均线"}]},
            }],
            config={"notifications": {"strategy_signal_lead_minutes": 8}},
        )

        self.assertIn("交易窗口 14:45", text)
        self.assertIn("操作指引", text)
        self.assertIn("买入候选", text)
        self.assertIn("卖出/减仓", text)
        self.assertIn("111111 测试ETF", text)
        self.assertIn("222222 风险ETF", text)

    def test_format_auto_refresh_failure_message_contains_action_guidance(self):
        text = notifications.format_auto_refresh_failure_message(
            trigger="web_auto",
            error="行情源超时",
            failed_at=datetime(2026, 5, 25, 10, 15, 0),
        )

        self.assertIn("自动刷新行情失败", text)
        self.assertIn("页面自动刷新", text)
        self.assertIn("行情源超时", text)
        self.assertIn("处理建议", text)


if __name__ == "__main__":
    unittest.main()
