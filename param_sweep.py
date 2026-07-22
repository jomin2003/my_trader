"""
=====================================================================
 PARAMETER SWEEP for intraday_pattern_scanner_v2 + backtest_harness
---------------------------------------------------------------------
 What it does:
   * Loads bar data + NIFTY trend map ONCE (expensive step).
   * Iterates a Cartesian grid over the strategy's hyperparameters:
        - ATR_MULTIPLIER          (SL width)
        - RISK_REWARD_RATIO       (target multiple)
        - MIN_SCORE_TO_TRADE      (signal quality gate)
        - REQUIRE_CONFIRMATION    (True/False)
        - MIN_TURNOVER_LAKHS      (liquidity floor)
        - MIN_SL_PCT / MAX_SL_PCT (SL clamps)
        - NIFTY gate mode         ('off' | 'soft' | 'strict')
        - top_per_bar             (breadth of signal ranking)
   * For each combo, monkey-patches the scanner module's globals and
     runs the backtest.
   * Computes a composite FITNESS score that balances:
        - expectancy in R
        - profit factor
        - sample size (penalises tiny-trade lucky flukes)
        - drawdown penalty
   * Ranks configs, saves CSV, warns about overfitting.
   * Optional walk-forward validation (train on first X%, test on rest)
     to catch curve-fitting before it burns real capital.

 Usage:
   python param_sweep.py --mode csv --csv-dir ./data \\
       --nifty-csv ./data/NIFTY.csv --preset default --out ./sweep_out

   # Custom grid via JSON file
   python param_sweep.py --mode csv --csv-dir ./data \\
       --grid ./my_grid.json --out ./sweep_out

   # Walk-forward: train on first 70% of dates, test on last 30%
   python param_sweep.py --mode csv --csv-dir ./data --walk-forward 0.7

 CRITICAL: parameter sweeps overfit. See warnings at the bottom of the
 report. Only trust results that also survive walk-forward or a
 fresh out-of-sample month.
=====================================================================
"""
from __future__ import annotations

import argparse
import io
import itertools
import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import intraday_pattern_scanner_v2 as scn
import backtest_harness as bh

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sweep")


# =====================================================================
# PRESET GRIDS
# =====================================================================
PRESETS: dict[str, dict] = {
    # ~ 3 * 3 * 3 * 2 * 3 = 162 runs; fast enough on any laptop
    "default": {
        "ATR_MULTIPLIER":       [1.0, 1.5, 2.0],
        "RISK_REWARD_RATIO":    [1.5, 2.0, 2.5],
        "MIN_SCORE_TO_TRADE":   [6, 7, 8],
        "REQUIRE_CONFIRMATION": [True, False],
        "nifty_gate":           ["off", "soft", "strict"],
        "MIN_TURNOVER_LAKHS":   [25],
        "MIN_SL_PCT":           [0.003],
        "MAX_SL_PCT":           [0.015],
        "top_per_bar":          [30],
    },
    # Quick sanity run (~ 12 combos)
    "quick": {
        "ATR_MULTIPLIER":       [1.5, 2.0],
        "RISK_REWARD_RATIO":    [2.0],
        "MIN_SCORE_TO_TRADE":   [6, 7, 8],
        "REQUIRE_CONFIRMATION": [True],
        "nifty_gate":           ["off", "soft"],
        "MIN_TURNOVER_LAKHS":   [25],
        "MIN_SL_PCT":           [0.003],
        "MAX_SL_PCT":           [0.015],
        "top_per_bar":          [30],
    },
    # Wide search (~ 1000+ combos; use only with long CSV history)
    "wide": {
        "ATR_MULTIPLIER":       [0.8, 1.0, 1.25, 1.5, 2.0, 2.5],
        "RISK_REWARD_RATIO":    [1.5, 2.0, 2.5, 3.0],
        "MIN_SCORE_TO_TRADE":   [5, 6, 7, 8, 9],
        "REQUIRE_CONFIRMATION": [True, False],
        "nifty_gate":           ["off", "soft", "strict"],
        "MIN_TURNOVER_LAKHS":   [10, 25, 50],
        "MIN_SL_PCT":           [0.002, 0.003],
        "MAX_SL_PCT":           [0.010, 0.015, 0.020],
        "top_per_bar":          [20, 30],
    },
}


# =====================================================================
# CONFIG CONTEXT MANAGER (safe monkey-patch + restore)
# =====================================================================
_SCANNER_ATTRS = [
    "ATR_MULTIPLIER", "RISK_REWARD_RATIO", "MIN_SCORE_TO_TRADE",
    "REQUIRE_CONFIRMATION", "MIN_TURNOVER_LAKHS",
    "MIN_SL_PCT", "MAX_SL_PCT",
]


class ScannerConfig:
    """Context manager: temporarily override scanner constants."""
    def __init__(self, **overrides):
        self.overrides = {k: v for k, v in overrides.items() if k in _SCANNER_ATTRS}
        self.original = {}

    def __enter__(self):
        for k, v in self.overrides.items():
            self.original[k] = getattr(scn, k)
            setattr(scn, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.original.items():
            setattr(scn, k, v)


# =====================================================================
# FITNESS FUNCTION (composite score, penalises noisy results)
# =====================================================================
def fitness(summary: dict, min_trades: int = 30) -> float:
    """
    Higher is better. Combines expectancy, profit factor, sample size,
    and a soft drawdown penalty. Configs with fewer than `min_trades`
    are heavily discounted -- they're statistical noise.
    """
    n = summary.get("trades", 0)
    if n <= 0:
        return -999.0

    exp_r = summary.get("expectancy_R", 0.0) or 0.0
    pf    = summary.get("profit_factor", 0.0) or 0.0
    dd    = summary.get("max_drawdown_pct", 0.0) or 0.0

    # Cap profit_factor at 5 to avoid infinities dominating rank
    pf_c = min(pf, 5.0) if pf != float("inf") else 5.0

    # Sample-size credibility (0..1). At n=min_trades -> ~0.5, at 3*min -> ~0.95
    cred = n / (n + min_trades)

    # Drawdown penalty: linear until 20%, then steeper
    dd_pen = max(0.0, 1.0 - dd / 20.0)

    # Composite: reward positive expectancy, punish DD, weight by credibility
    return round((exp_r * 2.0 + (pf_c - 1.0)) * cred * dd_pen, 4)


# =====================================================================
# DATA HELPERS
# =====================================================================
def _dhan_client():
    from dhanhq import DhanContext, dhanhq
    import os
    cid = os.getenv("DHAN_CLIENT_ID")
    tok = os.getenv("DHAN_ACCESS_TOKEN")
    if not cid or not tok:
        raise SystemExit("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN env vars")
    return dhanhq(DhanContext(cid, tok))


def load_data_dhan(days: int, max_symbols: int, sleep: float):
    """Fetch symbol bars + NIFTY once."""
    import requests
    dhan = _dhan_client()

    # ---- universe ----
    log.info("Downloading Dhan instrument master ...")
    resp = requests.get(scn.INSTRUMENT_MASTER_URL, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), low_memory=False)
    cols = {c.upper(): c for c in df.columns}
    C = lambda n: cols[n.upper()]
    eq = df[
        (df[C("SEM_EXM_EXCH_ID")].astype(str).str.upper() == "NSE") &
        (df[C("SEM_SEGMENT")].astype(str).str.upper() == "E") &
        (df[C("SEM_INSTRUMENT_NAME")].astype(str).str.upper() == "EQUITY")
    ].copy()
    if scn.USE_FNO_UNIVERSE_ONLY:
        fno = df[
            (df[C("SEM_EXM_EXCH_ID")].astype(str).str.upper() == "NSE") &
            (df[C("SEM_INSTRUMENT_NAME")].astype(str).str.upper().isin(["FUTSTK", "OPTSTK"]))
        ]
        under = fno[C("SEM_TRADING_SYMBOL")].astype(str).str.split("-").str[0].str.upper().unique()
        eq = eq[eq[C("SEM_TRADING_SYMBOL")].astype(str).str.upper().isin(under)]
    eq = eq.drop_duplicates(subset=[C("SEM_TRADING_SYMBOL")]).head(max_symbols)

    to_d = datetime.now(bh.IST).strftime("%Y-%m-%d")
    from_d = (datetime.now(bh.IST) - timedelta(days=days)).strftime("%Y-%m-%d")

    # NIFTY
    log.info("Fetching NIFTY 50 index bars ...")
    nifty_df = None
    try:
        r = dhan.intraday_minute_data(
            security_id="13", exchange_segment="IDX_I",
            instrument_type="INDEX",
            from_date=from_d, to_date=to_d, interval=5)
        data = r.get("data", r) if isinstance(r, dict) else {}
        if data and data.get("open"):
            nifty_df = pd.DataFrame({
                "open":data["open"], "high":data["high"], "low":data["low"],
                "close":data["close"],
                "volume":data.get("volume", [0]*len(data["open"]))})
            ts = data.get("timestamp") or data.get("startTime")
            if ts:
                nifty_df["ts"] = pd.to_datetime(ts, unit="s", utc=True).tz_convert(bh.IST)
                nifty_df = nifty_df.sort_values("ts").reset_index(drop=True)
    except Exception as e:
        log.warning(f"NIFTY fetch failed: {e}")

    log.info(f"Fetching bars {from_d}->{to_d} for {len(eq)} symbols")
    bars = {}
    for i, row in enumerate(eq.itertuples(index=False)):
        try:
            r = dhan.intraday_minute_data(
                security_id=str(row[eq.columns.get_loc(C("SEM_SMST_SECURITY_ID"))]),
                exchange_segment="NSE_EQ", instrument_type="EQUITY",
                from_date=from_d, to_date=to_d, interval=5)
            data = r.get("data", r) if isinstance(r, dict) else {}
            if data and data.get("open"):
                sdf = pd.DataFrame({
                    "open":data["open"], "high":data["high"], "low":data["low"],
                    "close":data["close"],
                    "volume":data.get("volume", [0]*len(data["open"]))})
                ts = data.get("timestamp") or data.get("startTime")
                if ts:
                    sdf["ts"] = pd.to_datetime(ts, unit="s", utc=True).tz_convert(bh.IST)
                    sdf = sdf.sort_values("ts").reset_index(drop=True)
                    if len(sdf) >= scn.MIN_CANDLES_NEEDED:
                        sym = str(row[eq.columns.get_loc(C("SEM_TRADING_SYMBOL"))])
                        bars[sym] = sdf
        except Exception:
            pass
        time.sleep(sleep)
        if (i+1) % 25 == 0:
            log.info(f"  fetched {i+1}/{len(eq)} (kept {len(bars)})")
    return bars, nifty_df


def load_data_csv(csv_dir: Path, nifty_csv: Path | None):
    bars = bh.load_csv_bars(csv_dir, skip_names={"NIFTY", "NIFTY50", "NIFTY_50"})
    nifty_df = None
    if nifty_csv is None:
        for cand in ("NIFTY.csv", "NIFTY50.csv", "NIFTY_50.csv"):
            p = csv_dir / cand
            if p.exists():
                nifty_csv = p; break
    if nifty_csv and Path(nifty_csv).exists():
        try:
            nifty_df = bh._read_bar_csv(Path(nifty_csv))
            log.info(f"Loaded NIFTY bars: {len(nifty_df)} rows")
        except Exception as e:
            log.warning(f"NIFTY CSV read failed: {e}")
    return bars, nifty_df


# =====================================================================
# WALK-FORWARD SPLIT
# =====================================================================
def split_dates(bars: dict, ratio: float) -> tuple[set, set]:
    """Split unique trading dates into (train, test) by ratio."""
    all_dates = sorted({ts.date()
                        for df in bars.values() for ts in df["ts"]})
    if not all_dates:
        return set(), set()
    cut = max(1, int(len(all_dates) * ratio))
    return set(all_dates[:cut]), set(all_dates[cut:])


def filter_bars_by_dates(bars: dict, keep_dates: set) -> dict:
    out = {}
    for sym, df in bars.items():
        sub = df[df["ts"].dt.date.isin(keep_dates)].reset_index(drop=True)
        if len(sub) >= scn.MIN_CANDLES_NEEDED:
            out[sym] = sub
    return out


def filter_nifty_by_dates(nifty: pd.DataFrame | None, keep_dates: set):
    if nifty is None or nifty.empty: return nifty
    return nifty[nifty["ts"].dt.date.isin(keep_dates)].reset_index(drop=True)


# =====================================================================
# SWEEP CORE
# =====================================================================
def grid_iter(grid: dict):
    """Yield dicts covering the Cartesian product of grid values."""
    keys = list(grid.keys())
    for combo in itertools.product(*[grid[k] for k in keys]):
        yield dict(zip(keys, combo))


def run_one(bars: dict, nifty_trend_soft: dict, nifty_trend_strict: dict,
            params: dict) -> dict:
    """Run one backtest with the given params. Returns summary + params."""
    gate = params.pop("nifty_gate")   # off / soft / strict
    top  = params.pop("top_per_bar")

    if gate == "strict":
        trend_map = nifty_trend_strict
        gate_on, strict = True, True
    elif gate == "soft":
        trend_map = nifty_trend_soft
        gate_on, strict = True, False
    else:
        trend_map = {}
        gate_on, strict = False, False

    with ScannerConfig(**params):
        res = bh.backtest(bars, top_per_bar=top,
                          nifty_trend_map=trend_map,
                          nifty_strict=strict,
                          nifty_gate_enabled=gate_on)
    summary = bh.summarize(res, bh.INITIAL_CAPITAL)
    row = {**params, "nifty_gate": gate, "top_per_bar": top, **summary}
    row["fitness"] = fitness(summary)
    return row


def sweep(bars: dict, nifty_df: pd.DataFrame | None, grid: dict) -> pd.DataFrame:
    log.info(f"Preparing NIFTY trend maps (soft + strict) ...")
    # Both maps are structurally identical; strictness is applied at gate time.
    # Building once is fine.
    trend_map = bh.build_nifty_trend(nifty_df, strict=False) if nifty_df is not None else {}

    combos = list(grid_iter(grid))
    log.info(f"Sweeping {len(combos)} configurations ...")
    rows = []
    t0 = time.time()
    for k, params in enumerate(combos, 1):
        try:
            row = run_one(bars, trend_map, trend_map, dict(params))
            rows.append(row)
        except Exception as e:
            log.warning(f"combo {k} failed: {e}")
        if k % max(1, len(combos)//20) == 0 or k == len(combos):
            elapsed = time.time() - t0
            rate = k / max(elapsed, 1e-9)
            eta = (len(combos) - k) / max(rate, 1e-9)
            log.info(f"  {k}/{len(combos)} done | {rate:.1f}/s | ETA {eta:.0f}s")

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("fitness", ascending=False).reset_index(drop=True)
    return df


# =====================================================================
# REPORTING
# =====================================================================
DISPLAY_COLS = [
    "fitness", "trades", "win_rate", "profit_factor",
    "expectancy_R", "expectancy_rs", "total_pnl", "return_pct",
    "max_drawdown_pct", "sharpe_annual",
    "ATR_MULTIPLIER", "RISK_REWARD_RATIO", "MIN_SCORE_TO_TRADE",
    "REQUIRE_CONFIRMATION", "MIN_TURNOVER_LAKHS",
    "MIN_SL_PCT", "MAX_SL_PCT", "top_per_bar", "nifty_gate",
]


def print_top(df: pd.DataFrame, k: int = 10, label: str = "TOP"):
    if df.empty:
        print(f"\n[{label}] no results")
        return
    cols = [c for c in DISPLAY_COLS if c in df.columns]
    print(f"\n============ {label} {k} CONFIGURATIONS ============")
    print(df[cols].head(k).to_string(index=False))


def robustness_check(df: pd.DataFrame) -> dict:
    """Sanity checks against curve-fitting."""
    checks = {}
    if df.empty:
        return {"note": "empty"}

    # 1. Best-vs-median gap
    med_fit = df["fitness"].median()
    best_fit = df["fitness"].iloc[0]
    checks["best_fitness"]   = round(best_fit, 3)
    checks["median_fitness"] = round(med_fit, 3)
    checks["best_over_median_ratio"] = round(best_fit / med_fit, 2) if med_fit > 0 else None

    # 2. How many configs are profitable
    prof = df[df.get("expectancy_R", 0) > 0]
    checks["configs_with_positive_expectancy"] = int(len(prof))
    checks["pct_configs_profitable"] = round(len(prof) / len(df) * 100, 1)

    # 3. Neighbourhood stability: does changing ONE parameter kill the edge?
    if not df.empty:
        top = df.iloc[0]
        neighbours = df.copy()
        # Configs sharing all but one param with the top
        shared_key_cols = ["ATR_MULTIPLIER", "RISK_REWARD_RATIO",
                           "MIN_SCORE_TO_TRADE", "REQUIRE_CONFIRMATION",
                           "nifty_gate"]
        neighbours["match"] = 0
        for c in shared_key_cols:
            if c in neighbours.columns:
                neighbours["match"] += (neighbours[c] == top[c]).astype(int)
        neigh = neighbours[neighbours["match"] == len(shared_key_cols) - 1]
        if not neigh.empty:
            checks["neighbour_median_fitness"] = round(neigh["fitness"].median(), 3)
            checks["neighbour_min_fitness"]    = round(neigh["fitness"].min(), 3)
    return checks


# =====================================================================
# WALK-FORWARD
# =====================================================================
def walk_forward(bars: dict, nifty_df, grid: dict, train_ratio: float):
    train_dates, test_dates = split_dates(bars, train_ratio)
    if not train_dates or not test_dates:
        raise SystemExit("Not enough dates for walk-forward split")

    log.info(f"WF split: train={len(train_dates)} dates | test={len(test_dates)} dates")

    tr_bars = filter_bars_by_dates(bars, train_dates)
    te_bars = filter_bars_by_dates(bars, test_dates)
    tr_ni   = filter_nifty_by_dates(nifty_df, train_dates)
    te_ni   = filter_nifty_by_dates(nifty_df, test_dates)

    log.info("Running sweep on TRAIN slice ...")
    tr_df = sweep(tr_bars, tr_ni, grid)
    if tr_df.empty:
        raise SystemExit("Train sweep produced no results")

    # Take top-5 configs from train and re-evaluate them on test
    log.info("Re-evaluating top 5 train configs on TEST slice ...")
    trend_te = bh.build_nifty_trend(te_ni, strict=False) if te_ni is not None else {}
    test_rows = []
    param_cols = [c for c in _SCANNER_ATTRS if c in tr_df.columns] + ["nifty_gate", "top_per_bar"]
    for _, top_row in tr_df.head(5).iterrows():
        params = {c: top_row[c] for c in param_cols if c in top_row}
        # cast dtypes to Python natives
        for k, v in list(params.items()):
            if isinstance(v, (np.integer,)): params[k] = int(v)
            elif isinstance(v, (np.floating,)): params[k] = float(v)
            elif isinstance(v, (np.bool_,)):    params[k] = bool(v)
        te_row = run_one(te_bars, trend_te, trend_te, dict(params))
        te_row["_train_fitness"] = top_row["fitness"]
        test_rows.append(te_row)
    te_df = pd.DataFrame(test_rows).sort_values("fitness", ascending=False)
    return tr_df, te_df


# =====================================================================
# ENTRY POINT
# =====================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["dhan", "csv"], required=True)
    ap.add_argument("--csv-dir", type=str, default="./data")
    ap.add_argument("--nifty-csv", type=str, default="")
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--max-symbols", type=int, default=80)
    ap.add_argument("--sleep", type=float, default=0.22)
    ap.add_argument("--preset", choices=list(PRESETS.keys()), default="default")
    ap.add_argument("--grid", type=str, default="",
                    help="Path to JSON grid (overrides --preset)")
    ap.add_argument("--walk-forward", type=float, default=0.0,
                    help="If >0 (e.g., 0.7), split dates into train/test by ratio")
    ap.add_argument("--top", type=int, default=15,
                    help="Print top-N configs to stdout")
    ap.add_argument("--out", type=str, default="./sweep_out")
    args = ap.parse_args()

    # ---- Load grid ----
    if args.grid:
        grid = json.loads(Path(args.grid).read_text())
    else:
        grid = PRESETS[args.preset]
    log.info(f"Grid: {json.dumps({k: v for k, v in grid.items()}, default=str)}")
    n_combos = 1
    for v in grid.values():
        n_combos *= len(v)
    log.info(f"Grid size: {n_combos} combinations")

    # ---- Load data ONCE ----
    if args.mode == "dhan":
        bars, nifty_df = load_data_dhan(args.days, args.max_symbols, args.sleep)
    else:
        nifty_csv = Path(args.nifty_csv) if args.nifty_csv else None
        bars, nifty_df = load_data_csv(Path(args.csv_dir).expanduser().resolve(), nifty_csv)
    log.info(f"Loaded {len(bars)} symbols | NIFTY bars: {0 if nifty_df is None else len(nifty_df)}")
    if not bars:
        raise SystemExit("No bar data available.")

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")

    # ---- Run sweep ----
    if args.walk_forward > 0:
        tr_df, te_df = walk_forward(bars, nifty_df, grid, args.walk_forward)
        tr_csv = out_dir / f"sweep_train_{tag}.csv"
        te_csv = out_dir / f"sweep_test_{tag}.csv"
        tr_df.to_csv(tr_csv, index=False)
        te_df.to_csv(te_csv, index=False)
        print_top(tr_df, args.top, label="TRAIN TOP")
        print_top(te_df, min(5, len(te_df)), label="OUT-OF-SAMPLE (from top-5 train)")
        checks = robustness_check(tr_df)
        print("\n============ ROBUSTNESS (train slice) ============")
        for k, v in checks.items(): print(f"  {k}: {v}")
        # Warn if OOS fitness collapses
        if not te_df.empty:
            oos_best = te_df["fitness"].iloc[0]
            in_best  = tr_df["fitness"].iloc[0]
            drop = (in_best - oos_best) / abs(in_best) * 100 if in_best else 0
            print(f"\n  in-sample best fitness: {in_best:.3f}")
            print(f"  out-of-sample best fitness: {oos_best:.3f}")
            if drop > 50:
                print(f"  ⚠️  OOS fitness collapsed by {drop:.0f}% — likely overfit")
            elif drop > 25:
                print(f"  ⚠️  OOS fitness dropped {drop:.0f}% — proceed cautiously")
            else:
                print(f"  ✅ OOS holds up (drop only {drop:.0f}%)")
        log.info(f"Saved: {tr_csv}\n         {te_csv}")
    else:
        df = sweep(bars, nifty_df, grid)
        csv_path = out_dir / f"sweep_{tag}.csv"
        df.to_csv(csv_path, index=False)
        print_top(df, args.top, label="TOP")
        checks = robustness_check(df)
        print("\n============ ROBUSTNESS ============")
        for k, v in checks.items(): print(f"  {k}: {v}")
        log.info(f"Saved: {csv_path}")

    # ---- Overfitting warning (always) ----
    print("\n" + "="*60)
    print("⚠️  OVERFITTING WARNING")
    print("="*60)
    print("A sweep this size (dozens–thousands of configs) will produce")
    print("a 'best' config that PARTIALLY reflects noise, not real edge.")
    print("Rules of thumb before trusting the winner:")
    print("  1. It must beat the median config by <2x (else overfit-y).")
    print("  2. It must have ≥100 trades in the tested window.")
    print("  3. Its parameter NEIGHBOURS (±1 grid step) must also be OK.")
    print("  4. It must survive walk-forward (--walk-forward 0.7).")
    print("  5. Do NOT go live on any config until it also wins on a")
    print("     FRESH month of data not touched during the sweep.")


if __name__ == "__main__":
    main()
