"""
MULTI-STRATEGY LIVE MODULE — OB Shorts + ORB + Gap-Fill + Candle-Structure.

Drop-in replacement for scanner's detect_patterns / score_signals.
Each signal tagged with 'strategy'. Candle-Structure signals also carry
'struct_sl' + 'struct_target' so the scanner uses structural exits
(targets placed just INSIDE real S/R walls -> they actually fill).

Requires:
  ob_data.csv    (precompute_order_blocks.py)  -> OB Shorts
  gap_data.csv   (precompute_gapfill.py)       -> Gap-Fill
  structure_levels.py                          -> Candle-Structure exits
  (ORB needs no precompute.)

STRATEGIES:
  A) OB SHORTS      - shooting star at bear OB zone, SHORT, 10:00-14:00
  B) ORB            - opening-range breakout, LONG+SHORT, 09:30-11:00
  C) GAP-FILL       - fade 1-3% gap on low volume, 09:30-11:30
  D) CANDLE-STRUCT  - candlestick reversal + structure-based SL/target
                      (engulfing/hammer/star), VWAP-aligned, 09:30-14:30

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

log = logging.getLogger("multi_strategy")

# ADX momentum filter: skip choppy/range-bound stocks
ADX_FILTER_ENABLED = True
ADX_MIN_THRESHOLD  = 20  # skip stocks with ADX below this (range-bound)

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
_CUR_DF = None   # set per-call so _row() can score confluence on this symbol's bars

# ---------- shared scanner config ----------
scn.MIN_CANDLES_NEEDED   = 4
scn.REQUIRE_CONFIRMATION = False
scn.RISK_REWARD_RATIO    = 2.0

# ---------- OB SHORTS params ----------
OB_DATA_PATH       = os.getenv("OB_DATA_PATH", str(Path(__file__).parent / "ob_data.csv"))
OB_TICK_SIZE       = 0.05
OB_MAX_SL_PCT      = 0.005
OB_VOL_MULT        = 1.2
OB_ENTRY_START     = (10, 0)
OB_ENTRY_END       = (14, 0)
PIN_WICK_RATIO     = 2.0
PIN_BODY_MAX_RANGE = 0.35

# ---------- ORB params ----------
ORB_END            = dtime(9, 30)
ORB_ENTRY_START    = (9, 30)
ORB_ENTRY_END      = (11, 0)
ORB_VOL_MULT       = 1.2

# ---------- GAP-FILL params ----------
GAP_DATA_PATH      = os.getenv("GAP_DATA_PATH", str(Path(__file__).parent / "gap_data.csv"))
GAP_MIN_PCT        = 1.0
GAP_MAX_PCT        = 3.0
GAP_ENTRY_START    = (9, 30)
GAP_ENTRY_END      = (11, 30)
GAP_SL_ATR_MULT    = 0.1
GAP_VOL_EMA_LEN    = 20

# ---------- CANDLE-STRUCTURE params ----------
CS_ENTRY_START     = (9, 30)
CS_ENTRY_END       = (14, 30)
CS_VOL_MULT        = 1.2
CS_MIN_BARS        = 20      # need history for swings/ATR

# ---------- lookup tables ----------
_OB = {}; _OB_LOADED = False
_GAP = {}; _GAP_LOADED = False
_TRADED = set()   # (symbol, date, strategy, direction) dedup


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
    return _OB.get((sym.upper(), day), [])


def _get_gap(sym, day):
    if not _GAP_LOADED: _load_gap()
    return _GAP.get((sym.upper(), day))


def reload_tables():
    global _OB_LOADED, _GAP_LOADED
    _OB.clear(); _GAP.clear(); _TRADED.clear()
    _OB_LOADED = False; _GAP_LOADED = False
    _load_ob(); _load_gap()


# ---------- helpers ----------
def _win(ts, s, e):
    t = ts.time() if hasattr(ts, "time") else ts
    cur = t.hour*60 + t.minute
    return s[0]*60+s[1] <= cur < e[0]*60+e[1]


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
    if c1<o1 and c2>o2 and o2<=c1 and c2>=o1 and b2>b1:
        hits.append((+1, "Bullish Engulfing", 5))
    if c1>o1 and c2<o2 and o2>=c1 and c2<=o1 and b2>b1:
        hits.append((-1, "Bearish Engulfing", 5))
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


# tag candle patterns so score_signals can route them
_CANDLE_NAMES = {"Bullish Engulfing","Bearish Engulfing","Hammer",
                 "Shooting Star","Morning Star","Evening Star"}


# =====================================================================
# detect_patterns — runs all 4 strategies
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

    # B & C) need opening range
    day = _day(df); rng = _or(day)
    if rng is not None:
        or_high, or_low = rng
        if _win(ts, ORB_ENTRY_START, ORB_ENTRY_END):
            if c > or_high and prev_c <= or_high:
                hits.append((+1, "ORB Long", 5))
            elif c < or_low and prev_c >= or_low:
                hits.append((-1, "ORB Short", 5))
        if _win(ts, GAP_ENTRY_START, GAP_ENTRY_END):
            if c < or_low and prev_c >= or_low:
                hits.append((-1, "GapFill Short", 5))
            elif c > or_high and prev_c <= or_high:
                hits.append((+1, "GapFill Long", 5))

    # D) CANDLE-STRUCTURE (entries across the session)
    if _STRUCT_OK and len(df) >= CS_MIN_BARS and _win(ts, CS_ENTRY_START, CS_ENTRY_END):
        hits.extend(_candle_patterns(df))

    return hits


# =====================================================================
# score_signals — validate + tag each
# =====================================================================
def score_signals(symbol, security_id, df, hits):
    if not hits: return []

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

    rows = []
    for signal, name, strength in hits:

        # ===== OB SHORTS =====
        if name == "OB Bear Star":
            if vol_ratio < OB_VOL_MULT: continue
            matched = None
            for ob in _get_ob(symbol, today):
                if ob["type"] != "BEAR" or ob["time"] >= now_t: continue
                if high_now >= ob["lo"] and high_now <= ob["hi"] + 0.5*atr_val:
                    matched = ob; break
            if matched is None: continue
            sl = matched["hi"] + OB_TICK_SIZE
            if (sl-close_now) <= 0 or (sl-close_now)/close_now > OB_MAX_SL_PCT: continue
            k = (symbol, today, "OB", -1)
            if k in _TRADED: continue
            _TRADED.add(k)
            rows.append(_row(symbol, security_id, "OB Bear Star (LIVE)", "SELL", -1,
                             strength+2, vol_ratio, close_now, high_now, low_now,
                             atr_val, vwap_val, now_t, "OB_SHORT"))

        # ===== ORB =====
        elif name in ("ORB Long", "ORB Short"):
            if vol_ratio < ORB_VOL_MULT: continue
            direction = 1 if name == "ORB Long" else -1
            k = (symbol, today, "ORB", direction)
            if k in _TRADED: continue
            _TRADED.add(k)
            rows.append(_row(symbol, security_id, f"{name} (LIVE)",
                             "BUY" if direction>0 else "SELL", direction,
                             strength+1, vol_ratio, close_now, high_now, low_now,
                             atr_val, vwap_val, now_t, "ORB"))

        # ===== GAP-FILL =====
        elif name in ("GapFill Long", "GapFill Short"):
            gap = _get_gap(symbol, today)
            if gap is None: continue
            if gap["gap_pct"] < GAP_MIN_PCT or gap["gap_pct"] > GAP_MAX_PCT: continue
            direction = 1 if name == "GapFill Long" else -1
            if direction < 0 and gap["dir"] != "UP": continue
            if direction > 0 and gap["dir"] != "DOWN": continue
            vol_ema = df["volume"].ewm(span=GAP_VOL_EMA_LEN, adjust=False).mean().iloc[-1]
            if vol_now >= vol_ema: continue
            k = (symbol, today, "GAP", direction)
            if k in _TRADED: continue
            _TRADED.add(k)
            rows.append(_row(symbol, security_id, f"{name} (LIVE)",
                             "BUY" if direction>0 else "SELL", direction,
                             strength+2, round(vol_now/max(vol_ema,1),2),
                             close_now, high_now, low_now,
                             gap["daily_atr"], vwap_val, now_t, "GAPFILL"))

        # ===== CANDLE-STRUCTURE =====
        elif name in _CANDLE_NAMES and _STRUCT_OK:
            if vol_ratio < CS_VOL_MULT: continue
            # VWAP alignment
            if vwap_val is not None:
                if signal > 0 and close_now <= vwap_val: continue
                if signal < 0 and close_now >= vwap_val: continue
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
            # attach structural exits — scanner will prefer these
            r["struct_sl"] = s_sl
            r["struct_target"] = s_tgt
            r["rr"] = meta["rr"]
            r["sl_method"] = meta["sl_method"]
            r["tgt_method"] = meta["tgt_method"]
            rows.append(r)

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
