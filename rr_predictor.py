"""
rr_predictor.py  —  Tier-3 live reader (v2: WIN-PROB + EXPECTED-VALUE picker)
============================================================================
Loads rr_model.txt + rr_meta.json (from rr_trainer.py v2) and, for a live
entry, sweeps the (sl_mult, tgt_mult) grid to pick the pair with the highest
RISK-ADJUSTED EXPECTED VALUE in R, net of costs:

        p    = model.predict(features + [sl_mult, tgt_mult])   # P(target first)
        EV_R = p * (tgt_mult / sl_mult) - (1 - p) - cost_R

This replaces the old "highest predicted net-R" pick that corner-collapsed to
the widest grid cell. Because a wider target lowers p and a wider stop inflates
the risk denominator, EV_R now genuinely trades hit-rate against reward.

BACKWARD/ FORWARD COMPATIBLE
  * New model  (meta.objective == "binary_winprob")  -> EV picker (above).
  * Legacy model (old net-R regressor)               -> falls back to the old
    "max predicted R" behaviour so nothing breaks if you point it at an old
    rr_model.txt.

SAFETY (unchanged contract)
  * OFF by default. Enable with env RR_GATE_ENABLED=1.
  * Refuses to act unless meta.skill_vs_mean > RR_MIN_SKILL (default 0.0).
  * Any load/predict error -> returns None (neutral). Never raises.

Env:
  RR_GATE_ENABLED  "1" to enable (default off)
  RR_MODEL_PATH    default ./rr_model.txt
  RR_META_PATH     default ./rr_meta.json
  RR_MIN_SKILL     require meta.skill_vs_mean > this (default 0.0)
  RR_MIN_PRED_R    require best EV_R >= this (default 0.0 for the new model)
"""

from __future__ import annotations
import json
import os
from typing import Dict, List, Optional

import rr_features   # shared feature definitions (train == infer)

MODEL_PATH = os.environ.get("RR_MODEL_PATH", "./rr_model.txt")
META_PATH = os.environ.get("RR_META_PATH", "./rr_meta.json")
MIN_SKILL = float(os.environ.get("RR_MIN_SKILL", "0.0"))
# new model: EV_R is a proper expected value, so 0.0 = only take +EV setups
MIN_PRED_R = float(os.environ.get("RR_MIN_PRED_R", "0.0"))
_ENABLED = os.environ.get("RR_GATE_ENABLED", "0") == "1"

_model = None
_meta: dict = {}
_features: List[str] = []
_sl_grid: List[float] = []
_tgt_grid: List[float] = []
_is_winprob = False
_cost_frac = 0.0                 # round-trip cost as a fraction of price
_loaded = False
_ok = False


def _load():
    global _model, _meta, _features, _sl_grid, _tgt_grid
    global _is_winprob, _cost_frac, _loaded, _ok
    _loaded = True
    try:
        with open(META_PATH) as f:
            _meta = json.load(f)
        if _meta.get("model") != "lightgbm":
            _ok = False
            return
        if float(_meta.get("skill_vs_mean", 0.0)) <= MIN_SKILL:
            _ok = False
            return
        _features = _meta["features"]
        _sl_grid = _meta["sl_grid"]
        _tgt_grid = _meta["tgt_grid"]
        _is_winprob = (_meta.get("objective") == "binary_winprob")
        cm = _meta.get("cost_model", {})
        # both-ways: 2*(slippage + taxes) in bps -> fraction
        _cost_frac = 2.0 * (float(cm.get("slippage_bps", 0)) +
                            float(cm.get("taxes_bps_oneway", 0))) / 1e4
        import lightgbm as lgb
        _model = lgb.Booster(model_file=MODEL_PATH)
        _ok = True
    except Exception as e:
        print(f"[rr_predictor] load failed -> neutral ({e})")
        _ok = False


def _ready() -> bool:
    if not _ENABLED:
        return False
    if not _loaded:
        _load()
    return _ok and _model is not None


def enabled() -> bool:
    """True only if the gate is ON and the model is load-ready + skilled."""
    return _ready()


def _cost_r(atr_frac: float, sl_mult: float) -> float:
    """Round-trip cost expressed in R (risk unit = sl_mult * ATR)."""
    if not _cost_frac or atr_frac is None or atr_frac <= 0 or sl_mult <= 0:
        return 0.0
    cost_atr = _cost_frac / atr_frac        # cost in ATR units
    return cost_atr / sl_mult               # in R


def best_rr(feat: Dict[str, float], side: int = 1) -> Optional[dict]:
    """
    feat: base feature dict (meta['features'] minus sl_mult/tgt_mult).
    side: +1 long, -1 short (reserved; model is side-agnostic by design).
    Returns {'sl_mult','tgt_mult','pred_r','pred_p'} or None (neutral).
    """
    if not _ready():
        return None
    try:
        atr_frac = float(feat.get("atr_frac", 0.0))
        batch, combos = [], []
        for sl_m in _sl_grid:
            for tgt_m in _tgt_grid:
                if tgt_m <= sl_m * 0.6:       # skip nonsensical sub-0.6 RR
                    continue
                row = []
                for f in _features:
                    if f == "sl_mult":
                        row.append(sl_m)
                    elif f == "tgt_mult":
                        row.append(tgt_m)
                    else:
                        row.append(float(feat.get(f, 0.0)))
                batch.append(row)
                combos.append((sl_m, tgt_m))
        if not batch:
            return None
        preds = _model.predict(batch)

        best = None
        best_score = -1e18
        best_p = None
        for (sl_m, tgt_m), pr in zip(combos, preds):
            if _is_winprob:
                p = float(pr)
                # risk-adjusted expected value, in R, net of costs
                score = p * (tgt_m / sl_m) - (1.0 - p) - _cost_r(atr_frac, sl_m)
            else:
                # legacy net-R regressor: keep old behaviour
                p = None
                score = float(pr)
            if score > best_score:
                best_score, best, best_p = score, (sl_m, tgt_m), p

        if best is None or best_score < MIN_PRED_R:
            return None
        out = {"sl_mult": best[0], "tgt_mult": best[1],
               "pred_r": round(best_score, 4)}
        if best_p is not None:
            out["pred_p"] = round(best_p, 4)
        return out
    except Exception as e:
        print(f"[rr_predictor] predict failed -> neutral ({e})")
        return None


def features_from_ohlc(df) -> Optional[Dict[str, float]]:
    """Build the base feature dict from an OHLCV DataFrame using the SHARED
    rr_features module so it matches training exactly."""
    try:
        return rr_features.latest_features(df)
    except Exception as e:
        print(f"[rr_predictor] feature build failed -> None ({e})")
        return None


def rr_summary() -> str:
    """One-liner for /status."""
    if not _loaded:
        _load()
    state = "ON" if _ENABLED else "OFF"
    if not _meta:
        return f"RR[{state}]: no model"
    kind = "winprob" if _meta.get("objective") == "binary_winprob" else "netR"
    return (f"RR[{state}]: {kind} "
            f"skill={_meta.get('skill_vs_mean','?')} "
            f"auc={_meta.get('auc','?')} "
            f"horizon={_meta.get('horizon_bars','?')}b "
            f"{'ready' if _ok else 'gated(low-skill)'}")


if __name__ == "__main__":
    os.environ["RR_GATE_ENABLED"] = "1"
    _ENABLED = True
    _load()
    print(rr_summary())
    demo = {"atr_frac": 0.004, "rsi": 55, "vwap_dist": 0.001, "ema_dist": 0.002,
            "adx": 22, "vol_ratio": 1.4, "ret1": 0.001, "ret3": 0.003,
            "range_pct": 0.006, "gap_pct": 0.0, "tod": 10.5, "body_frac": 0.6}
    print("best_rr(long) :", best_rr(demo, 1))
    print("best_rr(short):", best_rr(demo, -1))
