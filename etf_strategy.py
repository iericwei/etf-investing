"""
多因子评分模型
  动量因子  (35%): 3日 + 5日涨跌幅加权
  量能因子  (25%): 量比 × 价格方向协同
  技术因子  (25%): RSI 健康区间 + MACD 方向 + 均线排列
  趋势因子  (15%): 10日涨跌幅
硬过滤: RSI>80 | 5日跌幅>8% | 破MA20且持续下跌
"""

import numpy as np
import pandas as pd
from typing import Dict, List


# ── 技术指标计算 ───────────────────────────────────────────────────────

def _rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.inf)
    return 100 - (100 / (1 + rs))


def _macd(closes: pd.Series, fast=12, slow=26, signal=9):
    ema_f = closes.ewm(span=fast, adjust=False).mean()
    ema_s = closes.ewm(span=slow, adjust=False).mean()
    macd = ema_f - ema_s
    sig = macd.ewm(span=signal, adjust=False).mean()
    return macd, sig, macd - sig


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    c = df["close"]
    v = df["volume"]

    df["ma5"]  = c.rolling(5).mean()
    df["ma10"] = c.rolling(10).mean()
    df["ma20"] = c.rolling(20).mean()
    df["rsi"]  = _rsi(c)

    _, _, df["macd_hist"] = _macd(c)

    df["vol_ma20"]  = v.rolling(20).mean()
    df["vol_ratio"] = v / df["vol_ma20"].replace(0, np.nan)

    df["ret1"]  = c.pct_change(1) * 100
    df["ret3"]  = c.pct_change(3) * 100
    df["ret5"]  = c.pct_change(5) * 100
    df["ret10"] = c.pct_change(10) * 100

    return df


# ── 归一化 ────────────────────────────────────────────────────────────

def _normalize(series: pd.Series) -> pd.Series:
    mn, mx = series.min(), series.max()
    if abs(mx - mn) < 1e-10:
        return pd.Series(50.0, index=series.index)
    return (series - mn) / (mx - mn) * 100


# ── 多因子评分 ────────────────────────────────────────────────────────

def _score_all(etf_map: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for code, df in etf_map.items():
        last = df.iloc[-1]
        cl = float(last.get("close", 0))
        rows.append({
            "code":       code,
            "close":      cl,
            "ret3":       float(last.get("ret3", 0)),
            "ret5":       float(last.get("ret5", 0)),
            "ret10":      float(last.get("ret10", 0)),
            "rsi":        float(last.get("rsi", 50)),
            "macd_hist":  float(last.get("macd_hist", 0)),
            "vol_ratio":  float(last.get("vol_ratio", 1) or 1),
            "above_ma5":  int(cl > float(last.get("ma5", 0))),
            "above_ma10": int(cl > float(last.get("ma10", 0))),
            "above_ma20": int(cl > float(last.get("ma20", 0))),
            "ma_aligned": int(
                float(last.get("ma5", 0)) >
                float(last.get("ma10", 0)) >
                float(last.get("ma20", 0))
            ),
        })

    if not rows:
        return pd.DataFrame()

    s = pd.DataFrame(rows).set_index("code")

    # 动量因子 (35%)
    momentum = _normalize(s["ret3"] * 0.55 + s["ret5"] * 0.45)

    # 趋势因子 (15%)
    trend = _normalize(s["ret10"])

    # 量能因子 (25%): 量比 × 价格方向协同
    # ret3 clip(-4, 15) + 5 → 始终为正，量价共振时得高分
    vol_synergy = s["vol_ratio"] * (s["ret3"].clip(-4, 15) + 5)
    volume = _normalize(vol_synergy)

    # 技术因子 (25%)
    # RSI: 偏离 55 越远得分越低（过热/超卖均扣分）
    rsi_score = (100 - (s["rsi"] - 55).abs() * 2).clip(0, 100)
    macd_score = _normalize(s["macd_hist"])
    ma_score = s["above_ma5"] * 33 + s["above_ma10"] * 33 + s["ma_aligned"] * 34
    technical = rsi_score * 0.35 + macd_score * 0.35 + ma_score * 0.30

    s["score"]           = (momentum * 0.35 + trend * 0.15 + volume * 0.25 + technical * 0.25).round(1)
    s["momentum_score"]  = momentum.round(1)
    s["volume_score"]    = volume.round(1)
    s["technical_score"] = technical.round(1)
    s["trend_score"]     = trend.round(1)

    return s.reset_index()


def _passes_filter(row: pd.Series) -> tuple:
    """硬过滤，返回 (通过, 拒绝原因)"""
    if row["rsi"] > 82:
        return False, "RSI过热"
    if row["ret5"] < -9:
        return False, "5日跌幅>9%"
    # 破 MA20 且近期持续下跌（双重确认，避免误杀震荡标的）
    if row["above_ma20"] == 0 and row["ret3"] < -3 and row["ret5"] < -5:
        return False, "破MA20且持续下跌"
    return True, ""


# ── 主选股函数 ────────────────────────────────────────────────────────

def select_top(
    pool: list,
    etf_map: Dict[str, pd.DataFrame],
    realtime: Dict[str, dict],
    top_n: int = 10,
) -> List[dict]:
    """
    综合历史数据 + 实时行情评分，返回 top_n 个结果（已排序）
    """
    # 将实时最新价格合并进历史末行，重新计算指标
    enriched: Dict[str, pd.DataFrame] = {}
    for item in pool:
        code = item["code"]
        if code not in etf_map:
            continue
        df = etf_map[code].copy()
        rt = realtime.get(code)
        if rt and rt.get("price", 0) > 0:
            df.loc[df.index[-1], "close"] = rt["price"]
            if rt.get("volume", 0) > 0:
                df.loc[df.index[-1], "volume"] = rt["volume"]
        enriched[code] = compute_indicators(df)

    if not enriched:
        return []

    df_score = _score_all(enriched)

    # 硬过滤
    ok_mask = []
    for _, row in df_score.iterrows():
        passed, _ = _passes_filter(row)
        ok_mask.append(passed)
    df_score = df_score[ok_mask].sort_values("score", ascending=False).head(top_n)

    # 组装输出
    pool_meta = {item["code"]: item for item in pool}
    results = []
    for rank, (_, row) in enumerate(df_score.iterrows(), start=1):
        code = row["code"]
        meta = pool_meta.get(code, {})
        rt = realtime.get(code, {})
        last = enriched[code].iloc[-1]

        results.append({
            "rank":            rank,
            "code":            code,
            "name":            rt.get("name") or meta.get("name", code),
            "category":        meta.get("category", ""),
            "price":           rt.get("price") or float(last.get("close", 0)),
            "change_pct":      round(rt.get("change_pct", 0), 2),
            "ret3":            round(row["ret3"], 2),
            "ret5":            round(row["ret5"], 2),
            "ret10":           round(row["ret10"], 2),
            "rsi":             round(row["rsi"], 1),
            "vol_ratio":       round(row["vol_ratio"], 2),
            "ma_aligned":      bool(row["ma_aligned"]),
            "macd_bullish":    bool(row["macd_hist"] > 0),
            "score":           row["score"],
            "momentum_score":  row["momentum_score"],
            "volume_score":    row["volume_score"],
            "technical_score": row["technical_score"],
        })
    return results


# ── 卖出信号模型 ───────────────────────────────────────────────────────

_SIG_WEIGHT = {"强": 3, "中": 2, "弱": 1}


def compute_sell_signals(df: pd.DataFrame, realtime_price: float = 0) -> dict:
    """
    基于历史日 K + 实时价格计算卖出参考信号

    返回:
      signals      : [{"name": str, "level": "弱/中/强"}]
      urgency      : "持有 / 关注 / 考虑减仓 / 建议卖出"
      urgency_level: 0 / 1 / 2 / 3
    """
    if df.empty or len(df) < 10:
        return {"signals": [], "urgency": "数据不足", "urgency_level": -1}

    df = compute_indicators(df.copy())
    last = df.iloc[-1]

    price = realtime_price if realtime_price > 0 else float(last.get("close", 0))
    if price <= 0:
        return {"signals": [], "urgency": "数据不足", "urgency_level": -1}

    ma5   = float(last.get("ma5")  or 0)
    ma10  = float(last.get("ma10") or 0)
    ma20  = float(last.get("ma20") or 0)
    rsi   = float(last.get("rsi")  or 50)
    hist  = float(last.get("macd_hist") or 0)
    prev_hist = float(df["macd_hist"].iloc[-2]) if len(df) >= 2 else 0

    sigs = []

    # ── RSI 过热 ────────────────────────────────────────────────────
    if rsi > 80:
        sigs.append({"name": f"RSI过热 {rsi:.0f}", "level": "强"})
    elif rsi > 75:
        sigs.append({"name": f"RSI偏高 {rsi:.0f}", "level": "中"})
    elif rsi > 70:
        sigs.append({"name": f"RSI偏高 {rsi:.0f}", "level": "弱"})

    # ── MACD 柱状线转负（金叉→死叉）──────────────────────────────────
    if hist < 0 and prev_hist >= 0:
        sigs.append({"name": "MACD 刚转空", "level": "中"})
    elif hist < 0:
        sigs.append({"name": "MACD 看空",   "level": "弱"})

    # ── 均线位置 ────────────────────────────────────────────────────
    if ma20 > 0 and price < ma20:
        sigs.append({"name": "跌破 MA20", "level": "强"})
    elif ma10 > 0 and price < ma10:
        sigs.append({"name": "跌破 MA10", "level": "中"})
    elif ma5 > 0 and price < ma5:
        sigs.append({"name": "跌破 MA5",  "level": "弱"})

    # ── 均线空头排列 ─────────────────────────────────────────────────
    if ma5 > 0 and ma10 > 0 and ma20 > 0 and ma5 < ma10 < ma20:
        sigs.append({"name": "均线空头排列", "level": "强"})
    elif ma5 > 0 and ma10 > 0 and ma5 < ma10:
        sigs.append({"name": "均线死叉",    "level": "中"})

    # ── 高位回落（距近 5 日最高点）────────────────────────────────────
    if "high" in df.columns:
        high5 = float(df["high"].tail(5).max())
        if high5 > price:
            drop = (high5 - price) / high5 * 100
            if drop >= 5:
                sigs.append({"name": f"高位回落 {drop:.1f}%", "level": "强"})
            elif drop >= 3:
                sigs.append({"name": f"高位回落 {drop:.1f}%", "level": "中"})

    # ── 今日跌幅（实时 vs 昨收）──────────────────────────────────────
    prev_close = float(last.get("close") or 0)
    if prev_close > 0 and realtime_price > 0:
        today_ret = (realtime_price - prev_close) / prev_close * 100
        if today_ret < -3:
            sigs.append({"name": f"今日跌 {abs(today_ret):.1f}%", "level": "强"})
        elif today_ret < -1.5:
            sigs.append({"name": f"今日跌 {abs(today_ret):.1f}%", "level": "中"})

    # ── 综合评级 ─────────────────────────────────────────────────────
    total = sum(_SIG_WEIGHT[s["level"]] for s in sigs)
    if total == 0:
        urgency, ulevel = "持有",    0
    elif total <= 2:
        urgency, ulevel = "关注",    1
    elif total <= 5:
        urgency, ulevel = "考虑减仓", 2
    else:
        urgency, ulevel = "建议卖出", 3

    return {"signals": sigs, "urgency": urgency, "urgency_level": ulevel}
