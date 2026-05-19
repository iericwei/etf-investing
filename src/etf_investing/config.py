"""
集中配置加载模块。

默认配置写在 DEFAULT_CONFIG，项目根目录 config.json 可覆盖默认值。
只依赖 Python 标准库，避免为配置引入额外依赖。
"""

from __future__ import annotations

import copy
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

PACKAGE_DIR = Path(__file__).resolve().parent
BASE_DIR = PACKAGE_DIR.parents[1]
CONFIG_FILE = BASE_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "urls": {
        "eastmoney_universe": (
            "https://push2.eastmoney.com/api/qt/clist/get"
            "?pn=1&pz=5000&po=1&np=1"
            "&ut=bd1d9ddb04089700cf9c27f6f7426281"
            "&fltt=2&invt=2&fid=f6"
            "&fs=b:MK0021,b:MK0022,b:MK0023,b:MK0024"
            "&fields=f12,f13,f14,f2,f3,f6"
        ),
        "tencent_history": (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            "?_var=kline_dayhfq&param={prefix}{code},day,,,{days},qfq"
        ),
        "tencent_realtime": "https://qt.gtimg.cn/q={codes}",
    },
    "headers": {
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ),
        "tencent_referer": "https://gu.qq.com/",
        "eastmoney_referer": "https://fund.eastmoney.com/",
    },
    "network": {
        "timeouts": {
            "eastmoney_universe": 15,
            "tencent_history": 10,
            "tencent_realtime": 8,
        }
    },
    "selection": {
        "default_min_amount": 5e7,
        "default_max_count": 300,
        "default_top_n": 10,
        "web_top_n": 50,
        "history_days": 65,
        "history_workers": 10,
        "realtime_batch_size": 80,
        "min_history_rows": 30,
        "holding_history_workers": 5,
        "holding_min_history_rows": 10,
    },

    "models": {
        "active_selection_model": "multi_factor_v1",
        "active_backtest_scheme": "before_close_15m",
        "selection": {
            "multi_factor_v1": {
                "display_name": "多因子评分模型 v1",
                "factor_weights": {
                    "momentum": 0.35,
                    "volume": 0.25,
                    "technical": 0.25,
                    "trend": 0.15
                },
                "momentum": {
                    "ret3_weight": 0.55,
                    "ret5_weight": 0.45
                },
                "volume": {
                    "ret_window": "ret3",
                    "ret_clip_min": -4,
                    "ret_clip_max": 15,
                    "ret_offset": 5
                },
                "technical": {
                    "rsi_target": 55,
                    "rsi_penalty_per_point": 2,
                    "rsi_weight": 0.35,
                    "macd_weight": 0.35,
                    "ma_weight": 0.30,
                    "ma5_score": 33,
                    "ma10_score": 33,
                    "ma_aligned_score": 34
                },
                "filters": {
                    "max_rsi": 82,
                    "min_ret5": -9,
                    "ma20_down_ret3": -3,
                    "ma20_down_ret5": -5
                }
            }
        },
        "backtest": {
            "before_close_15m": {
                "display_name": "收盘前15分钟",
                "window_days": 22,
                "trade_time": "14:45",
                "trade_timing_label": "收盘前15分钟",
                "execution_price": "close",
            }
        }
    },
    "server": {
        "host": "0.0.0.0",
        "quote_port": 5678,
        "web_port": 8080,
        "quote_cache_ttl_seconds": 5,
        "debug": False,
    },
    "time": {
        "date_format": "%Y-%m-%d",
        "timestamp_format": "%Y-%m-%d %H:%M:%S",
        "report_time_format": "%Y-%m-%d  %H:%M",
        "quote_updated_format": "%H:%M:%S",
        "quote_updated_compact_format": "%H%M%S",
    },
    "web": {
        "initial_load_hint_seconds": [60, 90],
        "selection_poll_interval_ms": 2500,
        "holdings_refresh_seconds": 30,
        "holdings_countdown_interval_ms": 1000,
        "holdings_market_check_interval_ms": 60 * 1000,
        "auto_refresh_interval_ms": 10 * 60 * 1000,
        "auto_refresh_start_minute": 9 * 60 + 25,
        "auto_refresh_end_minute": 15 * 60 + 5,
    },
}


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_config() -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        if not isinstance(user_cfg, dict):
            raise ValueError(f"{CONFIG_FILE} 顶层必须是 JSON object")
        _deep_merge(cfg, user_cfg)
    return cfg


CONFIG = load_config()


def get_config(path: str, default: Any = None) -> Any:
    cur: Any = CONFIG
    for part in path.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return default
        cur = cur[part]
    return cur


def request_headers(source: str) -> dict[str, str]:
    referer_key = "eastmoney_referer" if source == "eastmoney" else "tencent_referer"
    return {
        "Referer": CONFIG["headers"][referer_key],
        "User-Agent": CONFIG["headers"]["user_agent"],
    }


def tencent_history_url(prefix: str, code: str, days: int) -> str:
    return CONFIG["urls"]["tencent_history"].format(prefix=prefix, code=code, days=days)


def tencent_realtime_url(qq_codes: list[str]) -> str:
    return CONFIG["urls"]["tencent_realtime"].format(codes=",".join(qq_codes))


def today_str() -> str:
    return date.today().strftime(CONFIG["time"]["date_format"])


def now_str(format_key: str = "timestamp_format") -> str:
    return datetime.now().strftime(CONFIG["time"][format_key])


def app_base_url(port_key: str) -> str:
    return f"http://localhost:{CONFIG['server'][port_key]}"


def web_runtime_config() -> dict[str, Any]:
    """暴露给前端 JS 的非敏感运行时配置。"""
    return {
        "selectionPollIntervalMs": CONFIG["web"]["selection_poll_interval_ms"],
        "holdingsRefreshSeconds": CONFIG["web"]["holdings_refresh_seconds"],
        "holdingsCountdownIntervalMs": CONFIG["web"]["holdings_countdown_interval_ms"],
        "holdingsMarketCheckIntervalMs": CONFIG["web"]["holdings_market_check_interval_ms"],
        "autoRefreshIntervalMs": CONFIG["web"]["auto_refresh_interval_ms"],
        "autoRefreshStartMinute": CONFIG["web"]["auto_refresh_start_minute"],
        "autoRefreshEndMinute": CONFIG["web"]["auto_refresh_end_minute"],
    }
