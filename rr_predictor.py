"""
rr_predictor.py  —  Tier-3 live reader: predict best SL/TGT from the trained model
===================================================================================
Loads rr_model.txt + rr_meta.json (from rr_trainer.py) and, for a live entry,
sweeps the (sl_mult, tgt_mult) grid to pick the pair with the highest PREDICTED
net R.

SAFETY:
  * OFF by default. Enable with env RR_GATE_ENABLED=1.
  * Refuses to act unless meta.skill_vs_mean > RR_MIN_SKILL (default 0.0):
    a zero-skill model returns None -> scanner keeps its existing logic.
  * Any load/predict error -> returns None (neutral). Never raises.

USAGE (in scanner.compute_sl_target):
    if rr_predictor.enabled():
        feat = rr_predictor.features_from_ohlc(df)
        rec  = rr_predictor.best_rr(feat, side)   # side: +1 long / -1 short
        if rec:  # {'sl_mult','tgt_mult','pred_r'}
            ...

Env:
  RR_GATE_ENABLED  "1" to enable (default off)
  RR_MODEL_PATH    default ./rr_model.txt
  RR_META_PATH     default ./rr_meta.json
  RR_MIN_SKILL     require meta.skill_vs_mean > this (default 0.0)
  RR_MIN_PRED_R    require best predicted R >= this (default -999 = off)
"""

from __future__ import annotations
import json, os
from typing import Dict, List, Optional
import rr_features   # shared feature definitions (train == infer)

MODEL_PATH = os.environ.get("RR_MODEL_PATH", "./rr_model.txt")
META_PATH = os.environ.get("RR_META_PATH", "./rr_meta.json")
MIN_SKILL = float(os.environ.get("RR_MIN_SKILL", "0.0"))
MIN_PRED_R = float(os.environ.get("RR_MIN_PRED_R", "-999"))
_ENABLED = os.environ.get("RR_GATE_ENABLED", "0") == "1"

_model = None
_meta: dict = {}
_features: List[str] = []
_sl_grid: List[float] = []
_tgt_grid: List[float] = []
_loaded = False
_ok = False


def _load():
    global _model, _meta, _features, _sl_grid, _tgt_grid, _loaded, _ok
    _loaded = True
    try:
        with open(META_PATH) as f:
            _meta = json.load(f)
        if _meta.get("model") != "lightgbm":
            _ok = False; return
        if float(_meta.get("skill_vs_mean", 0.0)) <= MIN_SKILL:
            _ok = False; return
        _features = _meta["features"]
        _sl_grid = _meta["sl_grid"]
        _tgt_grid = _meta["tgt_grid"]
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
    """True only if the gate is ON and the model is load-ready + skilled.
    Scanner uses this to avoid extra work (e.g. data fetch) when OFF."""
    return _ready()


def best_rr(feat: Dict[str, float], side: int = 1) -> Optional[dict]:
    """
    feat: dict with base features (meta['features'] minus sl_mult/tgt_mult).
          Missing keys default to 0.0.
    side: +1 long, -1 short (reserved for future side-specific models).
    Returns {'sl_mult','tgt_mult','pred_r'} or None (neutral).
    """
    if not _ready():
        return None
    try:
        best = None; best_r = -1e9
        batch = []; combos = []
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
                batch.append(row); combos.append((sl_m, tgt_m))
        if not batch:
            return None
        preds = _model.predict(batch)
        for (sl_m, tgt_m), pr in zip(combos, preds):
            if pr > best_r:
                best_r, best = float(pr), (sl_m, tgt_m)
        if best is None or best_r < MIN_PRED_R:
            return None
        return {"sl_mult": best[0], "tgt_mult": best[1], "pred_r": round(best_r, 4)}
    except Exception as e:
        print(f"[rr_predictor] predict failed -> neutral ({e})")
        return None


def features_from_ohlc(df) -> Optional[Dict[str, float]]:
    """Build the base feature dict from an OHLCV DataFrame (same bars
    fetch_intraday returns). Uses SHARED rr_features so it matches training."""
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
    return (f"RR[{state}]: {_meta.get('model','?')} "
            f"skill={_meta.get('skill_vs_mean','?')} "
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
