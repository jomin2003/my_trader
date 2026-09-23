"""
=====================================================================
TIER-1 #1 — RICH INDICATORS via pandas-ta  (strategy-scoring booster)
=====================================================================
Adds a library of confirmation indicators on top of your hand-coded
ATR/VWAP/ADX, then rolls them into a single 0..N "confluence score" you
can ADD to your existing signal score.

Design goals for YOUR bot:
  * Pure-Python (pandas-ta) — safe on Render's 512MB, no C build.
  * SAFE FALLBACK: if pandas-ta isn't installed, every function degrades
    to a lightweight numpy/pandas implementation, so the bot NEVER
    crashes for a missing dep.
  * Direction-aware: confluence_score(df, direction) returns how many
    indicators AGREE with the trade's direction (+1 long / -1 short).

Wire it into multi_strategy_live.score_signals (recommended) — where the
per-symbol bar `df` is available:
    import indicators_ta as ita
    boost, tags = ita.confluence_score(df, direction)   # df has o/h/l/c/volume
    row["score"] += boost                                # nudge the score
    row["ta"] = tags                                     # e.g. "ST+,EMA+,CMF+"

Install (add to the live bot's requirements.txt):
    pandas-ta>=0.3.14b0
"""
from __future__ import annotations

import logging
import numpy as np
import pandas as pd

log = logging.getLogger("indicators_ta")

# ---- optional dependency ----
try:
    import pandas_ta as pta  # noqa
    _PTA = True
except Exception:
    _PTA = False
    log.info("pandas_ta not installed — using numpy fallbacks")

# =====================================================================
# TUNABLES  (weights for each confirming indicator)
# =====================================================================
W_RSI        = 1     # momentum not overextended against us
W_SUPERTREND = 1     # trend agrees
W_EMA_STACK  = 1     # price vs EMA(20) agrees
W_CMF        = 1     # money-flow agrees
W_MACD       = 1     # MACD histogram agrees
RSI_LEN      = 14
CMF_LEN      = 20
ST_LEN       = 10
ST_MULT      = 3.0
EMA_LEN      = 20


# =====================================================================
# FALLBACK PRIMITIVES (used when pandas_ta is absent)
# =====================================================================
def _rsi(close: pd.Series, length: int = 14) -> float | None:
    if len(close) < length + 1:
        return None
    delta = close.diff()
    up = delta.clip(lower=0).rolling(length).mean()
    dn = (-delta.clip(upper=0)).rolling(length).mean()
    rs = up / dn.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)
    v = rsi.iloc[-1]
    return float(v) if pd.notna(v) else None


def _ema(close: pd.Series, length: int) -> float | None:
    # Canonical implementation lives in shared_indicators (identical math:
    # ewm(span=length, adjust=False)); kept here as a thin wrapper so the
    # pandas_ta-preferring read path doesn't change.
    try:
        from shared_indicators import ema as _shared_ema
        return _shared_ema(close, length)
    except Exception:
        pass
    if len(close) < length:
        return None
    return float(close.ewm(span=length, adjust=False).mean().iloc[-1])


def _macd_hist(close: pd.Series) -> float | None:
    # Canonical implementation lives in shared_indicators (identical 12/26/9).
    try:
        from shared_indicators import macd_hist as _shared_mh
        return _shared_mh(pd.DataFrame({"close": close}))
    except Exception:
        pass
    if len(close) < 35:
        return None
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return float((macd - signal).iloc[-1])


def _cmf(df: pd.DataFrame, length: int = 20) -> float | None:
    if len(df) < length:
        return None
    h, l, c, v = df["high"], df["low"], df["close"], df["volume"]
    rng = (h - l).replace(0, np.nan)
    mfm = ((c - l) - (h - c)) / rng
    mfv = mfm * v
    denom = v.rolling(length).sum()
    cmf = mfv.rolling(length).sum() / denom.replace(0, np.nan)
    val = cmf.iloc[-1]
    return float(val) if pd.notna(val) else None


def _atr(df: pd.DataFrame, length: int = 10) -> float | None:
    # Wilder ATR via the canonical shared implementation (was SMA-based).
    try:
        from shared_indicators import _wilder_atr_series
        h, l, c = df["high"].values, df["low"].values, df["close"].values
        s = _wilder_atr_series(h.astype(float), l.astype(float), c.astype(float), length)
        v = s[-1]
        return float(v) if pd.notna(v) else None
    except Exception:
        pass
    if len(df) < length + 1:
        return None
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    prev = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - prev), np.abs(l - prev)])
    return float(pd.Series(tr).rolling(length).mean().iloc[-1])


def _supertrend_dir(df: pd.DataFrame, length: int = 10, mult: float = 3.0) -> int:
    """Proper Supertrend direction via shared canonical recurrence.

    Old version built bands off the CURRENT bar only, so price could almost
    never cross them and it degenerated to an EMA proxy (ST tag ~= EMA tag).
    """
    try:
        from shared_indicators import supertrend as _st
        return int(_st(df, period=length, mult=mult))
    except Exception:
        pass
    atr = _atr(df, length)
    if atr is None:
        return 0
    e = _ema(df["close"], EMA_LEN)
    if e is None:
        return 0
    c = float(df["close"].iloc[-1])
    return 1 if c >= e else -1


# =====================================================================
# INDICATOR READS  (prefer pandas_ta, else fallback)
# =====================================================================
def _read_rsi(df):
    if _PTA:
        try:
            s = pta.rsi(df["close"], length=RSI_LEN)
            if s is not None and len(s.dropna()):
                return float(s.dropna().iloc[-1])
        except Exception:
            pass
    return _rsi(df["close"], RSI_LEN)


def _read_cmf(df):
    if _PTA:
        try:
            s = pta.cmf(df["high"], df["low"], df["close"], df["volume"], length=CMF_LEN)
            if s is not None and len(s.dropna()):
                return float(s.dropna().iloc[-1])
        except Exception:
            pass
    return _cmf(df, CMF_LEN)


def _read_macd_hist(df):
    if _PTA:
        try:
            m = pta.macd(df["close"])
            if m is not None and len(m.dropna()):
                hcol = [c for c in m.columns if c.upper().startswith("MACDH")]
                if hcol:
                    return float(m[hcol[0]].dropna().iloc[-1])
        except Exception:
            pass
    return _macd_hist(df["close"])


def _read_supertrend_dir(df):
    if _PTA:
        try:
            st = pta.supertrend(df["high"], df["low"], df["close"],
                                length=ST_LEN, multiplier=ST_MULT)
            if st is not None and len(st.dropna()):
                dcol = [c for c in st.columns if c.upper().startswith("SUPERTD")]
                if dcol:
                    return int(np.sign(st[dcol[0]].dropna().iloc[-1]))
        except Exception:
            pass
    return _supertrend_dir(df, ST_LEN, ST_MULT)


# =====================================================================
# CONFLUENCE SCORE — the thing you call from the scanner
# =====================================================================
def confluence_score(df: pd.DataFrame, direction: int):
    """Return (boost:int, tags:str). boost = weighted count of indicators
       that AGREE with `direction` (+1 long / -1 short). Neutral inputs add 0.
       Safe on short data: anything unavailable simply contributes 0."""
    if df is None or len(df) < 5 or direction == 0:
        return 0, ""
    df = df.copy()
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            return 0, ""
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    if len(df) < 5:
        return 0, ""

    boost = 0
    tags = []
    close = float(df["close"].iloc[-1])
    d = 1 if direction > 0 else -1

    # 1) RSI: long wants RSI not overbought (<70) and above ~45;
    #         short wants RSI not oversold (>30) and below ~55.
    rsi = _read_rsi(df)
    if rsi is not None:
        if d > 0 and 45 <= rsi < 70:
            boost += W_RSI; tags.append("RSI+")
        elif d < 0 and 30 < rsi <= 55:
            boost += W_RSI; tags.append("RSI-")

    # 2) Supertrend direction agrees
    st = _read_supertrend_dir(df)
    if st == d:
        boost += W_SUPERTREND; tags.append("ST+")

    # 3) EMA stack: price above EMA20 for longs, below for shorts
    e = _ema(df["close"], EMA_LEN)
    if e is not None:
        if d > 0 and close >= e:
            boost += W_EMA_STACK; tags.append("EMA+")
        elif d < 0 and close <= e:
            boost += W_EMA_STACK; tags.append("EMA-")

    # 4) CMF money-flow agrees (positive for longs, negative for shorts)
    cmf = _read_cmf(df)
    if cmf is not None:
        if d > 0 and cmf > 0:
            boost += W_CMF; tags.append("CMF+")
        elif d < 0 and cmf < 0:
            boost += W_CMF; tags.append("CMF-")

    # 5) MACD histogram agrees
    mh = _read_macd_hist(df)
    if mh is not None:
        if d > 0 and mh > 0:
            boost += W_MACD; tags.append("MACD+")
        elif d < 0 and mh < 0:
            boost += W_MACD; tags.append("MACD-")

    return int(boost), ",".join(tags)


def indicator_snapshot(df: pd.DataFrame) -> dict:
    "Raw values for logging/debug (not direction-scored)."
    if df is None or len(df) < 5:
        return {}
    return {
        "rsi":  _read_rsi(df),
        "cmf":  _read_cmf(df),
        "macd_hist": _read_macd_hist(df),
        "supertrend_dir": _read_supertrend_dir(df),
        "ema20": _ema(df["close"], EMA_LEN),
        "engine": "pandas_ta" if _PTA else "fallback",
    }

