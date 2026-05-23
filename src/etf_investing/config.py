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
LOCAL_CONFIG_FILE = BASE_DIR / "config.local.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "urls": {
        "eastmoney_universe": (
            "https://push2.eastmoney.com/api/qt/clist/get"
            "?pn=1&pz=5000&po=1&np=1"
            "&ut=bd1d9ddb04089700cf9c27f6f7426281"
            "&fltt=2&invt=2&fid=f6"
            "&fs=b:MK0021,b:MK0022,b:MK0023,b:MK0024"
            "&fields=f12,f13,f14,f2,f3,f6,f20"
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
            "eastmoney_intraday": 8,
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
        "active_backtest_scheme": "eric_c3_four_window",
        "active_portfolio_strategy": "eric_c3_rotation",
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
                "signal_model": "legacy_single_symbol",
            },
            "eric_c3_four_window": {
                "display_name": "Eric C3 四窗口回测",
                "window_days": 44,
                "trade_time": "14:45",
                "trade_windows": ["09:35", "11:30", "13:05", "14:45"],
                "trade_timing_label": "四窗口",
                "execution_price": "intraday_5m_close",
                "signal_model": "eric_c3_rotation",
                "selection_score_fallback": 100,
            }
        },
        "portfolio": {
            "legacy_single_symbol": {
                "display_name": "现有单标的信号",
                "strategy_type": "single_symbol",
                "selection_model": "multi_factor_v1",
                "backtest_scheme": "before_close_15m",
            },
            "eric_c3_rotation": {
                "display_name": "Eric C3 Rotation（艾瑞克C3 四窗口轮动）",
                "strategy_type": "portfolio_rotation",
                "selection_model": "multi_factor_v1",
                "backtest_scheme": "eric_c3_four_window",
                "trade_windows": ["09:35", "11:30", "13:05", "14:45"],
                "max_positions": 5,
                "target_weight": 0.2,
                "max_daily_actions": 3,
                "max_daily_sells": 2,
                "max_daily_buys": 2,
                "one_action_per_symbol_per_day": True,
                "entry": {
                    "min_selection_score": 72,
                    "min_buy_score": 6,
                    "max_sell_level": 1,
                    "require_price_above_ma10": True,
                    "require_ma5_gt_ma10_gt_ma20": True,
                    "min_ma20_slope5_pct": 0,
                    "min_ret10_pct": 3.5,
                    "max_ret10_pct": 24,
                    "min_ret20_pct": 5,
                    "min_rsi": 45,
                    "max_rsi": 76,
                    "min_vol_ratio": 0.9,
                    "max_vol_ratio": 4,
                    "max_annualized_volatility_pct": 80,
                },
                "exit": {
                    "hard_stop_loss_pct": -5.5,
                    "trailing_stop_pct": 8.5,
                    "profit_protect_min_profit_pct": 10,
                    "profit_protect_drawdown_pct": 4,
                    "strong_sell_level": 3,
                    "soft_sell_level": 2,
                    "soft_confirmation_days": 2,
                    "soft_grace_days": 3,
                    "soft_min_buy_score": 4,
                    "soft_ret5_floor_pct": -3,
                    "time_stop_days": 20,
                    "time_stop_min_return_pct": 2,
                },
                "monthly_guard": {
                    "profit_lock_return_pct": 10,
                    "profit_lock_score_add": 7,
                    "drawdown_stop_return_pct": -6,
                },
            },
        }
    },
    "server": {
        "host": "0.0.0.0",
        "quote_port": 5678,
        "market_data_port": 5680,
        "web_port": 8080,
        "quote_cache_ttl_seconds": 5,
        "debug": False,
    },
    "notifications": {
        "feishu_webhook_url": "",
        "strategy_signal_enabled": True,
        "strategy_signal_lead_minutes": 8,
        "strategy_signal_max_rows": 8,
        "strategy_signal_sell_urgency_min_level": 2,
        "strategy_signal_notify_no_signal": False,
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
    for path in (CONFIG_FILE, LOCAL_CONFIG_FILE):
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        if not isinstance(user_cfg, dict):
            raise ValueError(f"{path} 顶层必须是 JSON object")
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
