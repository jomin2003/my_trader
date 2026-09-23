"""
=====================================================================
KRONOS-ADAPTIVE SL/TARGET SIZING  (forward-looking exits)
=====================================================================
Turns Kronos's per-symbol forecast (vol + exp_ret) into an adaptive
adjustment for your stop-loss distance and target — so exits scale with
FORECAST volatility instead of lagging 14-bar ATR.

Design (hybrid, research-backed):
  * SL distance   : ATR/structure distance * a VOL MULTIPLIER derived
                    from Kronos's predicted volatility.
      - Kronos vol HIGH  -> widen SL  (don't get noise-stopped)
      - Kronos vol LOW   -> tighten SL (protect profit / smaller risk)
  * Target        : keep your RR-based target, but CAP it near Kronos's
                    expected move (exp_ret) when Kronos expects a smaller
                    move than your target implies. Fixes the classic
                    "target never gets hit" problem.
  * Direction gate: if the SIGNAL direction contradicts a confident
                    Kronos exp_ret, we do NOT invent a target on the wrong
                    side — we just fall back to the RR target.

SAFE BY DESIGN:
  * If Kronos has no view / is disabled / stale -> returns the ORIGINAL
    sl_dist and target unchanged (pure no-op).
  * All multipliers are clamped so a wild forecast can't produce a
    crazy stop.

Tunables (env, all optional):
  KEXIT_ENABLED        "true"/"false"   (default true)
  KEXIT_VOL_REF        reference vol % that maps to 1.0x (default 0.30)
  KEXIT_MIN_MULT       floor on the vol multiplier   (default 0.7)
  KEXIT_MAX_MULT       ceil  on the vol multiplier   (default 1.6)
  KEXIT_TGT_CAP_FRAC   cap target at this * |exp_ret| of entry (default 0.9)
  KEXIT_MIN_RR         never let the capped target fall below this RR (default 1.2)
"""
from __future__ import annotations

import os

KEXIT_ENABLED     = os.getenv("KEXIT_ENABLED", "true").lower() == "true"
VOL_REF           = float(os.getenv("KEXIT_VOL_REF", "0.30"))     # % vol that = 1.0x
MIN_MULT          = float(os.getenv("KEXIT_MIN_MULT", "0.7"))
MAX_MULT          = float(os.getenv("KEXIT_MAX_MULT", "1.6"))
TGT_CAP_FRAC      = float(os.getenv("KEXIT_TGT_CAP_FRAC", "0.9"))
MIN_RR            = float(os.getenv("KEXIT_MIN_RR", "1.2"))


def _vol_multiplier(vol_pct: float) -> float:
    """Map Kronos predicted vol (%) to a SL-distance multiplier, clamped.
       vol == VOL_REF -> 1.0x ; higher vol -> wider ; lower -> tighter."""
    if vol_pct is None or vol_pct <= 0:
        return 1.0
    m = vol_pct / max(VOL_REF, 1e-6)
    return max(MIN_MULT, min(MAX_MULT, m))


def adjust_exits(symbol: str, direction: int, entry: float,
                 sl_dist: float, target: float,
                 rr: float = 2.0):
    """Return (new_sl_dist, new_target, note).

    direction : +1 long / -1 short
    entry     : entry price
    sl_dist   : ORIGINAL stop distance (abs, price units) from ATR/structure
    target    : ORIGINAL target price
    rr        : the RR your bot used (for the min-RR floor after capping)

    Falls back to the originals if Kronos has no usable view.
    """
    if not KEXIT_ENABLED or entry <= 0 or sl_dist <= 0:
        return sl_dist, target, "kexit:off"

    # pull the Kronos view without importing heavy deps
    try:
        import kronos_gate
        view = kronos_gate.get_view(symbol)
    except Exception:
        view = None
    if not view:
        return sl_dist, target, "kexit:na"

    vol_pct = float(view.get("vol", 0.0))          # predicted volatility %
    exp_ret = float(view.get("exp_ret", 0.0))      # predicted % move (signed)

    # ---- 1) scale SL distance by forecast volatility ----
    mult = _vol_multiplier(vol_pct)
    new_sl_dist = round(sl_dist * mult, 2)
    if new_sl_dist <= 0:
        new_sl_dist = sl_dist

    # ---- 2) cap target near Kronos's expected move (same direction only) ----
    new_target = target
    tgt_note = "rr"
    kron_dir = 1 if exp_ret > 0 else (-1 if exp_ret < 0 else 0)
    if kron_dir == direction and abs(exp_ret) > 0:
        # price Kronos expects, discounted by TGT_CAP_FRAC (don't be greedy)
        kron_move = entry * (abs(exp_ret) / 100.0) * TGT_CAP_FRAC
        kron_target = round(entry + direction * kron_move, 2)
        rr_reward = abs(target - entry)
        kron_reward = abs(kron_target - entry)
        # only pull the target IN (never push it further out)
        if kron_reward < rr_reward:
            # but never below the min-RR floor vs the (new) stop
            floor_reward = MIN_RR * new_sl_dist
            final_reward = max(kron_reward, floor_reward)
            new_target = round(entry + direction * final_reward, 2)
            tgt_note = f"cap@{abs(exp_ret):.2f}%"

    note = f"kexit:mult{mult:.2f}/{tgt_note}"
    return new_sl_dist, new_target, note

