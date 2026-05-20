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

    def test_watch_reminder_sends_once_per_trading_day_at_before_close_time(self):
        sent = []
        now = datetime(2026, 5, 20, 14, 45)
        with patch.object(notifications, "send_feishu_text", side_effect=lambda text, webhook_url=None: sent.append(text) or True), \
             patch.object(notifications, "_today_key", return_value="2026-05-20"):
            first = notifications.maybe_send_watch_reminder(now=now, is_trading_day=True, state={})
            second = notifications.maybe_send_watch_reminder(now=now, is_trading_day=True, state={"last_watch_reminder_date": "2026-05-20"})

        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0].startswith("看盘提醒"))


if __name__ == "__main__":
    unittest.main()
