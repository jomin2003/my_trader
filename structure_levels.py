"""
STRUCTURE-AWARE SL/TARGET ENGINE

Places targets just INSIDE real support/resistance (so they fill) and
stops just BEYOND opposite structure (dodging noise). Fixes the
"target almost hits then reverses" problem of fixed-ATR targets.

Levels: swing pivots, opening range H/L, prev-day H/L/close, VWAP,
round numbers. Falls back to ATR if no clean structure. Enforces MIN_RR.
"""
from __future__ import annotations
import numpy as np
import pandas as pd

SWING_LOOKBACK = 3
LEVEL_CLUSTER  = 0.0015
BUFFER_ATR     = 0.15      # target this * ATR INSIDE the wall
SL_BUFFER_ATR  = 0.20      # SL this * ATR BEYOND structure
MIN_RR         = 1.2
MAX_RR         = 5.0
ROUND_STEP_PCT = 0.005


def _swings(df, k=SWING_LOOKBACK):
    highs, lows = [], []
    h = df["high"].values.astype(float); l = df["low"].values.astype(float)
    n = len(df)
    for i in range(k, n-k):
        if h[i] == max(h[i-k:i+k+1]): highs.append(h[i])
        if l[i] == min(l[i-k:i+k+1]): lows.append(l[i])
    return highs, lows


def _round_levels(price):
    step = max(round(price * ROUND_STEP_PCT), 1)
    base = round(price/step)*step
    return [base-step, base, base+step, base+2*step, base-2*step]


def _cluster(levels, tol):
    if not levels: return []
    levels = sorted(levels); out = [levels[0]]
    for lv in levels[1:]:
        if abs(lv-out[-1])/max(out[-1],1e-9) > tol: out.append(lv)
    return out


def build_levels(df, entry, prev_high=None, prev_low=None, prev_close=None,
                 or_high=None, or_low=None, vwap=None):
    sh, sl = _swings(df)
    levels = list(sh) + list(sl)
    for x in (prev_high, prev_low, prev_close, or_high, or_low, vwap):
        if x is not None and x > 0: levels.append(float(x))
    levels += _round_levels(entry)
    levels = [x for x in levels if x > 0]
    return _cluster(levels, LEVEL_CLUSTER)


def compute_structure_sl_target(df, entry, direction, atr,
                                 prev_high=None, prev_low=None, prev_close=None,
                                 or_high=None, or_low=None, vwap=None,
                                 max_risk_pct=0.015):
    if atr is None or atr <= 0:
        atr = entry * 0.005
    levels = build_levels(df, entry, prev_high, prev_low, prev_close,
                          or_high, or_low, vwap)
    tbuf = BUFFER_ATR * atr; sbuf = SL_BUFFER_ATR * atr
    meta = {"method": "structure", "levels_found": len(levels)}

    if direction > 0:
        resist = sorted([x for x in levels if x > entry + tbuf])
        supp   = sorted([x for x in levels if x < entry - sbuf], reverse=True)
        target = (resist[0]-tbuf) if resist else None
        sl     = (supp[0]-sbuf)   if supp   else None
    else:
        supp   = sorted([x for x in levels if x < entry - tbuf], reverse=True)
        resist = sorted([x for x in levels if x > entry + sbuf])
        target = (supp[0]+tbuf)   if supp   else None
        sl     = (resist[0]+sbuf) if resist else None

    if sl is None:
        sl = entry - direction*(1.5*atr); meta["sl_method"] = "atr_fallback"
    else:
        meta["sl_method"] = "structure"
    if target is None:
        risk = abs(entry-sl); target = entry + direction*2.0*risk
        meta["tgt_method"] = "rr_fallback"
    else:
        meta["tgt_method"] = "structure"

    risk = abs(entry-sl); reward = abs(target-entry)
    max_risk = max_risk_pct*entry
    if risk > max_risk:
        sl = entry - direction*max_risk; risk = max_risk; meta["sl_method"] += "+capped"
    if reward < MIN_RR*risk:
        target = entry + direction*MIN_RR*risk; meta["tgt_method"] += "+minrr"; reward = MIN_RR*risk
    if reward > MAX_RR*risk:
        target = entry + direction*MAX_RR*risk; meta["tgt_method"] += "+capped"

    meta["rr"] = round(reward/max(risk,1e-9), 2)
    return round(sl, 2), round(target, 2), meta
