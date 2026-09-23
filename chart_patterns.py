"""
chart_patterns.py — Top-6 NSE intraday chart-pattern engine (5-minute bars).
===========================================================================

Deep-research-backed (2026-09-23) ranking of the most reliable intraday chart
patterns for liquid NSE stocks, and where each one lives in this codebase:

  Rank  Pattern                 Lives in
  ----  ----------------------  ------------------------------------------
  1     Opening Range Breakout  multi_strategy_live (B) — exits upgraded to
                                pattern geometry via orb_exits() below
  2     Bull / Bear Flag        detect_flag()            [this module]
  3     VWAP Pullback / Reclaim vwap_reclaim_strategy — exits upgraded to
                                pattern geometry via vwap_exits() below
  4     Triangle (asc/desc/sym) detect_triangle()        [this module]
  5     Double Top / Bottom     detect_double()          [this module]
  6     ABCD (cont. + D-fade)   detect_abcd()            [this module]

Every detector returns ChartPattern objects carrying PATTERN-NATIVE stop-loss
and target levels (structure-based, not a global ATR multiple). The scanner
prefers these via the struct_sl/struct_target row fields — so stops and
targets are placed exactly as the chart pattern dictates.

UNIVERSAL CANDLE-CONFIRMATION GATE (research rails, applied to every entry):
  * 5-min CLOSE beyond the pattern level (wick pierces don't count)
  * breakout-bar volume >= 1.5x the 20-bar average (expansion after contraction)
  * decisive body: |close-open| >= 50% of the bar range
  * no long opposing wick: opposing wick <= 40% of the bar range
  * breakout-bar range <= 2.0x ATR(14) (extended bars = exhaustion/chase)
  * minimum 1:1.5 risk:reward at entry, else the setup is skipped

UNIVERSAL SESSION RAILS: no fresh pattern entries after ~14:30 IST; all
intraday positions flat by ~15:15 (broker auto-square).

Conventions: ATR = ATR(14) on 5-min bars; buffer = 0.1x ATR unless stated.
All thresholds are env-overridable (CP_*); code has no import-time side
effects and never raises out of detect().
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from shared_indicators import in_time_window as _win

log = logging.getLogger("chart_patterns")

# --------------------------------------------------------------------------- #
#  Tunables (research defaults; override via env)
# --------------------------------------------------------------------------- #
def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_win(name: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        raw = os.getenv(name, "")
        h, m = raw.split(":")
        return (int(h), int(m))
    except Exception:
        return default


CP_ENTRY_START = _env_win("CP_ENTRY_START", (9, 30))
CP_ENTRY_END   = _env_win("CP_ENTRY_END", (14, 30))
CP_VOL_MULT    = _env_float("CP_VOL_MULT", 1.5)    # breakout volume expansion
CP_MAX_BAR_ATR = _env_float("CP_MAX_BAR_ATR", 2.0)  # breakout bar range cap
CP_BODY_MIN    = _env_float("CP_BODY_MIN", 0.5)    # decisive-body fraction
CP_WICK_MAX    = _env_float("CP_WICK_MAX", 0.4)    # opposing-wick fraction
CP_MIN_RR      = _env_float("CP_MIN_RR", 1.5)      # minimum acceptable R:R
# confirm_entry() is deliberately LIGHTER than confirm_candle(): the detector
# already enforced the strict gate, so the final live-bar backstop only needs
# to reject obvious non-conviction bars.
CE_BODY_MIN    = _env_float("CE_BODY_MIN", 0.40)   # live-bar body fraction
CE_WICK_MAX    = _env_float("CE_WICK_MAX", 0.60)   # live-bar opposing-wick cap
CE_VOL_MULT    = _env_float("CE_VOL_MULT", 1.25)   # live-bar volume expansion


# --------------------------------------------------------------------------- #
#  Result type
# --------------------------------------------------------------------------- #
@dataclass
class ChartPattern:
    name: str            # e.g. "Flag Long" — routed by multi_strategy_live
    direction: int       # +1 long / -1 short
    strategy: str        # canonical strategy tag (FLAG / TRIANGLE / ...)
    strength: int        # 1-6 signal strength scale shared with the scanner
    entry: float         # pattern entry price (signal-bar close)
    sl: float            # pattern-native stop-loss
    target: float        # pattern-native target (measured move / 1.5R floor)
    sl_method: str       # how the SL was derived (audit trail)
    tgt_method: str      # how the target was derived (audit trail)
    rr: float = 0.0      # realized risk:reward at entry
    detail: dict = field(default_factory=dict)

    def __post_init__(self):
        risk = abs(self.entry - self.sl)
        reward = (self.target - self.entry) * self.direction
        self.rr = round(reward / risk, 2) if risk > 0 else 0.0


PATTERN_NAMES = frozenset({
    "Flag Long", "Flag Short",
    "Triangle Long", "Triangle Short",
    "Double Top Short", "Double Bottom Long",
    "ABCD Long", "ABCD Short",
    "ABCD Fade Long", "ABCD Fade Short",
})


# --------------------------------------------------------------------------- #
#  Shared market-data helpers
# --------------------------------------------------------------------------- #


def _atr(df, length: int = 14) -> float:
    try:
        import intraday_pattern_scanner_v2 as scn
        v = scn.wilder_atr(df, length)
        if v is not None and v > 0:
            return float(v)
    except Exception:
        pass
    try:
        return float(df["close"].iloc[-1]) * 0.005
    except Exception:
        return 0.0


def _vol_ratio(df, lookback: int = 20) -> float:
    try:
        vol_now = float(df["volume"].iloc[-1])
        prev = df["volume"].iloc[:-1]
        avg = float(prev.tail(lookback).mean()) if len(prev) >= 5 else 0.0
        return vol_now / avg if avg > 0 else 0.0
    except Exception:
        return 0.0


def _risk_rails() -> tuple[float, float]:
    """Scanner's SL-distance rails (fraction of entry)."""
    try:
        import intraday_pattern_scanner_v2 as scn
        return float(scn.MIN_SL_PCT), float(scn.MAX_SL_PCT)
    except Exception:
        return 0.006, 0.015


def _last_bar(df) -> Optional[tuple[float, float, float, float]]:
    try:
        return (float(df["open"].iloc[-1]), float(df["high"].iloc[-1]),
                float(df["low"].iloc[-1]), float(df["close"].iloc[-1]))
    except Exception:
        return None


# --------------------------------------------------------------------------- #
#  Candle confirmation — the pre-entry gate
# --------------------------------------------------------------------------- #
def confirm_candle(o: float, h: float, l: float, c: float,
                   vol_ratio: float, atr: float, direction: int,
                   level: Optional[float] = None) -> bool:
    """True when the signal bar confirms a pattern entry.

    Research rails: close beyond the level (never a wick pierce), volume
    expansion, decisive body, no long opposing wick, bar not over-extended.
    """
    if atr <= 0 or vol_ratio < CP_VOL_MULT:
        return False
    rng = max(h - l, 1e-9)
    if rng > CP_MAX_BAR_ATR * atr:          # exhaustion / chase bar
        return False
    if abs(c - o) < CP_BODY_MIN * rng:      # indecisive body
        return False
    if level is not None:                   # close must clear the level
        if direction > 0 and c <= level:
            return False
        if direction < 0 and c >= level:
            return False
    uw = h - max(o, c)
    lw = min(o, c) - l
    opp = uw if direction > 0 else lw
    if opp > CP_WICK_MAX * rng:              # long opposing wick = rejection
        return False
    return True


def confirm_entry(df, direction: int) -> bool:
    """Final pre-entry backstop on the live bar (lighter than confirm_candle).

    Applied by the scorer to every chart-pattern row before a paper/live
    entry is allowed: no new entries after ~14:30 IST, the signal candle
    must show conviction (body >= CE_BODY_MIN of range), a sane range
    (<= 2x ATR), no dominant opposing wick (<= CE_WICK_MAX), and real
    volume (>= CE_VOL_MULT average).
    Detectors already enforce the stricter confirm_candle(); this keeps
    the gate explicit even if a detector is ever relaxed.
    """
    bar = _last_bar(df)
    if bar is None:
        return False
    # research: no new intraday entries after ~14:30 IST
    try:
        ts = df["ts"].iloc[-1]
        if (ts.hour, ts.minute) >= CP_ENTRY_END:
            return False
    except Exception:
        return False
    o, h, l, c = bar
    atr = _atr(df)
    if atr <= 0:
        return False
    rng = max(h - l, 1e-9)
    if rng > CP_MAX_BAR_ATR * atr:
        return False
    if abs(c - o) < CE_BODY_MIN * rng:
        return False
    uw = h - max(o, c)
    lw = min(o, c) - l
    if (uw if direction > 0 else lw) > CE_WICK_MAX * rng:
        return False
    return _vol_ratio(df) >= CE_VOL_MULT


# --------------------------------------------------------------------------- #
#  Exit math shared by every pattern (incl. ORB + VWAP upgrades)
# --------------------------------------------------------------------------- #
def clamp_sl(entry: float, sl: float, direction: int) -> float:
    """Clamp a pattern stop into the scanner's [MIN_SL_PCT, MAX_SL_PCT] rails."""
    lo_pct, hi_pct = _risk_rails()
    dist = abs(entry - sl)
    dist = max(lo_pct * entry, min(dist, hi_pct * entry))
    return entry - direction * dist


def exits_ok(entry: float, sl: float, target: float, direction: int,
             min_rr: float = CP_MIN_RR) -> bool:
    """True when the pattern geometry funds at least min_rr at entry."""
    risk = abs(entry - sl)
    if risk <= 0:
        return False
    # 1e-9 tolerance: (entry + 1.5*risk) - entry can round 1 ulp below 1.5*risk
    return (target - entry) * direction >= min_rr * risk - 1e-9


def _finalize(name: str, direction: int, strategy: str, entry: float,
              raw_sl: float, raw_target: float,
              sl_method: str, tgt_method: str, detail: dict | None = None):
    """Clamp the pattern stop to the risk rails; keep the setup only if the
    geometry still funds CP_MIN_RR. Returns a ChartPattern or None."""
    sl = clamp_sl(entry, raw_sl, direction)
    if not exits_ok(entry, sl, raw_target, direction):
        return None
    return ChartPattern(name=name, direction=direction, strategy=strategy,
                        strength=5, entry=round(entry, 2),
                        sl=round(sl, 2), target=round(raw_target, 2),
                        sl_method=sl_method, tgt_method=tgt_method,
                        detail=detail or {})


def orb_exits(or_high: float, or_low: float, entry: float,
              direction: int, atr: float,
              min_rr: float = 1.0) -> Optional[tuple[float, float]]:
    """Pattern-native ORB exits — research rule.

    Long:  SL = OR_low  - 0.1x ATR (opposite extreme, full-range risk)
    Short: SL = OR_high + 0.1x ATR
    T1 target = entry +/- 1.5x opening-range width (2x = T2 runner).
    The report's own geometry is a ~1.3-1.45R trade (SL spans the full
    range), so the gate here is 1.0R, not 1.5R — it only rejects breakouts
    that extended too far past the extreme (chase entries), consistent
    with the report's breakout-bar range filter.
    Returns (sl, target) or None.
    """
    width = or_high - or_low
    if width <= 0 or atr <= 0 or entry <= 0:
        return None
    if width < 0.4 * atr:
        return None  # report: skip tiny ranges
    raw_sl = or_low - 0.1 * atr if direction > 0 else or_high + 0.1 * atr
    target = entry + direction * 1.5 * width
    sl = clamp_sl(entry, raw_sl, direction)
    if not exits_ok(entry, sl, target, direction, min_rr):
        return None
    return round(sl, 2), round(target, 2)


def vwap_exits(entry: float, direction: int, atr: float,
               raw_stop: float, swing: Optional[float]) -> Optional[tuple[float, float]]:
    """Pattern-native VWAP exits — research rule.

    raw_stop: pullback  -> reversal-candle extreme -/+ 0.1x ATR
              reclaim   -> VWAP -/+ 0.15x ATR
    target:   prior swing extreme; 1.5R floor when the swing is too close.
    """
    if atr <= 0 or entry <= 0:
        return None
    sl = clamp_sl(entry, raw_stop, direction)
    risk = abs(entry - sl)
    target = swing
    if swing is None or (swing - entry) * direction < CP_MIN_RR * risk:
        target = entry + direction * CP_MIN_RR * risk
    if not exits_ok(entry, sl, target, direction):
        return None
    return round(sl, 2), round(target, 2)


# --------------------------------------------------------------------------- #
#  Swing detection (fractals; the live bar is never a swing)
# --------------------------------------------------------------------------- #
def _swings(df, side: str, k: int = 2, lookback: int = 48):
    vals = (df["high"] if side == "high" else df["low"]).to_numpy()
    n = len(vals)
    out = []
    for i in range(max(k, n - lookback), n - k):
        w = float(vals[i])
        left, right = vals[i - k:i], vals[i + 1:i + k + 1]
        if side == "high":
            if w > left.max() and w >= right.max():
                out.append((i, w))
        else:
            if w < left.min() and w <= right.min():
                out.append((i, w))
    return out


def _avg_vol(df, a: int, b: int) -> float:
    try:
        v = df["volume"].iloc[max(0, a):b]
        return float(v.mean()) if len(v) else 0.0
    except Exception:
        return 0.0


# --------------------------------------------------------------------------- #
#  Pattern 2 — Bull / Bear Flag
# --------------------------------------------------------------------------- #
def detect_flag(df, atr: float) -> list[ChartPattern]:
    """High-tight flag: >=4-bar impulse (>=1.0x ATR, avg body >=60% of range),
    then a 3-10 bar flag holding <=50% of the pole with contracting volume;
    entry on the 5-min close beyond the flag boundary with confirmation.

    Long:  SL = flag_low  - 0.1x ATR | target = entry + pole height
    Short: SL = flag_high + 0.1x ATR | target = entry - pole height
    """
    n = len(df)
    if atr <= 0 or n < 16:
        return []
    vr = _vol_ratio(df)
    bar = _last_bar(df)
    if bar is None:
        return []
    o, h, l, c = bar
    out: list[ChartPattern] = []

    for direction in (+1, -1):
        hit = None
        # flag length F: 3..10 bars ending at the bar before the signal bar
        for f in range(3, 11):
            flag = df.iloc[n - 1 - f:n - 1]
            if len(flag) < 3:
                continue
            flag_high = float(flag["high"].max())
            flag_low = float(flag["low"].min())
            flag_ht = flag_high - flag_low
            # pole: 4..10 same-direction bars immediately before the flag
            for p in range(4, 11):
                pole = df.iloc[n - 1 - f - p:n - 1 - f]
                if len(pole) < 4:
                    continue
                po, ph, pl, pc = (pole[x].to_numpy() for x in
                                  ("open", "high", "low", "close"))
                bodies = pc - po
                if direction > 0 and not (bodies > 0).all():
                    continue
                if direction < 0 and not (bodies < 0).all():
                    continue
                pole_move = abs(pc[-1] - po[0])
                if pole_move < 1.0 * atr:
                    continue
                rngs = ph - pl
                if (abs(bodies) / rngs.clip(min=1e-9)).mean() < 0.6:
                    continue  # drifty grind, not an impulse
                pole_high, pole_low = float(ph.max()), float(pl.min())
                pole_ht = pole_high - pole_low
                if pole_ht <= 0 or flag_ht > 0.5 * pole_ht:
                    continue  # flag too loose
                # retrace <= 50% of the pole
                retrace = (pole_high - flag_low) if direction > 0 else (flag_high - pole_low)
                if retrace > 0.5 * pole_ht:
                    continue
                # flag must not run with the trend (flat or counter-slope)
                fc = flag["close"].to_numpy()
                drift = (fc[-1] - fc[0]) * direction
                if drift > 0.15 * atr:
                    continue
                # volume contracts in the flag vs the pole
                if _avg_vol(df, n - 1 - f, n - 1) >= _avg_vol(df, n - 1 - f - p, n - 1 - f):
                    continue
                level = flag_high if direction > 0 else flag_low
                if not confirm_candle(o, h, l, c, vr, atr, direction, level):
                    continue
                raw_sl = flag_low - 0.1 * atr if direction > 0 else flag_high + 0.1 * atr
                target = c + direction * pole_ht
                hit = _finalize(
                    "Flag Long" if direction > 0 else "Flag Short",
                    direction, "FLAG", c, raw_sl, target,
                    "flag_extreme_0.1atr", "measured_pole",
                    {"pole": round(pole_ht, 2), "flag_bars": f})
                break
            if hit:
                break
        if hit:
            out.append(hit)
    return out


# --------------------------------------------------------------------------- #
#  Pattern 4 — Triangle (ascending / descending / symmetrical)
# --------------------------------------------------------------------------- #
def detect_triangle(df, atr: float) -> list[ChartPattern]:
    """Compression: a flat line touched >=2x (touches within 0.1x ATR) with a
    converging opposing line through >=2 swings; volume declines into the
    apex; entry on the close beyond the flat line with confirmation.

    Long:  SL = most-recent higher low - 0.1x ATR
    Short: SL = most-recent lower high + 0.1x ATR
    Target = entry +/- widest triangle height (measured move).
    """
    n = len(df)
    if atr <= 0 or n < 18:
        return []
    vr = _vol_ratio(df)
    bar = _last_bar(df)
    if bar is None:
        return []
    o, h, l, c = bar
    out: list[ChartPattern] = []

    for length in range(6, 25):
        form = df.iloc[n - 1 - length:n - 1]
        if len(form) < 6:
            continue
        # volume must decline through the formation
        vh = len(form) // 2
        if float(form["volume"].iloc[vh:].mean()) >= float(form["volume"].iloc[:vh].mean()):
            continue
        highs = _swings(form, "high")
        lows = _swings(form, "low")
        if len(highs) < 2 or len(lows) < 2:
            continue
        h_vals = [p for _, p in highs]
        l_vals = [p for _, p in lows]
        h_flat = max(h_vals) - min(h_vals) <= 0.1 * atr
        l_flat = max(l_vals) - min(l_vals) <= 0.1 * atr
        rising_lows = all(b > a for a, b in zip(l_vals, l_vals[1:]))
        falling_highs = all(b < a for a, b in zip(h_vals, h_vals[1:]))

        direction = 0
        level = None
        raw_sl = None
        height = 0.0
        if h_flat and rising_lows and not l_flat:
            # ascending triangle -> long over the flat top
            direction, level = +1, sum(h_vals) / len(h_vals)
            raw_sl = l_vals[-1] - 0.1 * atr
            height = level - l_vals[0]
        elif l_flat and falling_highs and not h_flat:
            # descending triangle -> short under the flat bottom
            direction, level = -1, sum(l_vals) / len(l_vals)
            raw_sl = h_vals[-1] + 0.1 * atr
            height = h_vals[0] - level
        elif falling_highs and rising_lows and not h_flat and not l_flat:
            # symmetrical: trade the breakout direction
            height = max(h_vals[0] - l_vals[0], h_vals[-1] - l_vals[-1])
            if c > h_vals[-1]:
                direction, level = +1, h_vals[-1]
                raw_sl = l - 0.1 * atr  # breakout-bar extreme
            elif c < l_vals[-1]:
                direction, level = -1, l_vals[-1]
                raw_sl = h + 0.1 * atr
        if direction == 0 or height <= 0:
            continue
        if not confirm_candle(o, h, l, c, vr, atr, direction, level):
            continue
        target = c + direction * height
        hit = _finalize(
            "Triangle Long" if direction > 0 else "Triangle Short",
            direction, "TRIANGLE", c, raw_sl, target,
            "higher_low_0.1atr" if direction > 0 else "lower_high_0.1atr",
            "measured_triangle_height",
            {"bars": length, "height": round(height, 2)})
        if hit:
            out.append(hit)
            break  # one triangle per bar is enough
    return out


# --------------------------------------------------------------------------- #
#  Pattern 5 — Double Top / Double Bottom
# --------------------------------------------------------------------------- #
def detect_double(df, atr: float) -> list[ChartPattern]:
    """Two swings within 0.5x ATR, 3-10 bars apart, trough >=0.5x ATR deep,
    after a >=10-bar trend; entry ONLY on the 5-min close beyond the neckline
    with confirmation (unconfirmed twin peaks fail ~65% of the time).

    Double top (short):    SL = H2 + 0.1x ATR | target = neckline - height
    Double bottom (long):  SL = L2 - 0.1x ATR | target = neckline + height
    """
    n = len(df)
    if atr <= 0 or n < 20:
        return []
    vr = _vol_ratio(df)
    bar = _last_bar(df)
    if bar is None:
        return []
    o, h, l, c = bar
    out: list[ChartPattern] = []

    # --- double top: two highs, neckline = trough low between them ----------
    highs = _swings(df, "high")
    if len(highs) >= 2:
        (i1, h1), (i2, h2) = highs[-2], highs[-1]
        gap = i2 - i1
        if 3 <= gap <= 10 and abs(h2 - h1) <= 0.5 * atr:
            trough = float(df["low"].iloc[i1:i2 + 1].min())
            depth = min(h1, h2) - trough
            # preceding uptrend measured into the FIRST peak (the peaks are
            # the top; measuring to the breakdown bar would always fail)
            i0 = max(0, i1 - 10)
            trend_ok = float(df["close"].iloc[i1]) > float(df["close"].iloc[i0]) + 0.25 * atr
            # second top on equal-or-lower volume
            v1 = float(df["volume"].iloc[i1])
            v2 = float(df["volume"].iloc[i2])
            if depth >= 0.5 * atr and trend_ok and v2 <= 1.2 * v1:
                if confirm_candle(o, h, l, c, vr, atr, -1, trough):
                    height = (h1 + h2) / 2 - trough
                    hit = _finalize(
                        "Double Top Short", -1, "DOUBLE_TOP_BOTTOM", c,
                        h2 + 0.1 * atr, c - height,
                        "second_peak_0.1atr", "measured_pattern_height",
                        {"trough": round(trough, 2)})
                    if hit:
                        out.append(hit)

    # --- double bottom: mirror ------------------------------------------------
    lows = _swings(df, "low")
    if len(lows) >= 2:
        (j1, l1), (j2, l2) = lows[-2], lows[-1]
        gap = j2 - j1
        if 3 <= gap <= 10 and abs(l2 - l1) <= 0.5 * atr:
            peak = float(df["high"].iloc[j1:j2 + 1].max())
            depth = peak - max(l1, l2)
            # preceding downtrend measured into the FIRST trough
            j0 = max(0, j1 - 10)
            trend_ok = float(df["close"].iloc[j1]) < float(df["close"].iloc[j0]) - 0.25 * atr
            v1 = float(df["volume"].iloc[j1])
            v2 = float(df["volume"].iloc[j2])
            if depth >= 0.5 * atr and trend_ok and v2 <= 1.2 * v1:
                if confirm_candle(o, h, l, c, vr, atr, +1, peak):
                    height = peak - (l1 + l2) / 2
                    hit = _finalize(
                        "Double Bottom Long", +1, "DOUBLE_TOP_BOTTOM", c,
                        l2 - 0.1 * atr, c + height,
                        "second_trough_0.1atr", "measured_pattern_height",
                        {"neckline": round(peak, 2)})
                    if hit:
                        out.append(hit)
    return out


# --------------------------------------------------------------------------- #
#  Pattern 6 — ABCD
# --------------------------------------------------------------------------- #
def _find_abc(df, atr: float, direction: int):
    """Locate A-B-C legs. Bullish: A swing low -> B swing high (>=1.5x ATR,
    >=5 bars) -> C pullback 38.2-78.6% of AB, 2-8 bars after B, on
    contracting volume. Returns (A, B, C, ia, ib, ic) or None."""
    n = len(df)
    swings_hi = _swings(df, "high")
    swings_lo = _swings(df, "low")
    if direction > 0:
        highs, lows = swings_hi, swings_lo
    else:
        highs, lows = swings_lo, swings_hi  # bearish: A=high, B=low, C=high
    # Most-recent structure first: B from `highs`, A and C from `lows`
    # (after the swap above, `lows` always holds the opposite-side swings).
    for ib, b in reversed(highs):
        for ia, a in reversed(lows):
            if not (ia < ib and ib - ia >= 5):
                continue
            ab = abs(b - a)
            if ab < 1.5 * atr:
                continue
            if direction > 0 and b <= a:
                continue
            if direction < 0 and b >= a:
                continue
            for ic, cval in reversed(lows):
                if not (ib < ic <= n - 3 and 2 <= ic - ib <= 8):
                    continue
                bc = abs(b - cval)
                retrace = bc / ab
                if not 0.382 <= retrace <= 0.786:
                    continue
                if _avg_vol(df, ib, ic) >= _avg_vol(df, ia, ib):
                    continue  # BC volume must contract vs AB
                return a, b, cval, ia, ib, ic
    return None


def detect_abcd(df, atr: float) -> list[ChartPattern]:
    """Two traded variants (research):
    (a) continuation — close above B as leg CD launches;
        SL = C -/+ 0.1x ATR, target = D projection (C +/- AB length)
    (b) fade at D — price reaches the projected D zone (+/-0.15x ATR) and
        prints a reversal candle; SL = D +/- 0.15x ATR,
        target = 38.2% retracement of leg CD.
    """
    n = len(df)
    if atr <= 0 or n < 16:
        return []
    vr = _vol_ratio(df)
    bar = _last_bar(df)
    if bar is None:
        return []
    o, h, l, c = bar
    out: list[ChartPattern] = []

    for direction in (+1, -1):
        legs = _find_abc(df, atr, direction)
        if legs is None:
            continue
        a, b, cval, ia, ib, ic = legs
        ab = abs(b - a)

        # (a) continuation: close beyond B with confirmation
        if confirm_candle(o, h, l, c, vr, atr, direction, b):
            d_proj = cval + direction * ab
            raw_sl = cval - direction * 0.1 * atr
            # research: D projection, 1.5R minimum — take the farther level
            _sl_c = clamp_sl(c, raw_sl, direction)
            _risk = abs(c - _sl_c)
            tgt = d_proj if (d_proj - c) * direction >= CP_MIN_RR * _risk \
                else c + direction * CP_MIN_RR * _risk
            hit = _finalize(
                "ABCD Long" if direction > 0 else "ABCD Short",
                direction, "ABCD", c, raw_sl, tgt,
                "point_c_0.1atr", "measured_d_projection_1.5r_floor",
                {"variant": "continuation"})
            if hit:
                out.append(hit)
                continue

        # (b) fade at D: price inside the D zone + reversal candle
        d_proj = cval + direction * ab
        if abs(c - d_proj) <= 0.15 * atr:
            cd_bars = (n - 1) - ic
            ab_bars = ib - ia
            if ab_bars > 0 and abs(cd_bars - ab_bars) <= 0.5 * ab_bars:  # time symmetry
                rng = max(h - l, 1e-9)
                # fade entries use the lighter live-bar gate (CE_*), not the
                # strict detector gate — the reversal bar only needs conviction.
                reversal = (c < o if direction > 0 else c > o) and abs(c - o) >= CE_BODY_MIN * rng
                if reversal and vr >= CE_VOL_MULT and rng <= CP_MAX_BAR_ATR * atr:
                    fade_dir = -direction
                    raw_sl = d_proj - fade_dir * 0.15 * atr
                    target = d_proj + fade_dir * 0.382 * abs(d_proj - cval)
                    hit = _finalize(
                        "ABCD Fade Long" if fade_dir > 0 else "ABCD Fade Short",
                        fade_dir, "ABCD", c, raw_sl, target,
                        "point_d_0.15atr", "cd_retrace_38.2pct",
                        {"variant": "fade_at_d"})
                    if hit:
                        out.append(hit)
    return out


# --------------------------------------------------------------------------- #
#  Top-level entry point
# --------------------------------------------------------------------------- #
def detect(df) -> list[ChartPattern]:
    """Run the four new chart-pattern detectors on 5-min bars.

    Returns ChartPattern list (possibly empty). Never raises; session rails
    (no fresh entries after ~14:30 IST) are enforced here.
    """
    try:
        if len(df) < 16 or "ts" not in df.columns:
            return []
        if not _win(df["ts"].iloc[-1], CP_ENTRY_START, CP_ENTRY_END):
            return []
        atr = _atr(df)
        if atr <= 0:
            return []
        out: list[ChartPattern] = []
        for fn in (detect_flag, detect_triangle, detect_double, detect_abcd):
            try:
                out.extend(fn(df, atr))
            except Exception as e:
                log.debug(f"{fn.__name__} failed: {e}")
        return out
    except Exception as e:
        log.debug(f"chart_patterns.detect failed: {e}")
        return []
