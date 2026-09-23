"""
MULTI-STRATEGY LIVE MODULE — OB Shorts + ORB + Gap-Fill + Candle-Structure
+ Chart Patterns (flag / triangle / double-top-bottom / ABCD).

Drop-in replacement for scanner's detect_patterns / score_signals.
Each signal tagged with 'strategy'. Candle-Structure and Chart-Pattern
signals also carry 'struct_sl' + 'struct_target' so the scanner uses
pattern-native exits (stops/targets from the chart geometry -> entries,
stops and targets all come from the pattern, not a global ATR multiple).
ORB rows also carry pattern-native exits (SL at the opposite OR extreme,
target 1.5x OR width); VWAP rows get them from vwap_reclaim_strategy.

Requires:
  ob_data.csv    (precompute_order_blocks.py)  -> OB Shorts
  gap_data.csv   (precompute_gapfill.py)       -> Gap-Fill
  structure_levels.py                          -> Candle-Structure exits
  (ORB needs no precompute.)

STRATEGIES:
  A) OB SHORTS      - shooting star at bear OB zone, SHORT, 10:00-14:00
  B) ORB            - opening-range breakout, LONG+SHORT, 09:30-11:00
  C) GAPFILL/GAPGO  - fade small gaps / ride large gaps, 09:30-11:30
  D) CANDLE-STRUCT  - candlestick reversal + structure-based SL/target
                      (hammer/shooting/morning/evening), VWAP-aligned, 09:30-14:30
  E) VWAP_RECLAIM / VWAP_PULLBACK - institutional footprint, 09:30-14:30
  F) SUPER_TREND_MOMO - fresh Supertrend flip + ADX regime, 09:30-14:30
  G) MR_VWAP_FADE   - Connors-style VWAP mean reversion, 10:00-14:00
  H) PDHL_BREAK     - previous-day high/low breakout, 09:45-14:30
  I) CHART PATTERNS - flag / triangle / double-top-bottom / ABCD
                      (research top-6 geometry), 09:30-14:30

TIER-1 (NEW): indicators_ta confluence boost. Every signal's `score` is
nudged by how many extra indicators (RSI/Supertrend/EMA/CMF/MACD) agree
with its direction, and the agreeing tags are stored in `ta`. Safe no-op
if indicators_ta / pandas-ta is unavailable.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from datetime import time as dtime
import pandas as pd
import intraday_pattern_scanner_v2 as scn
from shared_indicators import in_time_window as _win

log = logging.getLogger("multi_strategy")

# ADX momentum filter: skip choppy/range-bound stocks
# Env-driven so Render tuned value (ADX_MIN_THRESHOLD=25) actually applies live.
# Falls back to scanner config then 20.
def _resolve_adx_threshold() -> int:
    try:
        import os
        v = os.getenv("ADX_MIN_THRESHOLD", "").strip()
        if v:
            return int(float(v))
    except Exception:
        pass
    try:
        import config_registry as _cfg
        v = _cfg.get("ADX_MIN_THRESHOLD", 20)
        return int(v)
    except Exception:
        return 20
ADX_FILTER_ENABLED = True
ADX_MIN_THRESHOLD  = _resolve_adx_threshold()

# Canonical strategy-name normalizer (side-block + allocator use it)
try:
    from config_registry import StrategyName
except Exception:                                  # pragma: no cover
    class StrategyName:                            # minimal fallback
        @staticmethod
        def normalize(name):
            return str(name or "").strip().upper()

# structural exits (targets inside S/R walls)
try:
    from structure_levels import compute_structure_sl_target
    _STRUCT_OK = True
except Exception as _e:
    _STRUCT_OK = False
    log.warning(f"structure_levels not available ({_e}) — Candle-Struct disabled")

# ---- Tier-1: indicators_ta confluence boost (safe no-op if missing) ----
try:
    import indicators_ta
    _ITA_OK = True
    log.info("[MULTI] indicators_ta confluence boost active")
except Exception as _e:
    _ITA_OK = False
    log.info(f"[MULTI] indicators_ta unavailable ({_e}) — confluence boost off")

# ---- NEW E) VWAP_RECLAIM strategy (research-backed, safe no-op if missing) ----
try:
    import vwap_reclaim_strategy as _VWAP
    _VWAP_OK = True
    log.info("[MULTI] VWAP_RECLAIM strategy active")
except Exception as _e:
    _VWAP_OK = False
    log.info(f"[MULTI] VWAP module unavailable ({_e}) — VWAP disabled")

# ---- I) CHART PATTERNS (research top-6: flag/triangle/double/ABCD) ----
# Deep-research-backed (2026-09-23) intraday chart-pattern engine: geometry
# detectors + candle-confirmation gate + pattern-native SL/target math
# (also used to upgrade ORB and VWAP exits to per-pattern levels).
try:
    import chart_patterns as _CP
    _CP_OK = True
    _CP_NAMES = _CP.PATTERN_NAMES
    log.info("[MULTI] chart_patterns engine active")
except Exception as _e:
    _CP_OK = False
    _CP_NAMES = frozenset()
    log.info(f"[MULTI] chart_patterns unavailable ({_e}) — pattern scan off")

import numpy as np

# ---- NEW G) MR_VWAP_FADE (Connors-style intraday mean reversion) ----
# Research: Connors & Alvarez, "Short Term Trading Strategies That Work" — RSI(2)/
# Double-7 buy panic dips in an uptrend, exit on strength (82.5% WR on SPY; see
# QuantifiedStrategies 2024 re-test). This repo's own research doc calls NIFTY50
# mega-caps the most mean-reverting names on the exchange — and every other
# strategy here is momentum. This is the missing class. Intraday adaptation:
# fade >=MR_FADE_SIGMA extensions from VWAP with RSI(2) confirmation; volume-spike
# veto because news-driven breaks continue instead of reverting.
MR_FADE_ENABLED   = bool(int(os.getenv("MR_FADE_ENABLED", "1")))
MR_FADE_SIGMA     = float(os.getenv("MR_FADE_SIGMA", "2.0"))
MR_FADE_RSI2_LOW  = float(os.getenv("MR_FADE_RSI2_LOW", "10"))
MR_FADE_RSI2_HIGH = float(os.getenv("MR_FADE_RSI2_HIGH", "90"))
MR_FADE_MAX_VOLR  = float(os.getenv("MR_FADE_MAX_VOLR", "1.3"))
MR_FADE_START     = (10, 0)   # (hour, minute) — matches _win()'s contract
MR_FADE_END       = (14, 30)
MR_FADE_MIN_BARS  = 30

# ---- H) PDHL_BREAK (previous-day high/low breakout) ----
# Research: tradingstats.net, 1,691 sessions of NQ futures (2015-2025) — 80.8% of
# sessions with an inside-range open break at least one previous-day level. Levels
# are seeded each morning by seed_today_gap.py into pdhl_data.csv.
PDHL_DATA_PATH    = os.getenv("PDHL_DATA_PATH", str(Path(__file__).parent / "pdhl_data.csv"))
PDHL_ENABLED      = bool(int(os.getenv("PDHL_ENABLED", "1")))
PDHL_BUFFER_PCT   = float(os.getenv("PDHL_BUFFER_PCT", "0.0005"))
PDHL_VOL_MULT     = float(os.getenv("PDHL_VOL_MULT", "1.2"))
PDHL_START        = (9, 45)
PDHL_END          = (14, 30)

_CUR_DF = None   # set per-call so _row() can score confluence on this symbol's bars

# ---------- shared scanner config ----------
# NOTE: the scanner module already resolves these from config_registry at
# import (env-overridable). Do NOT re-assign them here — hardcoding the
# defaults would silently clobber any env override.

# ---------- OB SHORTS params (PROFITABLE-ONLY v2) ----------
# Research: standalone OB 43% bounce = -EV. Only short with HTF downtrend +
# below-VWAP + Supertrend-red + FVG. Vol 1.5x, SL>=0.6% (cost/R<=0.3).
OB_DATA_PATH       = os.getenv("OB_DATA_PATH", str(Path(__file__).parent / "ob_data.csv"))
OB_TICK_SIZE       = 0.05
OB_MAX_SL_PCT      = 0.012
OB_MIN_SL_PCT      = 0.006
OB_VOL_MULT        = 1.5
OB_ENTRY_START     = (10, 0)
OB_ENTRY_END       = (14, 0)
PIN_WICK_RATIO     = 2.0
PIN_BODY_MAX_RANGE = 0.35
OB_REQUIRE_VWAP    = True   # short only below VWAP
OB_REQUIRE_FVG     = True   # FVG confluence required (64.8% mitigation edge)

# ---------- ORB params (PROFITABLE-ONLY v2) ----------
# 15-min 09:15-09:30 range, 5-min CLOSE-confirm +0.05% buffer, vol 1.5x,
# range 0.25-1.2% (ideal 0.4-0.8%), ADX>20, VWAP-align, SL opp end, 2R.
ORB_END            = dtime(9, 30)
ORB_ENTRY_START    = (9, 30)
ORB_ENTRY_END      = (11, 0)
ORB_VOL_MULT       = 1.5
ORB_BUFFER_PCT     = 0.0005  # 0.05% close-confirm buffer
ORB_MIN_RANGE_PCT  = 0.0025  # skip <0.25% no-expansion
ORB_MAX_RANGE_PCT  = 0.0120  # skip >1.2% extended
ORB_REQUIRE_VWAP   = True
ORB_REQUIRE_ADX    = True
# Accuracy gates (FTO/TradingView consensus): 20EMA slope + RSI momentum +
# candle-strength (close in outer 35% of bar). Kills weak mid-range closes.
ORB_REQUIRE_TREND  = True
ORB_REQUIRE_RSI    = True
ORB_REQUIRE_STRENGTH = True
FRIDAY_SKIP        = False  # Angel NSE study: skip Friday (weekend distortion). Enable after OOS check.
# Concretum 7k-stock study: take ORB only in the direction of the FIRST candle
# (bullish first 5-min bar -> longs only; bearish -> shorts only). Adds edge by
# trading with the opening order-flow imbalance, never against it.
ORB_FIRST_CANDLE_DIR = False

# ---------- GAP params (PROFITABLE-ONLY v2) ----------
# Research: fade ONLY small <0.6% no-news gaps (84% fill <0.5%, 71% 0.5-1%).
# Large >1% gaps are Gap-and-Go CONTINUATION (new strategy G), never fade.
GAP_DATA_PATH      = os.getenv("GAP_DATA_PATH", str(Path(__file__).parent / "gap_data.csv"))
GAP_MIN_PCT        = 0.25   # fade small gaps only
GAP_MAX_PCT        = 0.60   # >0.6% -> Gap-and-Go regime, do NOT fade
GAP_ENTRY_START    = (9, 30)
GAP_ENTRY_END      = (10, 30)  # fade decision in first hour (avg fill 45-78min)
GAP_SL_ATR_MULT    = 0.1
GAP_VOL_EMA_LEN    = 20
# Gap-and-Go continuation (NEW G): gap >1% + RVOL + trend-continuation
# Vortex 6.5k-gap study: gaps >2% do NOT extend (50.5% continuation, median
# +0.03%) — the tradeable band is 1-2%; continuation evidence peaks 09:45-10:30.
GAPGO_MIN_PCT      = 1.0
GAPGO_MAX_PCT      = 2.0
GAPGO_ENTRY_START  = (9, 45)
GAPGO_ENTRY_END    = (11, 0)
GAPGO_VOL_MULT     = 1.5
# Improvement: EMA trend filter — fade gap only when EMA50 > EMA200 (up trend)
# for gap-up fills, or EMA50 < EMA200 (down trend) for gap-down fills.
# Research: Dynamic Gap-Fill strategy adds EMA50/EMA200 trend alignment to
# improve win rate (FMZ: "Multiple Confirmation Mechanisms").
GAP_TREND_FILTER   = True
# Improvement: EMA20 intraday trend for entry bias (matches FMZ's EMA trend filter)
GAP_EMA_FAST       = 20
GAP_EMA_SLOW       = 200
# Improvement: RSI overbought/oversold filter for gap-fade quality
# Research (FMZ): RSI > 60 for shorts, RSI < 40 for longs adds confluence
GAP_RSI_FILTER     = True
GAP_RSI_OVERSOLD   = 40.0
GAP_RSI_OVERBOUGHT = 60.0

# ---------- CANDLE-STRUCTURE params (PROFITABLE-ONLY v2) ----------
# Research: bare engulfing = coin-flip intraday. Require 15m trend-align +
# S/R-VWAP confluence + vol 1.5x + close-confirm + TA>=2. Expect 55-60% only then.
CS_ENTRY_START     = (9, 30)
CS_ENTRY_END       = (14, 30)
CS_VOL_MULT        = 1.5
CS_MIN_BARS        = 20      # need history for swings/ATR
CS_MIN_TA          = 2       # min confluence tags (was 0)
CS_REQUIRE_TREND   = True    # 15m EMA trend-align required

# ---------- SUPER_TREND_MOMO params (NEW F) ----------
# (10,3) 15m + ADX>20 + 1H-align: 36->44% WR, DD halved, +11.4k net NIFTY fut.
ST_ENTRY_START     = (9, 30)
ST_ENTRY_END       = (14, 30)
ST_VOL_MULT        = 1.5
ST_LEN             = 10
ST_MULT            = 3.0

# ---------- lookup tables ----------
_OB = {}; _OB_LOADED = False
_GAP = {}; _GAP_LOADED = False
_TRADED = set()   # (symbol, date, strategy, direction) dedup

# ---------- side control ----------
# Set of (strategy, direction) pairs to suppress. Populated from env
# SIDE_BLOCK ("VWAP_RECLAIM:+1,SUPERTREND:-1") or by the allocator research.
SIDE_BLOCK: set[tuple[str, int]] = set()

def _load_side_block():
    raw = os.getenv("SIDE_BLOCK", "").strip()
    if not raw:
        return
    for part in raw.split(","):
        try:
            name, d = part.strip().rsplit(":", 1)
            SIDE_BLOCK.add((StrategyName.normalize(name), int(float(d))))
        except Exception:
            log.warning(f"SIDE_BLOCK: bad entry {part!r}")

_load_side_block()


def _load_ob():
    global _OB_LOADED
    if _OB_LOADED: return
    p = Path(OB_DATA_PATH)
    if p.exists():
        try:
            df = pd.read_csv(p); df["date"] = pd.to_datetime(df["date"]).dt.date
            for r in df.itertuples(index=False):
                _OB.setdefault((r.symbol.upper(), r.date), []).append(
                    {"type": r.ob_type, "time": r.ob_time,
                     "hi": float(r.ob_body_high), "lo": float(r.ob_body_low)})
            log.info(f"OB zones: {sum(len(v) for v in _OB.values())}")
        except Exception as e:
            log.warning(f"OB load failed: {e}")
    else:
        log.warning("ob_data.csv missing — OB Shorts disabled")
    _OB_LOADED = True


def _load_gap():
    global _GAP_LOADED
    if _GAP_LOADED: return
    p = Path(GAP_DATA_PATH)
    if p.exists():
        try:
            df = pd.read_csv(p); df["date"] = pd.to_datetime(df["date"]).dt.date
            for r in df.itertuples(index=False):
                _GAP[(r.symbol.upper(), r.date)] = {
                    "prev_close": float(r.prev_close), "gap_pct": float(r.gap_pct),
                    "dir": r.gap_dir, "daily_atr": float(r.daily_atr)}
            log.info(f"Gap-days: {len(_GAP)}")
        except Exception as e:
            log.warning(f"Gap load failed: {e}")
    else:
        log.warning("gap_data.csv missing — Gap-Fill disabled")
    _GAP_LOADED = True


def _get_ob(sym, day):
    if not _OB_LOADED: _load_ob()
    key = sym.upper()
    hit = _OB.get((key, day))
    if hit is not None:
        return hit
    # FIX: OB tables are precomputed post-market, so at any intraday moment the newest
    # available zone is YESTERDAY's. Keying strictly on today meant OB_RETEST could
    # never fire intraday (silent dead strategy). Retesting the most recent confirmed
    # zone strictly before today is the intended play — and introduces no look-ahead,
    # because those zones were confirmed with data available before today.
    # Fallback zones are copies marked "_past_day" (callers skip the formed-by-now
    # time filter for them — a yesterday zone is already confirmed).
    earlier = [d for (s, d) in _OB.keys() if s == key and d < day]
    if not earlier:
        return []
    return [dict(z, _past_day=True) for z in _OB.get((key, max(earlier)), [])]


def _get_gap(sym, day):
    if not _GAP_LOADED: _load_gap()
    # Keyed strictly on today: today's rows are written by the 09:20 IST
    # /trigger/seed-gap cron (seed_today_gap.py) from live first-bar data.
    # Yesterday's gap rows are a different trade — do NOT fall back to them here.
    return _GAP.get((sym.upper(), day))


# ---------- PDHL levels (previous-day high/low, seeded each morning) ----------
_PDHL: dict = {}
_PDHL_LOADED = False

def _load_pdhl():
    global _PDHL_LOADED
    if _PDHL_LOADED: return
    p = Path(PDHL_DATA_PATH)
    if p.exists():
        try:
            df = pd.read_csv(p); df["date"] = pd.to_datetime(df["date"]).dt.date
            for r in df.itertuples(index=False):
                _PDHL[(str(r.symbol).upper(), r.date)] = {
                    "pdh": float(r.pdh), "pdl": float(r.pdl),
                    "prev_close": float(r.prev_close),
                    "prev_range": float(r.prev_range),
                    "atr": float(r.atr) if getattr(r, "atr", 0) else None}
            log.info(f"PDHL levels: {len(_PDHL)}")
        except Exception as e:
            log.warning(f"PDHL load failed: {e}")
    else:
        log.warning("pdhl_data.csv missing — PDHL_BREAK disabled (run /trigger/seed-gap)")
    _PDHL_LOADED = True

def _get_pdhl(sym, day):
    if not _PDHL_LOADED: _load_pdhl()
    return _PDHL.get((sym.upper(), day))


def _rsi2(closes: pd.Series, period: int = 2) -> pd.Series:
    """Wilder RSI with a tiny period (Connors RSI-2)."""
    d = closes.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _vwap_bands(df: pd.DataFrame, win: int = 75):
    """Rolling session VWAP + mean-absolute-deviation sigma of (close − vwap).
    MAD-based sigma is robust to single-bar spikes that would inflate stddev."""
    c = df["close"].astype(float)
    v = df["volume"].astype(float)
    pv = (c * v).cumsum()
    vv = v.cumsum().replace(0, np.nan)
    vwap = pv / vv
    sigma = (c - vwap).abs().rolling(win, min_periods=10).mean()
    return vwap, sigma


def reload_tables():
    global _OB_LOADED, _GAP_LOADED, _PDHL_LOADED
    _OB.clear(); _GAP.clear(); _TRADED.clear(); _PDHL.clear()
    _OB_LOADED = False; _GAP_LOADED = False; _PDHL_LOADED = False
    _load_ob(); _load_gap(); _load_pdhl()


# ---------- helpers ----------
def _shooting_star(o, h, l, c):
    rng = max(h-l, 1e-9); body = abs(c-o); uw = h-max(o,c); lw = min(o,c)-l
    if body/rng > PIN_BODY_MAX_RANGE: return False
    if body > 0 and uw < PIN_WICK_RATIO*body: return False
    if uw <= lw: return False
    if (c-l)/rng > 0.5: return False
    return True


def _body(o, c): return abs(c - o)
def _rng(h, l):  return max(h - l, 1e-9)


def _candle_patterns(df):
    """Return list of (direction, name, strength) candlestick reversals."""
    if len(df) < 3:
        return []
    o0,h0,l0,c0 = (float(df[x].iloc[-3]) for x in ["open","high","low","close"])
    o1,h1,l1,c1 = (float(df[x].iloc[-2]) for x in ["open","high","low","close"])
    o2,h2,l2,c2 = (float(df[x].iloc[-1]) for x in ["open","high","low","close"])
    hits = []
    b1, b2 = _body(o1,c1), _body(o2,c2); r2 = _rng(h2,l2)
    uw = h2-max(o2,c2); lw = min(o2,c2)-l2
    # PRUNED (backtest 59d/10-sym: engulfs 65 trades, -Rs4.5k, PF 0.42-0.67):
    # Bullish/Bearish Engulfing removed. Keep Hammer/Shooting/Morning/Evening only.
    if b2>0 and lw>=2*b2 and uw<=0.3*b2 and b2/r2<=0.35:
        hits.append((+1, "Hammer", 4))
    if b2>0 and uw>=2*b2 and lw<=0.3*b2 and b2/r2<=0.35:
        hits.append((-1, "Shooting Star", 4))
    mid0=(o0+c0)/2
    if c0<o0 and _body(o0,c0)>0.6*_rng(h0,l0) and b1<0.4*_body(o0,c0) and c2>o2 and b2>0.6*r2 and c2>mid0:
        hits.append((+1, "Morning Star", 5))
    if c0>o0 and _body(o0,c0)>0.6*_rng(h0,l0) and b1<0.4*_body(o0,c0) and c2<o2 and b2>0.6*r2 and c2<mid0:
        hits.append((-1, "Evening Star", 5))
    return hits


def _day(df):
    return df[df["ts"].dt.date == df["ts"].iloc[-1].date()]


def _or(day):
    orb = day[day["ts"].dt.time < ORB_END]
    if len(orb) < 3: return None
    return float(orb["high"].max()), float(orb["low"].min())


# tag candle patterns so score_signals can route them (engulfs pruned)
_CANDLE_NAMES = {"Hammer","Shooting Star","Morning Star","Evening Star"}


# =====================================================================
# detect_patterns — runs all strategies A–I
# =====================================================================
def detect_patterns(df):
    if len(df) < scn.MIN_CANDLES_NEEDED or "ts" not in df.columns:
        return []
    ts = df["ts"].iloc[-1]
    o = float(df["open"].iloc[-1]); h = float(df["high"].iloc[-1])
    l = float(df["low"].iloc[-1]);  c = float(df["close"].iloc[-1])
    if c <= 0: return []
    prev_c = float(df["close"].iloc[-2])

    hits = []

    # A) OB SHORTS
    if _win(ts, OB_ENTRY_START, OB_ENTRY_END) and _shooting_star(o, h, l, c):
        hits.append((-1, "OB Bear Star", 4))

    # B) ORB with close-confirm buffer + range-quality gate
    day = _day(df); rng = _or(day)
    if rng is not None:
        or_high, or_low = rng
        or_width_pct = (or_high - or_low) / c if c > 0 else 0.0
        or_ok = ORB_MIN_RANGE_PCT <= or_width_pct <= ORB_MAX_RANGE_PCT
        # Concretum first-candle direction rule
        first_ok_long = first_ok_short = True
        if ORB_FIRST_CANDLE_DIR:
            first = day.iloc[0]
            first_ok_long = float(first["close"]) >= float(first["open"])
            first_ok_short = float(first["close"]) <= float(first["open"])
        if _win(ts, ORB_ENTRY_START, ORB_ENTRY_END) and or_ok:
            # close beyond range + 0.05% buffer (wick touches underperform)
            if (c > or_high * (1 + ORB_BUFFER_PCT) and prev_c <= or_high * (1 + ORB_BUFFER_PCT)
                    and first_ok_long):
                hits.append((+1, "ORB Long", 5))
            elif (c < or_low * (1 - ORB_BUFFER_PCT) and prev_c >= or_low * (1 - ORB_BUFFER_PCT)
                    and first_ok_short):
                hits.append((-1, "ORB Short", 5))
        # C) GAP-FADE small gaps only; GAP-GO large gaps continuation
        if _win(ts, GAP_ENTRY_START, GAP_ENTRY_END):
            if c < or_low and prev_c >= or_low:
                hits.append((-1, "GapFill Short", 5))
            elif c > or_high and prev_c <= or_high:
                hits.append((+1, "GapFill Long", 5))
        if _win(ts, GAPGO_ENTRY_START, GAPGO_ENTRY_END):
            if c > or_high and prev_c <= or_high:
                hits.append((+1, "GapGo Long", 5))
            elif c < or_low and prev_c >= or_low:
                hits.append((-1, "GapGo Short", 5))

    # D) CANDLE-STRUCTURE (entries across the session)
    if _STRUCT_OK and len(df) >= CS_MIN_BARS and _win(ts, CS_ENTRY_START, CS_ENTRY_END):
        hits.extend(_candle_patterns(df))

    # E) VWAP_RECLAIM + VWAP_PULLBACK (new: institutional footprint edge)
    if _VWAP_OK:
        try:
            hits.extend(_VWAP.detect(df))
        except Exception:
            pass

    # F) SUPER_TREND_MOMO (10,3) + ADX>20 regime — FRESH FLIP ONLY
    # (continuous-bar version overtraded 102/database; flip-only cuts ~70%)
    if _win(ts, ST_ENTRY_START, ST_ENTRY_END) and len(df) >= 32:
        try:
            import indicators_ta as _ita
            st_now = _ita._supertrend_dir(df, ST_LEN, ST_MULT)
            st_prev = _ita._supertrend_dir(df.iloc[:-1], ST_LEN, ST_MULT)
            if st_prev != st_now:  # fresh flip only
                if st_now == +1:
                    hits.append((+1, "SuperLong", 5))
                elif st_now == -1:
                    hits.append((-1, "SuperShort", 5))
        except Exception:
            pass

    # G) MR_VWAP_FADE (Connors-style mean reversion — the missing class here)
    if MR_FADE_ENABLED and _win(ts, MR_FADE_START, MR_FADE_END) and len(df) >= MR_FADE_MIN_BARS:
        try:
            _vw, _sg = _vwap_bands(df)
            vw = float(_vw.iloc[-1]); sg = float(_sg.iloc[-1])
            r2 = float(_rsi2(df["close"].astype(float)).iloc[-1])
            if vw > 0 and sg > 0 and np.isfinite(r2):
                if c <= vw - MR_FADE_SIGMA * sg and r2 <= MR_FADE_RSI2_LOW:
                    hits.append((+1, "VwapFade Long", 4))
                elif c >= vw + MR_FADE_SIGMA * sg and r2 >= MR_FADE_RSI2_HIGH:
                    hits.append((-1, "VwapFade Short", 4))
        except Exception:
            pass

    # H) CHART PATTERNS — research top-6: bull/bear flag, triangle,
    #    double top/bottom, ABCD (continuation + fade-at-D). Pure geometry +
    #    candle confirmation; per-pattern SL/target attached at scoring time.
    #    (ORB = rank 1 and VWAP = rank 3 keep their tuned detectors above and
    #    get pattern-native exits via chart_patterns.orb_exits/vwap_exits.)
    if _CP_OK:
        try:
            for _p in _CP.detect(df):
                hits.append((_p.direction, _p.name, _p.strength))
        except Exception:
            pass

    return hits


# =====================================================================
# score_signals — validate + tag each
# =====================================================================
def score_signals(symbol, security_id, df, hits):
    # Friday skip (Angel NSE: pre-weekend distortion). Off by default; enable after OOS check.
    if FRIDAY_SKIP:
        try:
            if df["ts"].iloc[-1].weekday() == 4:
                return []
        except Exception:
            pass
    # NOTE: no early `if not hits: return []` — PDHL hits are emitted below from
    # per-symbol level data, so the prelude must run first.
    # Tier-1: make this symbol's bars available to _row() for confluence
    global _CUR_DF
    _CUR_DF = df

    # ADX momentum filter — reject choppy stocks
    if ADX_FILTER_ENABLED and len(df) >= 28:
        adx_val = scn.adx(df, 14)
        if adx_val is not None and adx_val < ADX_MIN_THRESHOLD:
            return []

    close_now = float(df["close"].iloc[-1])
    high_now  = float(df["high"].iloc[-1])
    low_now   = float(df["low"].iloc[-1])
    vol_now   = float(df["volume"].iloc[-1])
    prev_vol  = df["volume"].iloc[:-1]
    avg_vol   = float(prev_vol.tail(20).mean()) if len(prev_vol) >= 5 else 0.0
    if avg_vol <= 0: return []
    if (close_now * avg_vol) / 1e5 < scn.MIN_TURNOVER_LAKHS: return []
    vol_ratio = vol_now / avg_vol
    atr_val = scn.wilder_atr(df, 14)
    if atr_val is None or atr_val <= 0:
        atr_val = close_now * 0.005
    vwap_val = scn.rolling_vwap(df)
    today = df["ts"].iloc[-1].date()
    now_t = df["ts"].iloc[-1].strftime("%H:%M")

    # PDHL breakout hits — emitted here (not in detect_patterns) because the levels
    # are per-symbol from pdhl_data.csv. Fresh-cross semantics: previous close inside
    # the prior range, current close beyond PDH/PDL with a small buffer.
    if PDHL_ENABLED and len(df) >= 12:
        ts_last = df["ts"].iloc[-1]
        if _win(ts_last, PDHL_START, PDHL_END):
            lv = _get_pdhl(symbol, today)
            if lv:
                prev_c = float(df["close"].iloc[-2])
                inside_open = lv["pdl"] <= prev_c <= lv["pdh"]  # gap-over-level days excluded
                if inside_open and vol_ratio >= PDHL_VOL_MULT:
                    if (close_now > lv["pdh"] * (1 + PDHL_BUFFER_PCT)
                            and prev_c <= lv["pdh"] * (1 + PDHL_BUFFER_PCT)):
                        hits.append((+1, "Pdhl Break Long", 5))
                    elif (close_now < lv["pdl"] * (1 - PDHL_BUFFER_PCT)
                            and prev_c >= lv["pdl"] * (1 - PDHL_BUFFER_PCT)):
                        hits.append((-1, "Pdhl Break Short", 5))

    if not hits: return []

    # Chart-pattern geometry is detected ONCE per symbol here; the per-row
    # branch below reuses this list instead of re-running the detectors.
    _cp_patterns = _CP.detect(df) if _CP_OK else []

    rows = []
    for signal, name, strength in hits:

        # ===== OB SHORTS (regime-conditional only) =====
        if name == "OB Bear Star":
            if vol_ratio < OB_VOL_MULT: continue
            # short only below VWAP (counter-VWAP OB = negative edge)
            if OB_REQUIRE_VWAP and vwap_val is not None and close_now >= vwap_val: continue
            matched = None
            for ob in _get_ob(symbol, today):
                if ob["type"] != "BEAR": continue
                # today's zone must already have formed by now; past-day zones are
                # confirmed by construction (marked "_past_day" by the fallback)
                if "_past_day" not in ob and ob["time"] >= now_t: continue
                if high_now >= ob["lo"] and high_now <= ob["hi"] + 0.5*atr_val:
                    matched = ob; break
            if matched is None: continue
            # FVG confluence: +2 score bonus (hard-gate killed all OB trades;
            # standalone OB 43% = -EV so reward FVG instead of requiring it)
            _ob_fvg = False
            if OB_REQUIRE_FVG:
                try:
                    tail = df.tail(6).reset_index(drop=True)
                    _ob_fvg = any(float(tail["high"].iloc[i]) < float(tail["low"].iloc[i-2])
                                  for i in range(2, len(tail)))
                except Exception:
                    _ob_fvg = False
            sl = matched["hi"] + OB_TICK_SIZE
            sl_pct = (sl-close_now)/close_now if close_now > 0 else 0.0
            # cost guard: SL>=0.6% (cost/R<=0.3), cap 1.2%
            if (sl-close_now) <= 0 or sl_pct < OB_MIN_SL_PCT or sl_pct > OB_MAX_SL_PCT: continue
            k = (symbol, today, "OB", -1)
            if k in _TRADED: continue
            _TRADED.add(k)
            rows.append(_row(symbol, security_id, "OB Bear Star (LIVE)", "SELL", -1,
                             strength+2+ (2 if _ob_fvg else 0), vol_ratio, close_now, high_now, low_now,
                             atr_val, vwap_val, now_t, "OB_SHORT"))

        # ===== ORB (filtered: VWAP + EMA-slope + RSI-momentum + strength) =====
        elif name in ("ORB Long", "ORB Short"):
            if vol_ratio < ORB_VOL_MULT: continue
            direction = 1 if name == "ORB Long" else -1
            if ORB_REQUIRE_VWAP and vwap_val is not None:
                if direction > 0 and close_now <= vwap_val: continue
                if direction < 0 and close_now >= vwap_val: continue
            # 20EMA slope must agree (flat MA = chop warning)
            if ORB_REQUIRE_TREND and len(df) >= 25:
                try:
                    e20 = df["close"].ewm(span=20, adjust=False).mean()
                    slope_ok = (float(e20.iloc[-1]) > float(e20.iloc[-4]) if direction > 0
                                else float(e20.iloc[-1]) < float(e20.iloc[-4]))
                    if not slope_ok: continue
                except Exception:
                    pass
            # RSI momentum: fresh break pushes through 55-60 (long) / under 45
            # (short); already >70 / <30 = chasing, take pullback instead.
            if ORB_REQUIRE_RSI and len(df) >= 16:
                try:
                    dlt = pd.Series(df["close"]).diff()
                    up = dlt.clip(lower=0).rolling(14).mean()
                    dn = (-dlt.clip(upper=0)).rolling(14).mean()
                    rs = up / dn.replace(0, float("nan"))
                    rsi_v = float((100 - 100 / (1 + rs)).iloc[-1])
                    if pd.notna(rsi_v):
                        if direction > 0 and not (55 <= rsi_v < 70): continue
                        if direction < 0 and not (30 < rsi_v <= 45): continue
                except Exception:
                    pass
            # Candle strength: close in outer 35% of bar range (conviction)
            if ORB_REQUIRE_STRENGTH:
                try:
                    rng = max(high_now - low_now, 1e-9)
                    pos = (close_now - low_now) / rng if direction > 0 else (high_now - close_now) / rng
                    if pos < 0.35: continue
                except Exception:
                    pass
            k = (symbol, today, "ORB", direction)
            if k in _TRADED: continue
            _TRADED.add(k)
            r = _row(symbol, security_id, f"{name} (LIVE)",
                     "BUY" if direction > 0 else "SELL", direction,
                     strength + 1, vol_ratio, close_now, high_now, low_now,
                     atr_val, vwap_val, now_t, "ORB")
            # Pattern-native exits (research rank-1 rule): SL at the opposite
            # OR extreme (+/- 0.1x ATR buffer), T1 target = 1.5x OR width.
            # The report's own geometry is ~1.3-1.45R, so the gate is 1.0R:
            # it only rejects chase breakouts. Falls back to the ATR profile
            # when the geometry can't fund even that.
            if _CP_OK:
                try:
                    _orng = _or(_day(df))
                    if _orng is not None:
                        _ex = _CP.orb_exits(_orng[0], _orng[1], close_now,
                                            direction, atr_val)
                        if _ex:
                            _sl, _tgt = _ex
                            r["struct_sl"] = _sl
                            r["struct_target"] = _tgt
                            r["rr"] = round(abs(_tgt - close_now) /
                                           max(abs(close_now - _sl), 1e-9), 2)
                            r["sl_method"] = "or_opposite_extreme_0.1atr"
                            r["tgt_method"] = "or_width_1.5x"
                except Exception:
                    pass
            rows.append(r)

        # ===== GAP-FILL (with EMA trend filter improvement) =====
        elif name in ("GapFill Long", "GapFill Short"):
            gap = _get_gap(symbol, today)
            if gap is None: continue
            if gap["gap_pct"] < GAP_MIN_PCT or gap["gap_pct"] > GAP_MAX_PCT: continue
            direction = 1 if name == "GapFill Long" else -1
            if direction < 0 and gap["dir"] != "UP": continue
            if direction > 0 and gap["dir"] != "DOWN": continue
            vol_ema = df["volume"].ewm(span=GAP_VOL_EMA_LEN, adjust=False).mean().iloc[-1]
            if vol_now >= vol_ema: continue
            # Improvement: EMA trend filter — only fade gaps WITH the intraday trend
            # Research (FMZ Dynamic Gap-Fill): EMA-fast > EMA-slow for longs, vice versa for shorts
            # FIX: old code required len(df)>=200 but df is today-only 5m (<75 bars),
            # so filter never fired. Use adaptive spans: EMA20/50 when <200 bars,
            # else EMA20/200.
            if GAP_TREND_FILTER and len(df) >= 30:
                _close = pd.Series(df["close"])
                _slow_span = GAP_EMA_SLOW if len(df) >= GAP_EMA_SLOW else 50
                ema_fast = float(_close.ewm(span=GAP_EMA_FAST, adjust=False).mean().iloc[-1])
                ema_slow = float(_close.ewm(span=_slow_span, adjust=False).mean().iloc[-1])
                if direction > 0 and ema_fast <= ema_slow: continue  # no uptrend → skip gap-down fade
                if direction < 0 and ema_fast >= ema_slow: continue  # no downtrend → skip gap-up fade
            # Improvement: RSI filter — only take gap-fade when RSI confirms mean-reversion setup
            if GAP_RSI_FILTER and len(df) >= 15:
                _delta = pd.Series(df["close"]).diff()
                _up = _delta.clip(lower=0).rolling(14).mean()
                _dn = (-_delta.clip(upper=0)).rolling(14).mean()
                _rs = _up / _dn.replace(0, float("nan"))
                _rsi_val = 100 - 100 / (1 + _rs)
                _rsi_val = float(_rsi_val.iloc[-1]) if pd.notna(_rsi_val.iloc[-1]) else None
                if _rsi_val is not None:
                    if direction < 0 and _rsi_val < GAP_RSI_OVERBOUGHT: continue  # short: RSI should be high (overbought)
                    if direction > 0 and _rsi_val > GAP_RSI_OVERSOLD: continue    # long: RSI should be low (oversold)
            # one gap trade/day TOTAL (fade XOR go — both same day = overtrade)
            if any((symbol, today, kk) in _TRADED for kk in ("GAP", "GAPGO")): continue
            k = (symbol, today, "GAP", direction)
            if k in _TRADED: continue
            _TRADED.add(k)
            rows.append(_row(symbol, security_id, f"{name} (LIVE)",
                             "BUY" if direction>0 else "SELL", direction,
                             strength+2, round(vol_now/max(vol_ema,1),2),
                             close_now, high_now, low_now,
                             gap["daily_atr"], vwap_val, now_t, "GAPFILL"))

        # ===== GAP-GO continuation (large gaps, never fade) =====
        elif name in ("GapGo Long", "GapGo Short"):
            gap = _get_gap(symbol, today)
            if gap is None: continue
            if gap["gap_pct"] < GAPGO_MIN_PCT or gap["gap_pct"] > GAPGO_MAX_PCT: continue
            direction = 1 if name == "GapGo Long" else -1
            # continuation WITH gap dir (fade would be -EV): UP gap -> long, DOWN -> short
            if direction > 0 and gap["dir"] != "UP": continue
            if direction < 0 and gap["dir"] != "DOWN": continue
            if vol_ratio < GAPGO_VOL_MULT: continue
            if ORB_REQUIRE_VWAP and vwap_val is not None:
                if direction > 0 and close_now <= vwap_val: continue
                if direction < 0 and close_now >= vwap_val: continue
            # one gap trade/day TOTAL (fade XOR go)
            if any((symbol, today, kk) in _TRADED for kk in ("GAP", "GAPGO")): continue
            k = (symbol, today, "GAPGO", direction)
            if k in _TRADED: continue
            _TRADED.add(k)
            rows.append(_row(symbol, security_id, f"{name} (LIVE)",
                             "BUY" if direction>0 else "SELL", direction,
                             strength+2, vol_ratio, close_now, high_now, low_now,
                             atr_val, vwap_val, now_t, "GAPGO"))

        # ===== CHART PATTERNS (flag/triangle/double-top-bottom/ABCD) =====
        # Research top-6 geometry detectors. Entries are confirmation-gated
        # twice: inside the detector (confirm_candle) and here on the live
        # bar (confirm_entry) — no paper/live trade without a confirming
        # candle. Rows carry pattern-native SL/target (struct_sl /
        # struct_target), which the scanner prefers over the ATR profile.
        elif name in _CP_NAMES and _CP_OK:
            _p = next((_x for _x in _cp_patterns
                       if _x.name == name and _x.direction == signal), None)
            if _p is None: continue
            if vol_ratio < _CP.CP_VOL_MULT: continue
            try:
                if not _CP.confirm_entry(df, signal): continue
            except Exception:
                pass
            k = (symbol, today, _p.strategy, signal)
            if k in _TRADED: continue
            _TRADED.add(k)
            r = _row(symbol, security_id, f"{name} (LIVE)",
                     "BUY" if signal > 0 else "SELL", signal,
                     strength + 1, vol_ratio, close_now, high_now, low_now,
                     atr_val, vwap_val, now_t, _p.strategy)
            r["struct_sl"] = _p.sl
            r["struct_target"] = _p.target
            r["rr"] = _p.rr
            r["sl_method"] = _p.sl_method
            r["tgt_method"] = _p.tgt_method
            rows.append(r)

        # ===== CANDLE-STRUCTURE (confluence-gated) =====
        elif name in _CANDLE_NAMES and _STRUCT_OK:
            if vol_ratio < CS_VOL_MULT: continue
            # VWAP alignment
            if vwap_val is not None:
                if signal > 0 and close_now <= vwap_val: continue
                if signal < 0 and close_now >= vwap_val: continue
            # 15m trend-align: EMA20 direction must agree (kills mid-chop flips)
            if CS_REQUIRE_TREND and len(df) >= 25:
                try:
                    ema20 = float(df["close"].ewm(span=20, adjust=False).mean().iloc[-1])
                    if signal > 0 and close_now < ema20: continue
                    if signal < 0 and close_now > ema20: continue
                except Exception:
                    pass
            # Prior-trend: reversal needs something to reverse (3-bar slope
            # into the pattern must oppose the signal direction)
            if len(df) >= 6:
                try:
                    c3 = float(df["close"].iloc[-4])
                    if signal > 0 and close_now <= c3: continue  # no preceding decline
                    if signal < 0 and close_now >= c3: continue  # no preceding rally
                except Exception:
                    pass
            # Close position: signal bar must close with conviction
            # (long upper 40%, short lower 40%)
            try:
                rng = max(high_now - low_now, 1e-9)
                if signal > 0 and (close_now - low_now) / rng < 0.40: continue
                if signal < 0 and (high_now - close_now) / rng < 0.40: continue
            except Exception:
                pass
            k = (symbol, today, "CANDLE", signal)
            if k in _TRADED: continue
            # opening range for structure inputs
            day = _day(df); rng = _or(day)
            or_high = rng[0] if rng else None
            or_low  = rng[1] if rng else None
            s_sl, s_tgt, meta = compute_structure_sl_target(
                df, close_now, signal, atr_val,
                or_high=or_high, or_low=or_low, vwap=vwap_val)
            _TRADED.add(k)
            r = _row(symbol, security_id, f"{name} (STRUCT)",
                     "BUY" if signal>0 else "SELL", signal,
                     strength+1, vol_ratio, close_now, high_now, low_now,
                     atr_val, vwap_val, now_t, "CANDLE_STRUCT")
            # TA>=2 gate: bare engulfing = coin-flip (require confluence)
            try:
                n_ta = len([t for t in str(r.get("ta", "")).split(",") if t.strip()])
            except Exception:
                n_ta = 0
            if n_ta < CS_MIN_TA: continue
            # attach structural exits — scanner will prefer these
            r["struct_sl"] = s_sl
            r["struct_target"] = s_tgt
            r["rr"] = meta["rr"]
            r["sl_method"] = meta["sl_method"]
            r["tgt_method"] = meta["tgt_method"]
            rows.append(r)

        # ===== SUPER_TREND_MOMO (NEW F: 10,3 + ADX + VWAP) =====
        elif name in ("SuperLong", "SuperShort"):
            if vol_ratio < ST_VOL_MULT: continue
            direction = 1 if name == "SuperLong" else -1
            if vwap_val is not None:
                if direction > 0 and close_now <= vwap_val: continue
                if direction < 0 and close_now >= vwap_val: continue
            k = (symbol, today, "ST", direction)
            if k in _TRADED: continue
            _TRADED.add(k)
            rows.append(_row(symbol, security_id, f"{name} (LIVE)",
                             "BUY" if direction>0 else "SELL", direction,
                             strength+2, vol_ratio, close_now, high_now, low_now,
                             atr_val, vwap_val, now_t, "SUPERTREND"))

        # ===== VWAP_RECLAIM / VWAP_PULLBACK (new) =====
        elif name in ("VWAP Reclaim Long", "VWAP Reclaim Short",
                      "VWAP Pullback Long", "VWAP Pullback Short") and _VWAP_OK:
            try:
                rows.extend(_VWAP.score(symbol, security_id, df, [(signal, name, strength)]))
            except Exception:
                pass

        # ===== MR_VWAP_FADE (Connors-style mean reversion; high WR, small RR) =====
        elif name in ("VwapFade Long", "VwapFade Short"):
            direction = 1 if "Long" in name else -1
            # volume-spike veto: news-driven breaks continue instead of reverting
            if vol_ratio > MR_FADE_MAX_VOLR: continue
            k = (symbol, today, "MRFADE", direction)
            if k in _TRADED: continue
            _TRADED.add(k)
            rows.append(_row(symbol, security_id, f"{name} (LIVE)",
                             "BUY" if direction > 0 else "SELL", direction,
                             strength, vol_ratio, close_now, high_now, low_now,
                             atr_val, vwap_val, now_t, "MR_VWAP_FADE"))

        # ===== PDHL_BREAK (previous-day high/low breakout) =====
        elif name in ("Pdhl Break Long", "Pdhl Break Short"):
            direction = 1 if "Long" in name else -1
            k = (symbol, today, "PDHL", direction)
            if k in _TRADED: continue
            _TRADED.add(k)
            rows.append(_row(symbol, security_id, f"{name} (LIVE)",
                             "BUY" if direction > 0 else "SELL", direction,
                             strength, vol_ratio, close_now, high_now, low_now,
                             atr_val, vwap_val, now_t, "PDHL_BREAK"))

    # FIX (profitability): ORB + GapFill fire on the SAME OR-break bar (same
    # direction). Old code returned both, wasting TOP_N slots and double-counting
    # signals. Keep only the highest-score row per direction per symbol-bar.
    if len(rows) > 1:
        best_by_dir = {}
        for r in rows:
            d = r.get("direction", 0)
            if d not in best_by_dir or r.get("score", 0) > best_by_dir[d].get("score", 0):
                best_by_dir[d] = r
        # If both directions present (rare: ORB long + Candle short same bar),
        # keep both; if same direction duplicated, keep best only.
        if len(best_by_dir) < len(rows):
            rows = list(best_by_dir.values())

    # Side control: block (strategy, direction) pairs proven to lose. Names are
    # normalized; direction +1 long / -1 short. Empty = trade both sides.
    if SIDE_BLOCK:
        rows = [r for r in rows
                if (StrategyName.normalize(r.get("strategy", "")), r.get("direction", 0))
                not in SIDE_BLOCK]

    return rows


def _row(sym, sid, pattern, side, direction, score, vr, px, hi, lo, atr, vwap, t, strat):
    # ---- Tier-1 confluence boost from indicators_ta (safe no-op if unavailable) ----
    ta_boost, ta_tags = 0, ""
    if _ITA_OK and _CUR_DF is not None:
        try:
            ta_boost, ta_tags = indicators_ta.confluence_score(_CUR_DF, direction)
        except Exception:
            ta_boost, ta_tags = 0, ""
    return {
        "symbol": sym, "security_id": sid, "pattern": pattern,
        "signal": side, "direction": direction, "strength": score,
        "vol_ratio": round(vr, 2), "score": score + ta_boost, "price": round(px, 2),
        "pattern_high": round(hi, 2), "pattern_low": round(lo, 2),
        "atr": round(atr, 2), "vwap": round(vwap, 2) if vwap else None,
        "strategy": strat, "ta": ta_tags, "time": t,
    }


def clear_cache():
    reload_tables()
