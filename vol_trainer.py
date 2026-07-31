"""
vol_trainer.py  —  Tier-2 nightly volatility forecaster
========================================================
Trains a LightGBM regressor to forecast each symbol's NEXT-day realized
volatility, then converts that into a per-symbol `vol_scale` multiplier that
the live scanner uses to widen/tighten ATR stops FORWARD-lookingly.

Pipeline (runs after market close, e.g. GitHub Actions ~16:00 IST):
  1. load OHLCV CSVs from --csv-dir  (your csv_downloader output)
  2. resample to daily if intraday, engineer volatility features
  3. pool all symbols -> ONE global LightGBM regressor (robust vs per-symbol)
  4. predict tomorrow's vol fraction for each symbol's latest bar
  5. vol_scale = clamp(pred_vol / trailing_baseline, LO, HI)
  6. write vol_forecast.json  (local + optional Gist push)

Design principles matching your stack:
  * CPU-only, no torch, tiny model  -> free GitHub Actions friendly
  * Interpretable (feature_importances_ logged)
  * Graceful EWMA fallback if LightGBM missing or data too thin
  * Never crashes the pipeline: worst case it emits neutral scales (1.0)

Target definition:
  Next-day Parkinson volatility as a fraction of price — a high/low-range
  estimator that is a clean proxy for intraday movement magnitude.

Env for Gist push (optional; same pattern as your Kronos job):
  GIST_TOKEN     -> GitHub PAT with `gist` scope
  VOL_GIST_ID    -> id of the gist holding vol_forecast.json
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ---- tunables -------------------------------------------------------------- #
VOL_SCALE_LO = 0.65       # never tighten stops below 65% of the strategy default
VOL_SCALE_HI = 1.60       # never widen beyond 160%
BASELINE_WINDOW = 20      # trailing days for the per-symbol vol baseline
MIN_ROWS_PER_SYMBOL = 40  # need at least this many daily bars to be useful
GIST_FILENAME = os.environ.get("VOL_GIST_FILENAME", "vol_forecast.json")

FEATURES = [
    "ret1", "ret2", "absret1",
    "rv5", "rv10", "rv20",
    "atr_frac", "park_vol", "range_pct",
    "gap_pct", "vol_z", "dow",
]


# --------------------------------------------------------------------------- #
#  Data loading + resampling
# --------------------------------------------------------------------------- #

def _cols(df: pd.DataFrame) -> Dict[str, str]:
    return {c.lower(): c for c in df.columns}


def _load_one(path: str) -> Optional[pd.DataFrame]:
    try:
        df = pd.read_csv(path)
    except Exception as e:
        print(f"[vol] skip {os.path.basename(path)} (read error: {e})")
        return None
    c = _cols(df)
    need = ("open", "high", "low", "close")
    if not all(k in c for k in need):
        print(f"[vol] skip {os.path.basename(path)} (missing OHLC)")
        return None

    out = pd.DataFrame({
        "open": df[c["open"]].astype(float),
        "high": df[c["high"]].astype(float),
        "low": df[c["low"]].astype(float),
        "close": df[c["close"]].astype(float),
        "volume": df[c["volume"]].astype(float) if "volume" in c else 0.0,
    })

    # timestamp handling -> resample intraday to daily
    ts_col = next((c[k] for k in ("timestamp", "ts", "datetime", "date", "time")
                   if k in c), None)
    if ts_col is not None:
        ts = pd.to_datetime(df[ts_col], errors="coerce", utc=True)
        out.index = ts
        out = out.dropna(axis=0, how="any")
        # if there are multiple rows per calendar day, it's intraday -> resample
        if out.index.normalize().duplicated().any():
            out = out.resample("1D").agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum",
            }).dropna(how="any")
    out = out.reset_index(drop=True)
    return out if len(out) >= MIN_ROWS_PER_SYMBOL else None


def _symbol_of(path: str) -> str:
    base = os.path.basename(path)
    for suf in (".csv", ".CSV"):
        if base.endswith(suf):
            base = base[: -len(suf)]
    return base.upper()


# --------------------------------------------------------------------------- #
#  Feature engineering
# --------------------------------------------------------------------------- #

def _parkinson(high: pd.Series, low: pd.Series) -> pd.Series:
    # Parkinson vol (per-bar): sqrt( (1/(4 ln2)) * ln(H/L)^2 )  -> fraction of price
    hl = np.log((high / low).clip(lower=1e-9))
    return np.sqrt((hl ** 2) / (4.0 * math.log(2.0)))


def _make_features(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    close = d["close"]
    logret = np.log(close / close.shift(1))

    d["ret1"] = logret
    d["ret2"] = np.log(close / close.shift(2))
    d["absret1"] = logret.abs()
    d["rv5"] = logret.rolling(5).std()
    d["rv10"] = logret.rolling(10).std()
    d["rv20"] = logret.rolling(20).std()

    # ATR(14) as fraction of price
    prev_c = close.shift(1)
    tr = pd.concat([(d["high"] - d["low"]),
                    (d["high"] - prev_c).abs(),
                    (d["low"] - prev_c).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    d["atr_frac"] = atr / close

    d["park_vol"] = _parkinson(d["high"], d["low"])
    d["range_pct"] = (d["high"] - d["low"]) / close
    d["gap_pct"] = (d["open"] - prev_c) / prev_c
    vol_mean = d["volume"].rolling(20).mean()
    vol_std = d["volume"].rolling(20).std().replace(0, np.nan)
    d["vol_z"] = ((d["volume"] - vol_mean) / vol_std).fillna(0.0)
    d["dow"] = (np.arange(len(d)) % 5).astype(float)  # proxy weekday cycle

    # TARGET: next-day Parkinson vol (what we want to forecast)
    d["y_next_vol"] = d["park_vol"].shift(-1)
    return d


def build_dataset(csv_dir: str) -> Tuple[pd.DataFrame, Dict[str, pd.DataFrame]]:
    paths = sorted(glob.glob(os.path.join(csv_dir, "*.csv")))
    if not paths:
        raise FileNotFoundError(f"no CSVs in {csv_dir}")
    rows: List[pd.DataFrame] = []
    per_symbol: Dict[str, pd.DataFrame] = {}
    for p in paths:
        raw = _load_one(p)
        if raw is None:
            continue
        feat = _make_features(raw)
        feat["symbol"] = _symbol_of(p)
        per_symbol[_symbol_of(p)] = feat
        rows.append(feat)
    if not rows:
        raise ValueError("no symbol had enough clean data")
    pooled = pd.concat(rows, ignore_index=True)
    print(f"[vol] {len(per_symbol)} symbols, {len(pooled)} pooled rows")
    return pooled, per_symbol


# --------------------------------------------------------------------------- #
#  Model
# --------------------------------------------------------------------------- #

def train_model(pooled: pd.DataFrame):
    """Train a global LightGBM regressor. Returns (model_or_None, info)."""
    train = pooled.dropna(subset=FEATURES + ["y_next_vol"]).copy()
    if len(train) < 200:
        return None, {"model": "ewma_fallback", "reason": f"only {len(train)} rows"}

    X = train[FEATURES].values
    y = train["y_next_vol"].values

    # time-ordered split (no shuffle -> honest, no look-ahead)
    cut = int(len(train) * 0.85)
    Xtr, Xva = X[:cut], X[cut:]
    ytr, yva = y[:cut], y[cut:]

    try:
        import lightgbm as lgb
    except Exception as e:
        return None, {"model": "ewma_fallback", "reason": f"lightgbm missing: {e}"}

    dtr = lgb.Dataset(Xtr, label=ytr, feature_name=FEATURES)
    dva = lgb.Dataset(Xva, label=yva, reference=dtr)
    params = dict(
        objective="regression",
        metric="mae",
        learning_rate=0.05,
        num_leaves=31,
        min_data_in_leaf=30,
        feature_fraction=0.85,
        bagging_fraction=0.85,
        bagging_freq=1,
        verbosity=-1,
    )
    model = lgb.train(
        params, dtr, num_boost_round=600, valid_sets=[dva],
        callbacks=[lgb.early_stopping(40, verbose=False), lgb.log_evaluation(0)],
    )
    pred_va = model.predict(Xva, num_iteration=model.best_iteration)
    mae = float(np.mean(np.abs(pred_va - yva)))
    base_mae = float(np.mean(np.abs(yva - ytr.mean())))
    skill = round(1 - mae / base_mae, 3) if base_mae > 0 else 0.0

    imp = dict(sorted(zip(FEATURES, model.feature_importance().tolist()),
                      key=lambda kv: -kv[1]))
    info = {
        "model": "lightgbm",
        "rows": len(train),
        "best_iter": int(model.best_iteration),
        "val_mae": round(mae, 6),
        "skill_vs_mean": skill,      # >0 means it beats predicting the average
        "top_features": list(imp.keys())[:5],
    }
    print(f"[vol] LightGBM trained: val_mae={mae:.6f} skill={skill} "
          f"iter={model.best_iteration}")
    print(f"[vol] top features: {info['top_features']}")
    return model, info


def _ewma_pred(feat: pd.DataFrame) -> float:
    """Fallback forecast: EWMA of recent Parkinson vol."""
    pv = feat["park_vol"].dropna()
    if pv.empty:
        return float("nan")
    return float(pv.ewm(span=10, adjust=False).mean().iloc[-1])


# --------------------------------------------------------------------------- #
#  Per-symbol scale
# --------------------------------------------------------------------------- #

def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _regime(scale: float) -> str:
    if scale >= 1.20:
        return "HIGH_VOL"
    if scale <= 0.85:
        return "LOW_VOL"
    return "NORMAL"


def predict_scales(model, info, per_symbol: Dict[str, pd.DataFrame]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    use_model = model is not None
    for sym, feat in per_symbol.items():
        latest = feat.iloc[-1]
        baseline_series = feat["park_vol"].dropna().tail(BASELINE_WINDOW)
        baseline = float(baseline_series.median()) if len(baseline_series) else float("nan")

        if use_model and not feat[FEATURES].iloc[-1:].isna().any(axis=None):
            pred = float(model.predict(
                feat[FEATURES].iloc[-1:].values,
                num_iteration=getattr(model, "best_iteration", None))[0])
        else:
            pred = _ewma_pred(feat)

        if not (np.isfinite(pred) and np.isfinite(baseline) and baseline > 0):
            scale = 1.0
        else:
            scale = _clamp(pred / baseline, VOL_SCALE_LO, VOL_SCALE_HI)

        out[sym] = {
            "pred_vol": round(pred, 6) if np.isfinite(pred) else None,
            "baseline": round(baseline, 6) if np.isfinite(baseline) else None,
            "scale": round(scale, 3),
            "regime": _regime(scale),
        }
    return out


# --------------------------------------------------------------------------- #
#  Output + Gist
# --------------------------------------------------------------------------- #

def _push_gist(payload: str) -> bool:
    token = os.environ.get("GIST_TOKEN", "")
    gid = os.environ.get("VOL_GIST_ID", "")
    if not (token and gid):
        print("[vol] Gist env not set -> local file only")
        return False
    import requests
    try:
        r = requests.patch(
            f"https://api.github.com/gists/{gid}",
            headers={"Authorization": f"token {token}",
                     "Accept": "application/vnd.github+json"},
            json={"files": {GIST_FILENAME: {"content": payload}}},
            timeout=20)
        r.raise_for_status()
        print(f"[vol] pushed to Gist {gid}")
        return True
    except Exception as e:
        print(f"[vol] Gist push failed: {e}")
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default="./data", help="dir of OHLCV CSVs")
    ap.add_argument("--out", default="./vol_forecast.json")
    ap.add_argument("--no-gist", action="store_true")
    args = ap.parse_args()

    try:
        pooled, per_symbol = build_dataset(args.csv_dir)
        model, info = train_model(pooled)
        scales = predict_scales(model, info, per_symbol)
    except Exception as e:
        print(f"[vol] FATAL, emitting neutral forecast: {e}")
        info = {"model": "neutral", "reason": str(e)}
        scales = {}

    forecast = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "meta": info,
        "scale_bounds": [VOL_SCALE_LO, VOL_SCALE_HI],
        "symbols": scales,
    }
    payload = json.dumps(forecast, indent=2)
    with open(args.out, "w") as f:
        f.write(payload)
    print(f"[vol] wrote {args.out}  ({len(scales)} symbols)")

    n_hi = sum(1 for v in scales.values() if v["regime"] == "HIGH_VOL")
    n_lo = sum(1 for v in scales.values() if v["regime"] == "LOW_VOL")
    print(f"[vol] regimes -> HIGH:{n_hi}  LOW:{n_lo}  NORMAL:{len(scales)-n_hi-n_lo}")

    if not args.no_gist:
        _push_gist(payload)


if __name__ == "__main__":
    main()
