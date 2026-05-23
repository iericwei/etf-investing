from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import error, request

from .config import BASE_DIR, CONFIG

SIGNAL_STATE_FILE = BASE_DIR / "data" / "holdings_signal_state.json"
NOTIFICATION_STATE_FILE = BASE_DIR / "data" / "notification_state.json"


def _post_json(url: str, payload: dict[str, Any], timeout: int = 8) -> bool:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return 200 <= int(getattr(resp, "status", 200)) < 300
    except (error.URLError, TimeoutError, OSError):
        return False


def feishu_webhook_url() -> str:
    return str(CONFIG.get("notifications", {}).get("feishu_webhook_url", "") or "").strip()


def send_feishu_text(text: str, webhook_url: str | None = None) -> bool:
    url = (webhook_url or feishu_webhook_url()).strip()
    if not url:
        return False
    body = text if text.startswith("QUANT") else f"QUANT {text}"
    return _post_json(url, {"msg_type": "text", "content": {"text": body}})


def load_json_state(path: Path, default: dict | None = None) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else (default or {})
    except Exception:
        return default or {}


def save_json_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def parse_hhmm(value: str) -> int:
    hour, minute = str(value).strip().split(":", 1)
    return int(hour) * 60 + int(minute)


def minute_to_hhmm(value: int) -> str:
    value = max(0, min(23 * 60 + 59, int(value)))
    return f"{value // 60:02d}:{value % 60:02d}"


def active_trade_windows(config: dict[str, Any] | None = None) -> list[str]:
    cfg = config or CONFIG
    models = cfg.get("models", {})
    strategy_name = models.get("active_portfolio_strategy")
    strategy = models.get("portfolio", {}).get(strategy_name, {}) if strategy_name else {}
    windows = strategy.get("trade_windows")
    if not windows:
        backtest_name = strategy.get("backtest_scheme") or models.get("active_backtest_scheme")
        backtest = models.get("backtest", {}).get(backtest_name, {}) if backtest_name else {}
        windows = backtest.get("trade_windows") or ([backtest.get("trade_time")] if backtest.get("trade_time") else [])
    unique: list[str] = []
    for window in windows or []:
        text = str(window).strip()
        if text and text not in unique:
            unique.append(text)
    return sorted(unique, key=parse_hhmm)


def due_strategy_windows(
    *,
    now: datetime,
    state: dict[str, Any],
    is_trading_day: bool,
    config: dict[str, Any] | None = None,
) -> list[str]:
    cfg = config or CONFIG
    notify_cfg = cfg.get("notifications", {})
    if not bool(notify_cfg.get("strategy_signal_enabled", True)) or not is_trading_day:
        return []
    lead = int(notify_cfg.get("strategy_signal_lead_minutes", 8))
    current = now.hour * 60 + now.minute
    today = now.strftime("%Y-%m-%d")
    done = state.get("strategy_signal_slots", {})
    due: list[str] = []
    for window in active_trade_windows(cfg):
        slot_key = f"{today} {window}"
        window_minute = parse_hhmm(window)
        if window_minute - lead <= current <= window_minute and not done.get(slot_key):
            due.append(window)
    return due


def mark_strategy_window_done(state: dict[str, Any], *, now: datetime, window: str, sent: bool, signal_count: int) -> dict:
    slots = state.setdefault("strategy_signal_slots", {})
    slots[f"{now.strftime('%Y-%m-%d')} {window}"] = {
        "sent": bool(sent),
        "signal_count": int(signal_count),
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return state


def _pct(value: Any) -> str:
    try:
        v = float(value)
    except Exception:
        return "—"
    return f"{'+' if v >= 0 else ''}{v:.2f}%"


def _num(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except Exception:
        return "—"


def _signal_reasons(row: dict[str, Any]) -> str:
    sig = row.get("trade_signal") or {}
    reasons = [s.get("name") for s in sig.get("buy_signals", []) if isinstance(s, dict) and s.get("name")]
    sell = sig.get("sell_signals") or row.get("sell_signals") or {}
    reasons += [s.get("name") for s in sell.get("signals", []) if isinstance(s, dict) and s.get("name")]
    return "；".join(reasons[:3]) or "按模型信号执行"


def _row_line(row: dict[str, Any], index: int, *, action: str) -> str:
    code = row.get("code", "")
    name = row.get("name") or code
    rank = row.get("rank")
    rank_text = f"#{rank}" if rank else "未入榜"
    score_text = _num(row.get("score"), 1)
    price_text = _num(row.get("price"), 3)
    ret5_text = _pct(row.get("ret5"))
    rsi_text = _num(row.get("rsi"), 1)
    if action == "sell":
        sell = row.get("sell_signals") or {}
        urgency = sell.get("urgency") or (row.get("trade_signal") or {}).get("label") or "卖出/减仓"
        return (
            f"{index}. {code} {name}｜{rank_text}｜现价 {price_text}｜5日 {ret5_text}｜RSI {rsi_text}｜"
            f"风险 {urgency}｜依据：{_signal_reasons(row)}"
        )
    return (
        f"{index}. {code} {name}｜{rank_text}｜评分 {score_text}｜现价 {price_text}｜5日 {ret5_text}｜"
        f"RSI {rsi_text}｜依据：{_signal_reasons(row)}"
    )


def format_strategy_signal_message(
    *,
    window: str,
    generated_at: datetime,
    buy_rows: list[dict[str, Any]],
    sell_rows: list[dict[str, Any]],
    holdings_count: int,
    config: dict[str, Any] | None = None,
) -> str:
    cfg = config or CONFIG
    lead = int(cfg.get("notifications", {}).get("strategy_signal_lead_minutes", 8))
    run_at = minute_to_hhmm(parse_hhmm(window) - lead)
    lines = [
        f"策略信号通知｜交易窗口 {window}",
        f"生成时间：{generated_at.strftime('%Y-%m-%d %H:%M:%S')}；计划提前运行：{run_at}；持仓数：{holdings_count}",
        "",
        "操作指引：",
        "- 买入：只从“买入候选”中选择，优先评分高、未持仓、成交额充足的标的；临近窗口确认价格未明显冲高后再下单。",
        "- 卖出：持仓出现在“卖出/减仓”时优先处理；强风险先减仓或清仓，中风险至少降低仓位并设置止损。",
        "- 无信号：不做追单，等待下一个策略窗口。",
    ]
    if buy_rows:
        lines += ["", "买入候选："]
        lines += [_row_line(row, i + 1, action="buy") for i, row in enumerate(buy_rows)]
    if sell_rows:
        lines += ["", "卖出/减仓："]
        lines += [_row_line(row, i + 1, action="sell") for i, row in enumerate(sell_rows)]
    if not buy_rows and not sell_rows:
        lines += ["", "本窗口没有触发买入或卖出信号。"]
    return "\n".join(lines)
