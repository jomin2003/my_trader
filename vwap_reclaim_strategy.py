"""
vwap_reclaim_strategy.py — NEW STRATEGY E) VWAP_RECLAIM + FVG confluence.
========================================================================
WHY THIS EXISTS (research-backed):
  * VWAP is the single most robust intraday edge on liquid NSE names
    (RELIANCE-type liquidity: VWAP = institutional footprint). Your bot uses
    VWAP only as a FILTER; this promotes it to a STRATEGY.
  * MarketNetra NSE calibration: VWAP pullback + squeeze breakout wins
    58-63% at ~1:1; VWAP reclaim + RSI-divergence is highest-conviction.
  * arshadakl VWAP+RSI bot: 6-filter stack (close-confirm + VWAP cross +
    consolidation-skip + 1.5x vol + RSI 40-70 + pivot confluence) lifted WR
    36% -> 70%, -60% signals, -80% false positives.
  * FXNX 1000-trade SMC audit: FVG mitigation 64.8% (high edge) vs standalone
    OB bounce 43.1% (negative edge). So entries REQUIRE FVG or VWAP confluence,
    never blind OB limits.

WHAT IT TRADES (E1 + E2, long+short, 09:30-14:30):
  E1 VWAP_RECLAIM: close crosses back over session VWAP after >=30min on the
     other side, with volume >=1.3x avg20 and RSI in neutral-confirm zone
     (long: 45<=RSI<70, short: 30<RSI<=55). Needs >=20 bars for VWAP stability.
  E2 VWAP_PULLBACK (trend continuation): price already on the right side of
     VWAP for >=10 bars, pulls back to within 0.10% of VWAP, then prints a
     bullish/bearish confirmation bar (close back in trend direction) on
     rising volume. Skips 11:00-13:00 dead zone unless vol>=1.8x (institutional
     footprint only).

EXITS (wired via strategy_exits profiles below — add these rows):
  VWAP_RECLAIM sl 2.0xATR rr 1.5 trail 1.1  (balanced: reclaim can re-whipsaw)
  VWAP_PULLBACK sl 2.0xATR rr 1.5 trail 1.0 (trend-continuation: tight stop, run)

INTEGRATION (3 lines in multi_strategy_live.py):
  1. import vwap_reclaim_strategy as _VWAP
  2. in detect_patterns(): hits.extend(_VWAP.detect(df))
  3. in score_signals(): rows.extend(_VWAP.score(symbol, security_id, df, hits))
  Or run standalone: from vwap_reclaim_strategy import detect, score.

COST NOTE (₹1L capital, Rs500 risk):
  VWAP names are liquid => 3bps slip realistic. Roundtrip 18bps = 36-60% of a
  0.3-0.5% SL. Live exit profile (strategy_exits.py): sl 2.0xATR, rr 1.5.
  Backtest gate: PF>=1.3, expR>0.15.
"""
from __future__ import annotations
import logging
from datetime import time as dtime
import pandas as pd

from shared_indicators import in_time_window as _win

log = logging.getLogger("vwap_reclaim")

ENTRY_START = (9, 30)
ENTRY_END = (14, 30)
DEAD_START = (11, 0)
DEAD_END = (13, 0)
VOL_MULT = 1.5
DEAD_VOL_MULT = 1.8
MIN_BARS = 20
TREND_BARS = 10
PULLBACK_PCT = 0.0010  # within 0.10% of VWAP
RSI_LONG_LO, RSI_LONG_HI = 45.0, 70.0
RSI_SHORT_LO, RSI_SHORT_HI = 30.0, 55.0

_TRADED = set()  # (symbol, date, "VWAP", direction)


def _in_dead(ts):
    return _win(ts, DEAD_START, DEAD_END)


def _vwap(df) -> float | None:
    try:
        import intraday_pattern_scanner_v2 as scn
        v = scn.rolling_vwap(df)
        return float(v) if v else None
    except Exception:
        pass
    # fallback: session cumulative typical*vol
    try:
        day = df[df["ts"].dt.date == df["ts"].iloc[-1].date()]
        tp = (day["high"] + day["low"] + day["close"]) / 3.0
        cum_pv = (tp * day["volume"]).cumsum().iloc[-1]
        cum_v = day["volume"].cumsum().iloc[-1]
        return float(cum_pv / cum_v) if cum_v > 0 else None
    except Exception:
        return None


def _rsi(close: pd.Series, length: int = 14) -> float | None:
    if len(close) < length + 1:
        return None
    import numpy as np
    delta = close.diff()
    up = delta.clip(lower=0).rolling(length).mean()
    dn = (-delta.clip(upper=0)).rolling(length).mean()
    rs = up / dn.replace(0, float("nan"))
    rsi = 100 - 100 / (1 + rs)
    v = rsi.iloc[-1]
    return float(v) if pd.notna(v) else None


def _atr(df, length: int = 14) -> float | None:
    try:
        import intraday_pattern_scanner_v2 as scn
        return scn.wilder_atr(df, length)
    except Exception:
        return None


def detect(df) -> list:
    """Return hits: [(direction, name, strength)]. Pure pattern, no volume/indicator."""
    if len(df) < MIN_BARS or "ts" not in df.columns:
        return []
    ts = df["ts"].iloc[-1]
    if not _win(ts, ENTRY_START, ENTRY_END):
        return []
    try:
        vwap = _vwap(df)
    except Exception:
        return []
    if vwap is None:
        return []
    c = float(df["close"].iloc[-1])
    prev_c = float(df["close"].iloc[-2])
    # Need history on one side for reclaim
    closes = df["close"].tail(7)
    hits = []
    # E1: VWAP reclaim (cross back over)
    if prev_c <= vwap and c > vwap:
        # was below for a while? at least 4 of last 6 below
        below = sum(1 for x in closes.iloc[:-1] if float(x) <= vwap)
        if below >= 4:
            hits.append((+1, "VWAP Reclaim Long", 5))
    elif prev_c >= vwap and c < vwap:
        above = sum(1 for x in closes.iloc[:-1] if float(x) >= vwap)
        if above >= 4:
            hits.append((-1, "VWAP Reclaim Short", 5))
    # E2: VWAP pullback (trend + touch + bounce)
    if not hits and len(df) >= MIN_BARS + TREND_BARS:
        side_ok_long = all(float(x) > vwap for x in df["close"].iloc[-TREND_BARS - 1:-1])
        side_ok_short = all(float(x) < vwap for x in df["close"].iloc[-TREND_BARS - 1:-1])
        dist = abs(c - vwap) / vwap if vwap > 0 else 1.0
        o = float(df["open"].iloc[-1])
        if side_ok_long and dist <= PULLBACK_PCT and c > o:
            hits.append((+1, "VWAP Pullback Long", 5))
        elif side_ok_short and dist <= PULLBACK_PCT and c < o:
            hits.append((-1, "VWAP Pullback Short", 5))
    return hits


def _fvg_bonus(df, direction: int) -> int:
    """+1 if a fresh 3-bar FVG exists in trade direction within last 5 bars.
    Bullish FVG: low[i] > high[i-2]. Bearish: high[i] < low[i-2]."""
    try:
        if len(df) < 8:
            return 0
        tail = df.tail(6).reset_index(drop=True)
        for i in range(2, len(tail)):
            if direction > 0:
                if float(tail["low"].iloc[i]) > float(tail["high"].iloc[i - 2]):
                    return 1
            else:
                if float(tail["high"].iloc[i]) < float(tail["low"].iloc[i - 2]):
                    return 1
        return 0
    except Exception:
        return 0


def score(symbol, security_id, df, hits):
    """Validate hits -> signal rows (same schema as multi_strategy_live._row)."""
    if not hits:
        return []
    try:
        import intraday_pattern_scanner_v2 as scn
        import indicators_ta as ita
        _ITA = True
    except Exception:
        scn = None
        _ITA = False
    # Pattern-native exits (research rank-3 rule); safe no-op if unavailable.
    try:
        import chart_patterns as _CP
    except Exception:
        _CP = None
    close_now = float(df["close"].iloc[-1])
    vol_now = float(df["volume"].iloc[-1])
    prev_vol = df["volume"].iloc[:-1]
    avg_vol = float(prev_vol.tail(20).mean()) if len(prev_vol) >= 5 else 0.0
    if avg_vol <= 0:
        return []
    min_turnover = float(getattr(scn, "MIN_TURNOVER_LAKHS", 25)) if scn else 25.0
    if (close_now * avg_vol) / 1e5 < min_turnover:
        return []
    vol_ratio = vol_now / avg_vol
    atr_val = _atr(df, 14)
    if atr_val is None or atr_val <= 0:
        atr_val = close_now * 0.005
    vwap_val = _vwap(df)
    if vwap_val is None:
        return []
    today = df["ts"].iloc[-1].date()
    now_t = df["ts"].iloc[-1].strftime("%H:%M")
    ts = df["ts"].iloc[-1]
    # Flat-VWAP skip: slope <0.03%/30min on 5m = chop (11:00-13:30 usually)
    try:
        from shared_indicators import vwap_slope_pct
        _slope = vwap_slope_pct(df, lookback=6)
        if _slope is not None and abs(_slope) * 6 < 0.03:
            return []
    except Exception:
        pass
    # Dead-zone throttle: 11:00-13:00 needs institutional volume
    need_vol = DEAD_VOL_MULT if _in_dead(ts) else VOL_MULT
    rsi_val = _rsi(df["close"].astype(float), 14)

    rows = []
    for signal, name, strength in hits:
        if "VWAP" not in name:
            continue
        if vol_ratio < need_vol:
            continue
        # RSI confirm (neutral zone, not exhausted)
        if rsi_val is not None:
            if signal > 0 and not (RSI_LONG_LO <= rsi_val < RSI_LONG_HI):
                continue
            if signal < 0 and not (RSI_SHORT_LO < rsi_val <= RSI_SHORT_HI):
                continue
        # VWAP side sanity (reclaim already crossed; pullback must still hold side)
        if "Pullback" in name:
            if signal > 0 and close_now <= vwap_val:
                continue
            if signal < 0 and close_now >= vwap_val:
                continue
        k = (symbol, today, "VWAP", signal)
        if k in _TRADED:
            continue
        _TRADED.add(k)
        # confluence boost (same engine as main bot)
        ta_boost, ta_tags = (0, "")
        if _ITA:
            try:
                ta_boost, ta_tags = ita.confluence_score(df, signal)
            except Exception:
                ta_boost, ta_tags = (0, "")
        fvg = _fvg_bonus(df, signal)
        strat_tag = "VWAP_RECLAIM" if "Reclaim" in name else "VWAP_PULLBACK"
        row = {
            "symbol": symbol, "security_id": security_id,
            "pattern": f"{name} (LIVE)", "signal": "BUY" if signal > 0 else "SELL",
            "direction": signal, "strength": strength,
            "vol_ratio": round(vol_ratio, 2), "score": strength + ta_boost + fvg,
            "price": round(close_now, 2),
            "pattern_high": round(float(df["high"].iloc[-1]), 2),
            "pattern_low": round(float(df["low"].iloc[-1]), 2),
            "atr": round(float(atr_val), 2),
            "vwap": round(float(vwap_val), 2),
            "strategy": strat_tag,
            "ta": (ta_tags + (",FVG+" if fvg else "")),
            "time": now_t,
        }
        # Pattern-native exits (research): pullback -> SL below/above the
        # reversal candle (+/- 0.1x ATR); reclaim -> SL beyond VWAP
        # (+/- 0.15x ATR); target -> prior swing extreme, 1.5R floor.
        # Skipped when the geometry can't fund 1.5R (ATR profile fallback).
        if _CP is not None:
            try:
                _lb = df.iloc[-1]
                if "Pullback" in name:
                    _stop = (float(_lb["low"]) - 0.1 * atr_val if signal > 0
                             else float(_lb["high"]) + 0.1 * atr_val)
                    _sm = "rev_candle_0.1atr"
                else:
                    _stop = (vwap_val - 0.15 * atr_val if signal > 0
                             else vwap_val + 0.15 * atr_val)
                    _sm = "vwap_0.15atr"
                _hist = df.iloc[:-1]
                _swing = (float(_hist["high"].tail(20).max()) if signal > 0
                          else float(_hist["low"].tail(20).min()))
                _ex = _CP.vwap_exits(close_now, signal, atr_val, _stop, _swing)
                if _ex:
                    row["struct_sl"], row["struct_target"] = _ex
                    row["rr"] = round(abs(_ex[1] - close_now) /
                                      max(abs(close_now - _ex[0]), 1e-9), 2)
                    row["sl_method"] = _sm
                    row["tgt_method"] = "prior_swing_1.5r_floor"
            except Exception:
                pass
        rows.append(row)
    return rows


def clear():
    _TRADED.clear()


if __name__ == "__main__":
    print("VWAP_RECLAIM strategy module OK. Wire via multi_strategy_live integration notes in docstring.")
