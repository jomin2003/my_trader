"""OB Shorts LIVE strategy module - paper trading validated.

Config from Colab backtest 2026-07-22:
  - 50 trades, 52% WR, Exp_R +0.271, PF 1.34, RR 1:4
  - Bootstrap validated (percentile 48.9 = real, not lucky)
  - Monte Carlo permutation test: p < 0.0001 on Exp_R, PnL, PF
  - Sample size 50 below 100-trade robustness threshold
  - PAPER TRADE ONLY, no real money for 2 weeks minimum
"""
from __future__ import annotations
import os
from pathlib import Path
import pandas as pd
import intraday_pattern_scanner_v2 as scn


OB_DATA_PATH = os.getenv("OB_DATA_PATH",
    str(Path(__file__).parent / "ob_data.csv"))

# WINNING CONFIG from Colab (2026-07-22)
scn.RISK_REWARD_RATIO    = 4.0
scn.MIN_SCORE_TO_TRADE   = 6
scn.MIN_CANDLES_NEEDED   = 5
scn.REQUIRE_CONFIRMATION = False

# OB filter thresholds
TICK_SIZE          = 0.05
MAX_SL_PCT_OF_PX   = 0.005
VOL_CONFIRM_MULT   = 1.2
ENTRY_START_HM     = (10, 0)
ENTRY_END_HM       = (14, 0)
PIN_WICK_RATIO     = 2.0
PIN_BODY_MAX_RANGE = 0.35

_OB_TABLE = {}
_OB_LOADED = False
_TRADED_TODAY = set()


def _load_ob_table():
    global _OB_LOADED
    if _OB_LOADED: return
    path = Path(OB_DATA_PATH)
    if not path.exists():
        print(f"[OB LIVE] ob_data.csv missing at {path}")
        print(f"[OB LIVE] Run precompute_order_blocks.py daily after market close")
        _OB_LOADED = True; return
    try:
        df = pd.read_csv(path)
        df["date"] = pd.to_datetime(df["date"]).dt.date
        for row in df.itertuples(index=False):
            key = (row.symbol.upper(), row.date)
            _OB_TABLE.setdefault(key, []).append({
                "type": row.ob_type, "time": row.ob_time,
                "hi": float(row.ob_body_high), "lo": float(row.ob_body_low),
                "full_hi": float(row.ob_high), "full_lo": float(row.ob_low),
            })
        total = sum(len(v) for v in _OB_TABLE.values())
        print(f"[OB LIVE] Loaded {total} OBs from {path.name}")
    except Exception as e:
        print(f"[OB LIVE] Failed to load: {e}")
    _OB_LOADED = True


def _get_obs(symbol, day):
    if not _OB_LOADED: _load_ob_table()
    return _OB_TABLE.get((symbol.upper(), day), [])


def reload_ob_table():
    global _OB_LOADED
    _OB_TABLE.clear(); _TRADED_TODAY.clear(); _OB_LOADED = False
    _load_ob_table()


def _is_shooting_star(o, h, l, c):
    rng = max(h - l, 1e-9); body = abs(c - o)
    lower_wick = min(o, c) - l; upper_wick = h - max(o, c)
    if body / rng > PIN_BODY_MAX_RANGE: return False
    if body > 0 and upper_wick < PIN_WICK_RATIO * body: return False
    if upper_wick <= lower_wick: return False
    if (c - l) / rng > 0.5: return False
    return True


def _in_entry_window(ts):
    try:
        t = ts.time() if hasattr(ts, "time") else ts
        cur = t.hour * 60 + t.minute
        start = ENTRY_START_HM[0] * 60 + ENTRY_START_HM[1]
        end   = ENTRY_END_HM[0] * 60 + ENTRY_END_HM[1]
        return start <= cur < end
    except: return True


def detect_patterns(df):
    """SHORTS ONLY - only detect bearish shooting stars for OB fade."""
    if len(df) < scn.MIN_CANDLES_NEEDED: return []
    if "ts" not in df.columns: return []
    if not _in_entry_window(df["ts"].iloc[-1]): return []
    o = float(df["open"].iloc[-1]); h = float(df["high"].iloc[-1])
    l = float(df["low"].iloc[-1]); c = float(df["close"].iloc[-1])
    if c <= 0: return []
    if _is_shooting_star(o, h, l, c):
        return [(-1, "OB Bear Star (LIVE)", 4)]
    return []


def score_signals(symbol, security_id, df, hits):
    if not hits: return []
    close_now = float(df["close"].iloc[-1])
    high_now = float(df["high"].iloc[-1])
    low_now = float(df["low"].iloc[-1])
    vol_now = float(df["volume"].iloc[-1])
    prev_vol = df["volume"].iloc[:-1]
    avg_vol = float(prev_vol.tail(20).mean()) if len(prev_vol) >= 5 else 0.0
    if avg_vol <= 0: return []
    if (close_now * avg_vol) / 1e5 < scn.MIN_TURNOVER_LAKHS: return []
    vol_ratio = vol_now / avg_vol
    if vol_ratio < VOL_CONFIRM_MULT: return []
    today = df["ts"].iloc[-1].date()
    obs = _get_obs(symbol, today)
    if not obs: return []
    atr_val = scn.wilder_atr(df, 14); vwap_val = scn.rolling_vwap(df)
    if atr_val is None or vwap_val is None: return []
    now_time = df["ts"].iloc[-1].strftime("%H:%M")
    rows = []
    for signal, name, strength in hits:
        for ob in obs:
            if ob["time"] >= now_time: continue
            if ob["type"] != "BEAR": continue
            if not (high_now >= ob["lo"] and high_now <= ob["hi"] + 0.5 * atr_val): continue
            sl_price = ob["hi"] + TICK_SIZE
            sl_dist = sl_price - close_now
            if sl_dist <= 0 or sl_dist / close_now > MAX_SL_PCT_OF_PX: continue
            key = (symbol, today, ob["type"], ob["time"])
            if key in _TRADED_TODAY: continue
            _TRADED_TODAY.add(key)
            rows.append({
                "symbol": symbol, "security_id": security_id,
                "pattern": name, "signal": "SELL", "direction": -1,
                "strength": strength, "vol_ratio": round(vol_ratio, 2),
                "score": strength + 2, "price": round(close_now, 2),
                "pattern_high": round(high_now, 2), "pattern_low": round(low_now, 2),
                "atr": round(atr_val, 2), "vwap": round(vwap_val, 2),
                "ob_hi": round(ob["hi"], 2), "ob_lo": round(ob["lo"], 2),
                "ob_time": ob["time"], "ob_native_sl": round(sl_price, 2),
                "time": now_time,
            })
            break
    return rows


def clear_ob_cache(): reload_ob_table()
