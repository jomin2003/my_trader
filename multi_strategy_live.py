"""
MULTI-STRATEGY LIVE MODULE — OB Shorts + ORB + Gap-Fill (all 3).

Drop-in replacement for scanner's detect_patterns / score_signals.
Each signal is tagged with 'strategy' so Telegram/logs show which fired.

Requires precomputed data files:
  - ob_data.csv   (from precompute_order_blocks.py)  -> OB Shorts
  - gap_data.csv  (from precompute_gapfill.py)        -> Gap-Fill
  (ORB needs no precompute — opening range is built live.)

STRATEGY A — OB SHORTS (validated p<0.0001):
  shooting-star rejection at a bear order-block zone, vol>1.2×SMA,
  SHORT only, entry 10:00-14:00.

STRATEGY B — ORB (opening range breakout):
  fresh close beyond 09:15-09:30 range, vol>1.2×SMA, LONG+SHORT,
  entry 09:30-11:00, one trade/direction/day.

STRATEGY C — GAP-FILL (fade the gap):
  gap 1-3%, fresh close back through opposite OR edge on LOW volume
  (below vol-EMA20 = exhaustion), target = prev close, entry 09:30-11:30.
"""
from __future__ import annotations

import os
from pathlib import Path
from datetime import time as dtime
import pandas as pd
import intraday_pattern_scanner_v2 as scn

# ---------- shared scanner config ----------
scn.MIN_CANDLES_NEEDED   = 4
scn.REQUIRE_CONFIRMATION = False
scn.RISK_REWARD_RATIO    = 2.0   # blended default (see note in deploy guide)

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
            print(f"[MULTI] OB zones: {sum(len(v) for v in _OB.values())}")
        except Exception as e:
            print(f"[MULTI] OB load failed: {e}")
    else:
        print(f"[MULTI] ob_data.csv missing — OB Shorts disabled")
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
            print(f"[MULTI] Gap-days: {len(_GAP)}")
        except Exception as e:
            print(f"[MULTI] Gap load failed: {e}")
    else:
        print(f"[MULTI] gap_data.csv missing — Gap-Fill disabled")
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


def _day(df):
    return df[df["ts"].dt.date == df["ts"].iloc[-1].date()]


def _or(day):
    orb = day[day["ts"].dt.time < ORB_END]
    if len(orb) < 3: return None
    return float(orb["high"].max()), float(orb["low"].min())


# =====================================================================
# detect_patterns — runs all 3
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
        # B) ORB breakout (both dir)
        if _win(ts, ORB_ENTRY_START, ORB_ENTRY_END):
            if c > or_high and prev_c <= or_high:
                hits.append((+1, "ORB Long", 5))
            elif c < or_low and prev_c >= or_low:
                hits.append((-1, "ORB Short", 5))
        # C) GAP-FILL (fade): short if breaks below OR_low, long if above OR_high
        if _win(ts, GAP_ENTRY_START, GAP_ENTRY_END):
            if c < or_low and prev_c >= or_low:
                hits.append((-1, "GapFill Short", 5))
            elif c > or_high and prev_c <= or_high:
                hits.append((+1, "GapFill Long", 5))

    return hits


# =====================================================================
# score_signals — validate + tag each
# =====================================================================
def score_signals(symbol, security_id, df, hits):
    if not hits: return []

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
        atr_val = close_now * 0.005   # fallback early in day
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
            # short only on gap-up, long only on gap-down
            if direction < 0 and gap["dir"] != "UP": continue
            if direction > 0 and gap["dir"] != "DOWN": continue
            # LOW-volume exhaustion filter (vol below EMA20)
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

    return rows


def _row(sym, sid, pattern, side, direction, score, vr, px, hi, lo, atr, vwap, t, strat):
    return {
        "symbol": sym, "security_id": sid, "pattern": pattern,
        "signal": side, "direction": direction, "strength": score,
        "vol_ratio": round(vr, 2), "score": score, "price": round(px, 2),
        "pattern_high": round(hi, 2), "pattern_low": round(lo, 2),
        "atr": round(atr, 2), "vwap": round(vwap, 2) if vwap else None,
        "strategy": strat, "time": t,
    }


def clear_cache():
    reload_tables()
