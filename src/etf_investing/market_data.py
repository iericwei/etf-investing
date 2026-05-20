"""本地分钟行情库：SQLite 存储、数据源切换、请求日志。"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable
from contextlib import closing

import pandas as pd

from .config import CONFIG
from .data import fetch_eastmoney_intraday_history

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "market_data.sqlite3"

VALID_PERIODS = {"1", "5", "15", "30", "60"}
DEFAULT_INTRADAY_PERIOD = "5"


@dataclass
class IntradayFetchResult:
    df: pd.DataFrame
    source: str
    error: str | None = None


def normalize_code(code: str) -> str:
    return str(code).strip().zfill(6)[-6:]


def normalize_period(period: str) -> str:
    period = str(period).strip()
    if period not in VALID_PERIODS:
        raise ValueError(f"不支持的 period={period}，可选: {sorted(VALID_PERIODS)}")
    return period


def market_prefix(code: str) -> str:
    code = normalize_code(code)
    return "SH" if code.startswith(("5", "6", "9", "11", "12", "13", "18")) else "SZ"


def _db_path(path: str | Path | None = None) -> Path:
    return Path(path) if path else DEFAULT_DB_PATH


class MarketDataStore:
    """SQLite 分钟行情库存储。"""

    def __init__(self, path: str | Path | None = None):
        self.path = _db_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS intraday_bars (
                    code TEXT NOT NULL,
                    period TEXT NOT NULL,
                    datetime TEXT NOT NULL,
                    date TEXT NOT NULL,
                    time TEXT NOT NULL,
                    open REAL,
                    close REAL,
                    high REAL,
                    low REAL,
                    volume REAL,
                    amount REAL,
                    source TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY (code, period, datetime)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_intraday_code_date ON intraday_bars(code, period, date)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS request_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    code TEXT,
                    period TEXT,
                    days INTEGER,
                    source TEXT,
                    success INTEGER NOT NULL,
                    rows INTEGER NOT NULL,
                    error TEXT,
                    detail TEXT
                )
                """
            )
            conn.commit()

    def save_intraday(self, code: str, period: str, df: pd.DataFrame, source: str) -> int:
        code = normalize_code(code)
        period = normalize_period(period)
        if df is None or df.empty:
            return 0
        rows = []
        fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for _, row in df.iterrows():
            dt = pd.to_datetime(row.get("datetime"), errors="coerce")
            if pd.isna(dt):
                continue
            rows.append(
                (
                    code,
                    period,
                    dt.strftime("%Y-%m-%d %H:%M:%S"),
                    dt.strftime("%Y-%m-%d"),
                    str(row.get("time") or dt.strftime("%H:%M")),
                    _num(row.get("open")),
                    _num(row.get("close")),
                    _num(row.get("high")),
                    _num(row.get("low")),
                    _num(row.get("volume")),
                    _num(row.get("amount")),
                    source,
                    fetched_at,
                )
            )
        if not rows:
            return 0
        with closing(self.connect()) as conn:
            conn.executemany(
                """
                INSERT INTO intraday_bars (
                    code, period, datetime, date, time, open, close, high, low, volume, amount, source, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(code, period, datetime) DO UPDATE SET
                    date=excluded.date,
                    time=excluded.time,
                    open=excluded.open,
                    close=excluded.close,
                    high=excluded.high,
                    low=excluded.low,
                    volume=excluded.volume,
                    amount=excluded.amount,
                    source=excluded.source,
                    fetched_at=excluded.fetched_at
                """,
                rows,
            )
            conn.commit()
        return len(rows)

    def load_intraday(self, code: str, period: str, start_date: date, end_date: date) -> pd.DataFrame:
        code = normalize_code(code)
        period = normalize_period(period)
        with closing(self.connect()) as conn:
            rows = conn.execute(
                """
                SELECT datetime, date, time, open, close, high, low, volume, amount, source
                FROM intraday_bars
                WHERE code = ? AND period = ? AND date BETWEEN ? AND ?
                ORDER BY datetime
                """,
                (code, period, start_date.isoformat(), end_date.isoformat()),
            ).fetchall()
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([dict(row) for row in rows])
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        for col in ["open", "close", "high", "low", "volume", "amount"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.dropna(subset=["datetime"]).reset_index(drop=True)

    def log_request(
        self,
        *,
        endpoint: str,
        code: str | None = None,
        period: str | None = None,
        days: int | None = None,
        source: str | None = None,
        success: bool,
        rows: int = 0,
        error: str | None = None,
        detail: dict | None = None,
    ) -> None:
        with closing(self.connect()) as conn:
            conn.execute(
                """
                INSERT INTO request_logs(timestamp, endpoint, code, period, days, source, success, rows, error, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    endpoint,
                    code,
                    period,
                    days,
                    source,
                    1 if success else 0,
                    int(rows or 0),
                    error,
                    json.dumps(detail or {}, ensure_ascii=False),
                ),
            )
            conn.commit()

    def recent_logs(self, limit: int = 50) -> list[dict]:
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM request_logs ORDER BY id DESC LIMIT ?",
                (max(min(int(limit), 500), 1),),
            ).fetchall()
        return [dict(row) for row in rows]


def _num(value) -> float | None:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _today_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "datetime" not in df.columns:
        return pd.DataFrame()
    today = pd.Timestamp(datetime.now().date())
    copy = df.copy()
    copy["datetime"] = pd.to_datetime(copy["datetime"], errors="coerce")
    return copy[copy["datetime"].dt.normalize() == today].dropna(subset=["datetime"])


def fetch_current_intraday(code: str, period: str, days: int = 3) -> IntradayFetchResult:
    """优先用现有行情接口获取当天分钟级数据；若当天为空，降级 FUTU。"""
    code = normalize_code(code)
    period = normalize_period(period)
    errors: list[str] = []

    try:
        df = fetch_eastmoney_intraday_history(code, period=period, days=max(days, 1))
        today_df = _today_rows(df)
        if not today_df.empty:
            return IntradayFetchResult(today_df, "eastmoney")
        errors.append("eastmoney 当天分钟数据为空")
    except Exception as e:
        errors.append(f"eastmoney 异常: {e}")

    futu_result = fetch_futu_intraday_history(code, period=period, days=max(days, 1))
    if not futu_result.df.empty:
        return futu_result
    if futu_result.error:
        errors.append(futu_result.error)
    return IntradayFetchResult(pd.DataFrame(), "none", "; ".join(errors))


def fetch_futu_intraday_history(
    code: str,
    period: str = "15",
    days: int = 3,
    *,
    start_date: date | str | None = None,
    end_date: date | str | None = None,
) -> IntradayFetchResult:
    """可选 FUTU OpenAPI 分时历史数据源。未安装/未启动 OpenD 时返回空。"""
    code = normalize_code(code)
    period = normalize_period(period)
    try:
        import futu as ft
    except Exception as e:
        return IntradayFetchResult(pd.DataFrame(), "futu", f"futu-api 不可用: {e}")

    ktype_map = {
        "1": getattr(ft.KLType, "K_1M", "K_1M"),
        "5": getattr(ft.KLType, "K_5M", "K_5M"),
        "15": getattr(ft.KLType, "K_15M", "K_15M"),
        "30": getattr(ft.KLType, "K_30M", "K_30M"),
        "60": getattr(ft.KLType, "K_60M", "K_60M"),
    }
    host = CONFIG.get("futu", {}).get("host", "127.0.0.1")
    port = int(CONFIG.get("futu", {}).get("port", 11111))
    symbol = f"{market_prefix(code)}.{code}"
    if start_date is not None or end_date is not None:
        start_obj = date.fromisoformat(str(start_date)) if start_date is not None else datetime.now().date()
        end_obj = date.fromisoformat(str(end_date)) if end_date is not None else start_obj
        start = start_obj.isoformat()
        end = end_obj.isoformat()
    else:
        start = (datetime.now() - timedelta(days=max(int(days), 1))).strftime("%Y-%m-%d")
        end = datetime.now().strftime("%Y-%m-%d")
    quote_ctx = None
    try:
        quote_ctx = ft.OpenQuoteContext(host=host, port=port)
        ret, raw, _ = quote_ctx.request_history_kline(symbol, start=start, end=end, ktype=ktype_map[period], max_count=10000)
        if ret != ft.RET_OK:
            return IntradayFetchResult(pd.DataFrame(), "futu", f"futu 请求失败: {raw}")
        df = _normalize_futu_intraday(raw)
        today_df = _today_rows(df)
        return IntradayFetchResult(today_df if not today_df.empty else df, "futu")
    except Exception as e:
        return IntradayFetchResult(pd.DataFrame(), "futu", f"futu 异常: {e}")
    finally:
        if quote_ctx is not None:
            try:
                quote_ctx.close()
            except Exception:
                pass


def fetch_futu_today_intraday_history(code: str, period: str = "15") -> IntradayFetchResult:
    """通过 FUTU OpenAPI 获取当天分钟行情；只返回当天行。"""
    result = fetch_futu_intraday_history(code, period=period, days=3)
    if result.df.empty:
        return result
    today_df = _today_rows(result.df)
    if today_df.empty:
        return IntradayFetchResult(pd.DataFrame(), "futu", "futu 当天分钟数据为空")
    return IntradayFetchResult(today_df, "futu", result.error)


def _normalize_futu_intraday(raw: pd.DataFrame) -> pd.DataFrame:
    if raw is None or raw.empty:
        return pd.DataFrame()
    df = raw.rename(
        columns={
            "time_key": "datetime",
            "open": "open",
            "close": "close",
            "high": "high",
            "low": "low",
            "volume": "volume",
            "turnover": "amount",
        }
    ).copy()
    if "datetime" not in df.columns:
        return pd.DataFrame()
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"])
    if df.empty:
        return pd.DataFrame()
    df["date"] = df["datetime"].dt.normalize()
    df["time"] = df["datetime"].dt.strftime("%H:%M")
    if "amount" not in df.columns:
        df["amount"] = 0
    for col in ["open", "close", "high", "low", "volume", "amount"]:
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors="coerce")
    keep = ["datetime", "date", "time", "open", "close", "high", "low", "volume", "amount"]
    return df[keep].dropna(subset=["close"]).sort_values("datetime").reset_index(drop=True)


def get_intraday_from_store_or_fetch(
    code: str,
    period: str = DEFAULT_INTRADAY_PERIOD,
    days: int = 5,
    *,
    refresh: bool = False,
    store: MarketDataStore | None = None,
) -> tuple[pd.DataFrame, str, str | None]:
    """读取本地行情库；需要当天数据或强刷时从接口/FUTU 拉取并入库。"""
    store = store or MarketDataStore()
    code = normalize_code(code)
    period = normalize_period(period)
    days = max(int(days), 1)
    end = datetime.now().date()
    start = end - timedelta(days=days)

    if not refresh:
        cached = store.load_intraday(code, period, start, end)
        today_cached = _today_rows(cached)
        if not cached.empty and not today_cached.empty:
            return cached, "local_store", None

    fetched = fetch_current_intraday(code, period, days=min(max(days, 1), 10))
    if not fetched.df.empty:
        store.save_intraday(code, period, fetched.df, fetched.source)

    combined = store.load_intraday(code, period, start, end)
    if not combined.empty:
        return combined, f"local_store+{fetched.source}" if not fetched.df.empty else "local_store", fetched.error
    return pd.DataFrame(), fetched.source, fetched.error


def backfill_intraday_history(
    codes: Iterable[str],
    period: str = DEFAULT_INTRADAY_PERIOD,
    *,
    days: int = 30,
    store: MarketDataStore | None = None,
    futu_batch_size: int = 55,
    futu_pause_seconds: float = 31.0,
) -> dict:
    """优先用 FUTU 回溯历史分时；FUTU 无数据时兼容回填当天行情。"""
    store = store or MarketDataStore()
    period = normalize_period(period)
    days = max(int(days), 1)
    summary = {"success": True, "period": period, "days": days, "total_rows": 0, "items": []}
    normalized_codes = [normalize_code(code) for code in codes]
    for index, code in enumerate(normalized_codes):
        if index > 0 and futu_batch_size > 0 and index % futu_batch_size == 0 and futu_pause_seconds > 0:
            time.sleep(futu_pause_seconds)
        fetched = fetch_futu_intraday_history(code, period=period, days=days)
        endpoint = "backfill_history"
        if fetched.df.empty:
            fallback = fetch_current_intraday(code, period, days=3)
            fetched = IntradayFetchResult(fallback.df, fallback.source, fetched.error or fallback.error)
            endpoint = "backfill_today"
        saved = store.save_intraday(code, period, fetched.df, fetched.source) if not fetched.df.empty else 0
        ok = saved > 0
        item = {"code": code, "success": ok, "source": fetched.source, "rows": saved, "error": fetched.error}
        summary["items"].append(item)
        summary["total_rows"] += saved
        store.log_request(
            endpoint=endpoint,
            code=code,
            period=period,
            days=days if endpoint == "backfill_history" else 1,
            source=fetched.source,
            success=ok,
            rows=saved,
            error=fetched.error,
        )
    summary["success"] = any(item["success"] for item in summary["items"])
    return summary


def backfill_today(codes: Iterable[str], period: str = DEFAULT_INTRADAY_PERIOD, *, store: MarketDataStore | None = None) -> dict:
    store = store or MarketDataStore()
    period = normalize_period(period)
    summary = {"success": True, "period": period, "total_rows": 0, "items": []}
    for code in codes:
        code = normalize_code(code)
        fetched = fetch_current_intraday(code, period, days=3)
        saved = store.save_intraday(code, period, fetched.df, fetched.source) if not fetched.df.empty else 0
        ok = saved > 0
        item = {"code": code, "success": ok, "source": fetched.source, "rows": saved, "error": fetched.error}
        summary["items"].append(item)
        summary["total_rows"] += saved
        store.log_request(
            endpoint="backfill_today",
            code=code,
            period=period,
            days=1,
            source=fetched.source,
            success=ok,
            rows=saved,
            error=fetched.error,
        )
    summary["success"] = any(item["success"] for item in summary["items"])
    return summary
