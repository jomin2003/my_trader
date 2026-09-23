"""
strategy_exits.py
=================
Per-strategy exit tuning for intraday_pattern_scanner_v2.

WHY THIS EXISTS
---------------
The scanner already does ATR-based SL/TGT, trailing, breakeven, partials and
Kronos-adaptive exits.  What it does NOT do is tune those per strategy — every
strategy shares the global ATR_MULTIPLIER / RISK_REWARD_RATIO.  This module adds
that missing layer WITHOUT touching any existing exit logic or state.

It is a pure lookup table + two tiny helpers.  Safe no-op fallbacks everywhere:
if a strategy isn't listed, you get the scanner's current global behaviour.

INTEGRATION  (3 tiny edits, shown in the chat message):
  1. import this module in the scanner
  2. compute_sl_target() reads per-strategy sl_mult / rr
  3. _update_trailing_stop() reads per-strategy trail_mult   (optional)

Tune the numbers below from YOUR backtest blotter over time.
"""

from __future__ import annotations
from typing import Optional, Tuple

# --------------------------------------------------------------------------- #
#  Per-strategy exit profiles
#
#  sl_mult   -> SL distance   = sl_mult  * ATR   (overrides ATR_MULTIPLIER)
#  rr        -> target        = rr       * SL    (overrides RISK_REWARD_RATIO)
#  trail_mult-> trail distance= trail_mult* ATR  (overrides TRAILING_ATR_MULT)
#
#  Anything omitted falls back to the scanner's global default => zero risk.
# --------------------------------------------------------------------------- #

_PROFILES = {
    # SQUAREOFF FIX (135 timeouts): 2R targets = 4x ATR moves, rarely hit intraday.
    # RR 1.5 across runners lifts hit-rate to 40-47% breakeven band.
    "CANDLE_STRUCT": {"sl_mult": 2.0, "rr": 1.5, "trail_mult": 1.3},
    "ORB":           {"sl_mult": 2.0, "rr": 1.5, "trail_mult": 1.1},
    "OB_SHORTS":     {"sl_mult": 2.0, "rr": 1.5, "trail_mult": 1.0},
    "OB SHORTS":     {"sl_mult": 2.0, "rr": 1.5, "trail_mult": 1.0},  # alias
    "GAP_FILL":      {"sl_mult": 2.0, "rr": 1.5, "trail_mult": 1.2},
    "GAP-FILL":      {"sl_mult": 2.0, "rr": 1.5, "trail_mult": 1.2},  # alias
    "GAPFILL":       {"sl_mult": 2.0, "rr": 1.5, "trail_mult": 1.2},
    "GAPGO":         {"sl_mult": 2.0, "rr": 1.5, "trail_mult": 1.1},
    "VWAP_RECLAIM":  {"sl_mult": 2.0, "rr": 1.5, "trail_mult": 1.1},
    "VWAP_PULLBACK": {"sl_mult": 2.0, "rr": 1.5, "trail_mult": 1.0},
    "SUPERTREND":    {"sl_mult": 2.0, "rr": 1.5, "trail_mult": 1.2},
    # Mean reversion profile: many small wins (RR < 1), tight SL, quick trail —
    # the mirror image of the momentum profiles above (Connors RSI-2 shape).
    "MR_VWAP_FADE":  {"sl_mult": 1.2, "rr": 0.8, "trail_mult": 0.5},
    # Previous-day-level breakout: momentum profile, moderate trail.
    "PDHL_BREAK":    {"sl_mult": 1.5, "rr": 1.5, "trail_mult": 1.0},
    # Chart-pattern fallbacks (struct_sl/struct_target normally carry the
    # pattern-native levels; these apply only if geometry exits are absent).
    "FLAG":              {"sl_mult": 2.0, "rr": 1.5, "trail_mult": 1.1},
    "TRIANGLE":          {"sl_mult": 2.0, "rr": 1.5, "trail_mult": 1.1},
    "DOUBLE_TOP_BOTTOM": {"sl_mult": 2.0, "rr": 1.5, "trail_mult": 1.0},
    "ABCD":              {"sl_mult": 2.0, "rr": 1.5, "trail_mult": 1.1},
}


def _norm(name: Optional[str]) -> str:
    return (name or "").strip().upper()


def get_exit_params(
    strategy: Optional[str],
    default_sl_mult: float,
    default_rr: float,
    default_trail_mult: float,
) -> Tuple[float, float, float]:
    """
    Return (sl_mult, rr, trail_mult) for a strategy.

    Pass the scanner's current globals as the defaults so any strategy that
    isn't in the table behaves EXACTLY as before (safe no-op).
    """
    p = _PROFILES.get(_norm(strategy), {})
    return (
        float(p.get("sl_mult", default_sl_mult)),
        float(p.get("rr", default_rr)),
        float(p.get("trail_mult", default_trail_mult)),
    )


def trail_mult_for(strategy: Optional[str], default_trail_mult: float) -> float:
    """Convenience: just the trailing multiplier for a strategy."""
    return float(_PROFILES.get(_norm(strategy), {}).get("trail_mult", default_trail_mult))


# --------------------------------------------------------------------------- #
#  Self-test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    # scanner globals for the test
    G_SL, G_RR, G_TRAIL = 1.5, 2.0, 1.0

    for strat in ["CANDLE_STRUCT", "ORB", "GAP-FILL", "OB SHORTS", "UNKNOWN", None]:
        sl, rr, tr = get_exit_params(strat, G_SL, G_RR, G_TRAIL)
        print(f"{str(strat):15s} -> sl_mult={sl}  rr={rr}  trail_mult={tr}")

    # UNKNOWN / None must fall back to globals exactly
    assert get_exit_params("UNKNOWN", G_SL, G_RR, G_TRAIL) == (G_SL, G_RR, G_TRAIL)
    assert get_exit_params(None, G_SL, G_RR, G_TRAIL) == (G_SL, G_RR, G_TRAIL)
    print("Fallback safety: OK")
    print("Self-test passed")
