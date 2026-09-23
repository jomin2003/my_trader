"""
shared_indicators.py — SINGLE SOURCE OF TRUTH for technical indicators.
===========================================================================
Extracts ATR, VWAP, RSI, EMA, MACD, Supertrend from 4+ duplicate
implementations (intraday_pattern_scanner_v2.py, indicators_ta.py,
vol_trainer.py, vectorbt_sweep.py) into ONE module.

All functions work on a DataFrame with columns [open, high, low, close, volume]
and return a float or None.  Never crash — always return None on insufficient data.

Usage:
    from shared_indicators import atr, rolling_vwap, rsi, ema, macd_hist
    atr_val = atr(df, period=14)
"""
from __future__ import annotations

import logging
import numpy as np
import pandas as pd

log = logging.getLogger("indicators")


# --------------------------------------------------------------------------- #
# CORE INDICATORS (numpy/pandas only — zero deps)                          #
# --------------------------------------------------------------------------- #

def atr(df: pd.DataFrame, period: int = 14) -> float | None:
    """Average True Range using Wilder's smoothing.
    
    Args:
        df: DataFrame with open/high/low/close columns
        period: ATR period (default 14)
    Returns:
        ATR value or None if insufficient data
    """
    if len(df) < period + 1:
        return None
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
    atr_vals = np.zeros_like(tr)
    atr_vals[period - 1] = tr[:period].mean()
    for i in range(period, len(tr)):
        atr_vals[i] = (atr_vals[i - 1] * (period - 1) + tr[i]) / period
    result = float(atr_vals[-1])
    return result if result > 0 else None


def rolling_vwap(df: pd.DataFrame) -> float | None:
    """Volume-weighted average price for the entire bar history.
    
    Returns None if less than 3 bars or no volume data.
    """
    if len(df) < 3:
        return None
    if "volume" not in df.columns:
        return None
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].values.astype(float)
    if vol.sum() <= 0:
        return float(tp.mean())
    return float((tp * vol).cumsum().iloc[-1] / vol.sum())


def rsi(df: pd.DataFrame, period: int = 14) -> float | None:
    """Relative Strength Index — simple average of gains/losses over the
    most recent `period` bars (Cutler's RSI).

    NOTE (2026-09-23 fix): this previously averaged the *oldest* `period`
    deltas in the frame (up[:period]) instead of the most recent ones, so
    the confluence score was reading momentum from hours ago. Fixed to
    use the trailing window.
    """
    if len(df) < period + 1:
        return None
    close = df["close"].values.astype(float)
    delta = np.diff(close)
    up = np.clip(delta, 0, np.inf)[-period:]
    dn = np.clip(-delta, 0, np.inf)[-period:]
    avg_up = np.mean(up)
    avg_dn = np.mean(dn)
    if avg_dn == 0:
        return 100.0
    rs = avg_up / avg_dn
    return float(100 - 100 / (1 + rs))


def ema(df, period: int = 20) -> float | None:
    """Exponential Moving Average.
    
    Accepts a pd.DataFrame, pd.Series, or list of values.
    """
    if isinstance(df, pd.DataFrame):
        if "close" in df.columns:
            close = df["close"].values.astype(float)
        elif len(df.columns) > 0:
            close = df.iloc[:, 0].values.astype(float)
        else:
            return None
    elif isinstance(df, pd.Series):
        close = df.values.astype(float)
    else:
        close = list(df)
    if len(close) < period:
        return None
    return float(pd.Series(close).ewm(span=period, adjust=False).mean().iloc[-1])


def macd_hist(df: pd.DataFrame) -> float | None:
    """MACD histogram (12/26/9)."""
    if len(df) < 35:
        return None
    close = df["close"].values.astype(float)
    ema12 = pd.Series(close).ewm(span=12, adjust=False).mean()
    ema26 = pd.Series(close).ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return float((macd - signal).iloc[-1])


def _wilder_atr_series(h: np.ndarray, l: np.ndarray, c: np.ndarray, period: int) -> np.ndarray:
    """Wilder-smoothed ATR series (matches TA-Lib/MT5 ADX Wilder)."""
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)]).astype(float)
    out = np.full_like(tr, np.nan, dtype=float)
    if len(tr) < period:
        return out
    out[period - 1] = float(np.mean(tr[:period]))
    for i in range(period, len(tr)):
        out[i] = (out[i - 1] * (period - 1) + tr[i]) / period
    return out


def supertrend(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> int:
    """Supertrend direction: +1 (uptrend), -1 (downtrend), 0 (unknown).

    Proper TradingView/O. Seban recurrence with carried final bands
    (upper only moves down, lower only moves up) and close-through flips.
    Old approximation built bands off the CURRENT bar only, so price could
    almost never cross them and it degenerated to an EMA proxy.
    """
    need = period + 2
    if len(df) < need:
        return 0
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    atr_s = _wilder_atr_series(h, l, c, period)
    hl2 = (h + l) / 2.0
    basic_up = hl2 + mult * atr_s
    basic_lo = hl2 - mult * atr_s
    # first ATR-ready bar seeds trend up (wickra-core convention)
    start = int(np.where(~np.isnan(atr_s))[0][0]) if np.any(~np.isnan(atr_s)) else period - 1
    f_up = basic_up[start]
    f_lo = basic_lo[start]
    direction = 1
    for i in range(start + 1, len(df)):
        bu, bl = basic_up[i], basic_lo[i]
        if np.isnan(bu) or np.isnan(bl):
            continue
        prev_close = c[i - 1]
        f_up = bu if (bu < f_up or prev_close > f_up) else f_up
        f_lo = bl if (bl > f_lo or prev_close < f_lo) else f_lo
        if direction < 0:
            direction = 1 if c[i] > f_up else -1
        else:
            direction = -1 if c[i] < f_lo else 1
    return int(direction)


def supertrend_series(df: pd.DataFrame, period: int = 10, mult: float = 3.0) -> pd.Series:
    """Per-bar Supertrend direction series (+1/-1, 0 where warmup)."""
    if len(df) < period + 2:
        return pd.Series([0] * len(df), index=df.index)
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    atr_s = _wilder_atr_series(h, l, c, period)
    hl2 = (h + l) / 2.0
    basic_up = hl2 + mult * atr_s
    basic_lo = hl2 - mult * atr_s
    dirs = np.zeros(len(df), dtype=int)
    start = int(np.where(~np.isnan(atr_s))[0][0])
    f_up = basic_up[start]; f_lo = basic_lo[start]; d = 1
    dirs[:start + 1] = 0
    for i in range(start + 1, len(df)):
        bu, bl = basic_up[i], basic_lo[i]
        if np.isnan(bu) or np.isnan(bl):
            dirs[i] = d; continue
        prev_close = c[i - 1]
        f_up = bu if (bu < f_up or prev_close > f_up) else f_up
        f_lo = bl if (bl > f_lo or prev_close < f_lo) else f_lo
        if d < 0:
            d = 1 if c[i] > f_up else -1
        else:
            d = -1 if c[i] < f_lo else 1
        dirs[i] = d
    return pd.Series(dirs, index=df.index)


def adx(df: pd.DataFrame, period: int = 14) -> float | None:
    """Wilder ADX (TA-Lib/MT5 Wilder convention).

    Fixes: old code divided the whole DI series by a single scalar
    (first ATR value) instead of the rolling Wilder ATR, and seeded ADX
    with one raw DX. Now: Wilder-smooth +DM/-DM/TR, +DI/-DI off smoothed
    TR, first ADX = mean of first `period` DX, then Wilder smoothing.
    Needs ~2*period bars for stable values; returns None before that.
    """
    if len(df) < 2 * period:
        return None
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    n = len(df)
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)]).astype(float)
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        up_move = h[i] - h[i - 1]
        dn_move = l[i - 1] - l[i]
        if up_move > dn_move and up_move > 0:
            plus_dm[i] = up_move
        elif dn_move > up_move and dn_move > 0:
            minus_dm[i] = dn_move
    # Wilder smoothing: seed with sum of first `period`, then X -= X/n; X += today
    s_tr = float(np.sum(tr[1:period + 1]))
    s_plus = float(np.sum(plus_dm[1:period + 1]))
    s_minus = float(np.sum(minus_dm[1:period + 1]))
    dx_vals: list[float] = []
    for i in range(period + 1, n):
        s_tr = s_tr - s_tr / period + tr[i]
        s_plus = s_plus - s_plus / period + plus_dm[i]
        s_minus = s_minus - s_minus / period + minus_dm[i]
        if s_tr <= 0:
            dx_vals.append(dx_vals[-1] if dx_vals else 0.0)
            continue
        pdi = 100.0 * s_plus / s_tr
        mdi = 100.0 * s_minus / s_tr
        denom = pdi + mdi
        dx_vals.append(100.0 * abs(pdi - mdi) / denom if denom > 0 else (dx_vals[-1] if dx_vals else 0.0))
    if len(dx_vals) < period:
        return None
    adx_v = float(np.mean(dx_vals[:period]))
    for v in dx_vals[period:]:
        adx_v = (adx_v * (period - 1) + v) / period
    return float(adx_v) if 0.0 <= adx_v <= 100.0 else float(adx_v)


# --------------------------------------------------------------------------- #
# CONVENIENCE WRAPERS                                                       #
# --------------------------------------------------------------------------- #

def wilder_atr(df: pd.DataFrame, period: int = 14) -> float | None:
    """Backward-compatible alias for atr().  Preserves the name used
    in the original codebase."""
    return atr(df, period)


def df_atr_fraction(df: pd.DataFrame, period: int = 14) -> float | None:
    """ATR as a fraction of close price.  Used by vol_trainer."""
    a = atr(df, period)
    if a is None:
        return None
    close = float(df["close"].iloc[-1])
    if close <= 0:
        return None
    return float(a / close)


# --------------------------------------------------------------------------- #
# CONFLUENCE HELPERS (used by indicators_ta)                                #
# --------------------------------------------------------------------------- #

def confluence_score(
    df: pd.DataFrame,
    direction: int,
    rsi_period: int = 14,
    ema_period: int = 20,
    cmf_period: int = 20,
    st_period: int = 10,
    st_mult: float = 3.0,
) -> tuple[int, str]:
    """Count how many indicators agree with the trade direction.
    
    Returns (boost: int, tags: str).
    Safe on short data — returns (0, "") if insufficient bars.
    """
    if df is None or len(df) < max(rsi_period, ema_period, cmf_period, st_period) + 5:
        return 0, ""

    close = df["close"].values.astype(float)
    if len(close) < 5:
        return 0, ""

    tags = []
    boost = 0

    # RSI — momentum band aligned with direction (matches indicators_ta):
    # long 45<=r<70, short 30<r<=55. Loose r<70/r>30 gates let exhausted
    # reversals through and inflated scores.
    r = rsi(df, rsi_period)
    if r is not None:
        if direction > 0 and 45 <= r < 70:
            boost += 1
            tags.append("RSI+")
        elif direction < 0 and 30 < r <= 55:
            boost += 1
            tags.append("RSI-")
        elif direction == 0:
            pass  # neutral

    # EMA stack
    e = ema(df, ema_period)
    if e is not None:
        current = float(close[-1])
        if (direction > 0 and current > e) or (direction < 0 and current < e):
            boost += 1
            tags.append("EMA+")

    # MACD histogram
    mh = macd_hist(df)
    if mh is not None:
        if (direction > 0 and mh > 0) or (direction < 0 and mh < 0):
            boost += 1
            tags.append("MACD+")

    # Supertrend
    st_dir = supertrend(df, st_period, st_mult)
    if st_dir != 0:
        if (direction > 0 and st_dir > 0) or (direction < 0 and st_dir < 0):
            boost += 1
            tags.append("ST+")

    return boost, " ".join(tags) if tags else ""


def session_vwap(df: pd.DataFrame) -> float | None:
    """Session-anchored VWAP (today's bars only when a tz-aware `ts` column exists).

    `rolling_vwap` accumulates the whole frame, which is wrong when the frame
    spans days. Live/backtest day-slices are usually single-session already;
    this makes the anchor explicit.
    """
    if len(df) < 3 or "volume" not in df.columns:
        return rolling_vwap(df)
    try:
        if "ts" in df.columns:
            day = df["ts"].dt.date.iloc[-1]
            d = df[df["ts"].dt.date == day]
            if len(d) >= 3:
                tp = (d["high"] + d["low"] + d["close"]) / 3.0
                vol = d["volume"].values.astype(float)
                if vol.sum() > 0:
                    return float((tp * vol).cumsum().iloc[-1] / vol.sum())
    except Exception:
        pass
    return rolling_vwap(df)


def vwap_slope_pct(df: pd.DataFrame, lookback: int = 6) -> float | None:
    """VWAP slope over last `lookback` bars, in % (per-bar, relative).

    Flat VWAP (|slope| < ~0.005%/bar, i.e. <0.03%/30min on 5m) = chop:
    skip VWAP crosses/reclaims there. Returns None if unavailable.
    """
    try:
        if len(df) < lookback + 3 or "volume" not in df.columns:
            return None
        vals = []
        sub = df.tail(lookback + 1)
        for i in range(len(sub)):
            v = session_vwap(df.iloc[: len(df) - len(sub) + i + 1])
            if v is None:
                return None
            vals.append(v)
        if vals[0] <= 0:
            return None
        return float((vals[-1] / vals[0] - 1.0) * 100.0 / max(len(vals) - 1, 1))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# BACKTEST-SAFE ACCESSORS                                                 #
# --------------------------------------------------------------------------- #

def in_time_window(ts, start: tuple[int, int], end: tuple[int, int]) -> bool:
    """True if ts falls in [start, end). start/end are (hour, minute) tuples.

    Single shared implementation of the intraday session-window check that
    used to be copy-pasted across the strategy modules.
    """
    t = ts.time() if hasattr(ts, "time") else ts
    cur = t.hour * 60 + t.minute
    return start[0] * 60 + start[1] <= cur < end[0] * 60 + end[1]


def latest_ohlcv(df: pd.DataFrame) -> dict | None:
    """Extract latest OHLCV as a plain dict.  Safe on empty DataFrame."""
    if len(df) < 1:
        return None
    row = df.iloc[-1]
    return {
        "open": float(row["open"]),
        "high": float(row["high"]),
        "low": float(row["low"]),
        "close": float(row["close"]),
        "volume": float(row["volume"]) if "volume" in df.columns else 0.0,
    }


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    # Quick self-test
    np.random.seed(42)
    n = 50
    close = 100 + np.cumsum(np.random.randn(n) * 0.5)
    high = close + np.abs(np.random.randn(n) * 0.3)
    low = close - np.abs(np.random.randn(n) * 0.3)
    open_p = close - np.random.rand(n) * 0.4 + 0.2
    volume = np.random.randint(100000, 1000000, n).astype(float)

    df = pd.DataFrame({"open": open_p, "high": high, "low": low, "close": close, "volume": volume})

    print(f"ATR(14): {atr(df)}")
    print(f"VWAP: {rolling_vwap(df)}")
    print(f"RSI(14): {rsi(df)}")
    print(f"EMA(20): {ema(df)}")
    print(f"MACD hist: {macd_hist(df)}")
    print(f"ADX(14): {adx(df)}")
    print(f"Supertrend: {supertrend(df)}")
    print(f"Confluence(+1): {confluence_score(df, +1)}")
    print("All shared_indicators tests passed ✓")
