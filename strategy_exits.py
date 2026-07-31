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
    # engulfing / reversal structure — give it room, let winners run
    "CANDLE_STRUCT": {"sl_mult": 1.6, "rr": 2.8, "trail_mult": 1.3},

    # opening-range breakout — trends hard: tighter stop, wider target
    "ORB":           {"sl_mult": 1.2, "rr": 3.0, "trail_mult": 1.1},

    # order-block shorts — snug stop just above the block
    "OB_SHORTS":     {"sl_mult": 1.3, "rr": 2.2, "trail_mult": 1.0},
    "OB SHORTS":     {"sl_mult": 1.3, "rr": 2.2, "trail_mult": 1.0},  # alias

    # gap-fill fade — mean-reverting: modest target, don't chase
    "GAP_FILL":      {"sl_mult": 1.4, "rr": 1.8, "trail_mult": 1.2},
    "GAP-FILL":      {"sl_mult": 1.4, "rr": 1.8, "trail_mult": 1.2},  # alias
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
