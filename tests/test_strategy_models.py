import sys
import unittest
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from etf_investing import strategy as etf_strategy
from etf_investing.config import CONFIG


def make_history(start=10.0, step=0.2, days=40):
    closes = [start + i * step for i in range(days)]
    return pd.DataFrame({
        "close": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "volume": [1000 + i * 10 for i in range(days)],
    })


class StrategyModelConfigTests(unittest.TestCase):
    def test_default_config_declares_active_selection_model(self):
        self.assertIn("models", CONFIG)
        self.assertEqual(CONFIG["models"]["active_selection_model"], "multi_factor_v1")
        self.assertIn("multi_factor_v1", CONFIG["models"]["selection"])

    def test_score_all_uses_configured_factor_weights(self):
        etf_map = {
            "AAA": etf_strategy.compute_indicators(make_history(start=10, step=0.4)),
            "BBB": etf_strategy.compute_indicators(make_history(start=30, step=-0.1)),
        }
        model_cfg = etf_strategy.get_selection_model_config("multi_factor_v1")
        trend_only = etf_strategy.merge_model_config(model_cfg, {
            "factor_weights": {
                "momentum": 0,
                "volume": 0,
                "technical": 0,
                "trend": 1,
            }
        })
        scored = etf_strategy.get_selection_model("multi_factor_v1", trend_only).score_all(etf_map)
        for _, row in scored.iterrows():
            self.assertAlmostEqual(float(row["score"]), float(row["trend_score"]), places=1)

    def test_custom_model_can_be_registered_and_used_by_select_top(self):
        class ReverseCodeModel(etf_strategy.SelectionModel):
            name = "reverse_code_test"

            def score_all(self, etf_map):
                rows = []
                for idx, code in enumerate(sorted(etf_map.keys(), reverse=True), start=1):
                    rows.append({
                        "code": code,
                        "close": float(etf_map[code].iloc[-1]["close"]),
                        "ret3": 0.0,
                        "ret5": 0.0,
                        "ret10": 0.0,
                        "rsi": 50.0,
                        "macd_hist": 1.0,
                        "vol_ratio": 1.0,
                        "above_ma5": 1,
                        "above_ma10": 1,
                        "above_ma20": 1,
                        "ma_aligned": 1,
                        "score": 100 - idx,
                        "momentum_score": 0.0,
                        "volume_score": 0.0,
                        "technical_score": 0.0,
                        "trend_score": 0.0,
                    })
                return pd.DataFrame(rows)

        etf_strategy.register_selection_model(ReverseCodeModel)
        pool = [
            {"code": "AAA", "name": "A", "category": "T"},
            {"code": "ZZZ", "name": "Z", "category": "T"},
        ]
        etf_map = {item["code"]: make_history() for item in pool}
        results = etf_strategy.select_top(pool, etf_map, {}, top_n=1, model_name="reverse_code_test")
        self.assertEqual(results[0]["code"], "ZZZ")


if __name__ == "__main__":
    unittest.main()
