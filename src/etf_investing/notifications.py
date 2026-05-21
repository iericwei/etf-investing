from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib import request, error

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


def _signal_change_text(change: dict[str, Any]) -> str:
    field = str(change.get("field") or "信号").strip()
    old = str(change.get("from") or "未知").strip() or "未知"
    new = str(change.get("to") or "未知").strip() or "未知"
    return f"- {field}：由「{old}」变为「{new}」"


def format_holding_change_message(rows: list[dict[str, Any]]) -> str:
    lines = ["持仓信号变动"]
    for row in rows:
        code = row.get("code")
        name = row.get("name") or code
        lines.append(f"{code} {name}")
        for change in row.get("signal_changes", []):
            if isinstance(change, dict):
                lines.append(_signal_change_text(change))
    return "\n".join(lines)


def load_json_state(path: Path, default: dict | None = None) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else (default or {})
    except Exception:
        return default or {}


def save_json_state(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _today_key(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def maybe_send_watch_reminder(*, now: datetime | None = None, is_trading_day: bool = True, state: dict | None = None) -> bool:
    cfg = CONFIG.get("notifications", {})
    if not bool(cfg.get("watch_reminder_enabled", True)) or not is_trading_day:
        return False
    now = now or datetime.now()
    reminder_minute = int(cfg.get("watch_reminder_minute", 14 * 60 + 45))
    if now.hour * 60 + now.minute < reminder_minute:
        return False
    state_file_backed = state is None
    state = load_json_state(NOTIFICATION_STATE_FILE) if state is None else state
    today = _today_key(now)
    if state.get("last_watch_reminder_date") == today:
        return False
    ok = send_feishu_text(f"看盘提醒：距离收盘约 15 分钟，请查看持仓和模型/卖出信号。时间 {now.strftime('%H:%M')}")
    if ok:
        state["last_watch_reminder_date"] = today
        if state_file_backed:
            save_json_state(NOTIFICATION_STATE_FILE, state)
    return ok
