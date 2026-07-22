"""
=====================================================================
 MONTE CARLO PERMUTATION TEST for intraday_pattern_scanner_v2 pipeline
---------------------------------------------------------------------
 Answers TWO independent questions about your backtest:

 Q1. "Given my trade distribution, how lucky/unlucky was this equity
      curve?"   -> BOOTSTRAP TRADE RESAMPLING
      * Resamples the observed trades WITH REPLACEMENT N times.
      * Rebuilds equity curves, computes distribution of:
          final PnL, max drawdown, Sharpe, win rate.
      * Reports 5/50/95 percentiles + where the actual result lands.
      * Fast (millisecond-per-iter). Good for confidence intervals.

 Q2. "Is my strategy's edge better than RANDOM entries?"
      -> RANDOM-SIGNAL PERMUTATION
      * Reads the real trades' characteristics: how many trades per
        day, BUY/SELL ratio, session-hour distribution, symbol pool.
      * For each of N permutations, generates a SYNTHETIC signal set
        with the same statistical fingerprint but PICKED AT RANDOM.
      * Runs the SAME backtest engine (same SL/target/cost model).
      * Records the null distribution of expectancy_R.
      * Computes a one-sided p-value:
             p = P(random expectancy >= observed expectancy)
      * p < 0.05 => the strategy's edge is unlikely to be pure luck.
      * p > 0.20 => you probably curve-fit; DO NOT go live.

 Two answers you want:
    * Bootstrap: is the PnL stable, or does one lucky trade drive it?
    * Permutation: does the STRATEGY carry information, or would
      any random rule perform similarly?

 Usage:

   # Bootstrap only  (fast, needs just the trades CSV)
   python monte_carlo.py --trades ./bt_out/trades_20260722.csv \\
       --n-boot 2000 --out ./mc_out

   # Full test: bootstrap + permutation
   python monte_carlo.py --trades ./bt_out/trades_20260722.csv \\
       --csv-dir ./data --nifty-csv ./data/NIFTY.csv \\
       --n-boot 2000 --n-perms 200 --out ./mc_out
=====================================================================
"""
from __future__ import annotations

import argparse
import logging
import time
from dataclasses import asdict
from datetime import datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import intraday_pattern_scanner_v2 as scn
import backtest_harness as bh

IST = ZoneInfo("Asia/Kolkata")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mc")


# =====================================================================
# BOOTSTRAP: resample trade PnLs
# =====================================================================
def _equity_metrics(pnls: np.ndarray, initial_capital: float) -> dict:
    """Given an ordered array of trade PnLs, compute equity metrics."""
    equity = initial_capital + np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    dd_pct = (peak - equity) / peak
    max_dd = float(dd_pct.max()) if len(dd_pct) else 0.0

    total_pnl = float(pnls.sum())
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]
    win_rate = float(len(wins) / len(pnls)) if len(pnls) else 0.0
    pf = (float(wins.sum() / -losses.sum())
          if len(losses) and losses.sum() < 0 else float("inf"))

    # Per-trade "Sharpe" (mean/std), annualized rough: sqrt(252 * trades_per_day)
    # We don't have trades/day here so just report per-trade risk-adjusted PnL.
    mu, sd = float(pnls.mean()), float(pnls.std(ddof=1)) if len(pnls) > 1 else 0.0
    per_trade_sharpe = mu / sd if sd > 0 else 0.0

    return {
        "total_pnl":    total_pnl,
        "max_dd_pct":   round(max_dd * 100, 2),
        "win_rate":     round(win_rate * 100, 2),
        "profit_factor": round(min(pf, 999.0), 2),
        "avg_pnl":      round(mu, 2),
        "per_trade_sharpe": round(per_trade_sharpe, 3),
    }


def bootstrap_trades(trades_df: pd.DataFrame, n_iter: int = 2000,
                     initial_capital: float = 100_000,
                     seed: int = 42) -> dict:
    """
    Resample trade PnLs WITH REPLACEMENT n_iter times.
    Returns:
      observed:       metrics on the actual trade order
      distribution:   DataFrame with metrics for each resample
      percentiles:    5/50/95 percentiles of each metric
      observed_rank:  where the actual result lands in the sim distribution
    """
    if trades_df.empty:
        raise ValueError("No trades to bootstrap")

    rng = np.random.default_rng(seed)
    pnls = trades_df["pnl_net"].values.astype(float)
    n = len(pnls)

    observed = _equity_metrics(pnls, initial_capital)

    rows = []
    for _ in range(n_iter):
        sample = rng.choice(pnls, size=n, replace=True)
        rows.append(_equity_metrics(sample, initial_capital))
    dist = pd.DataFrame(rows)

    pcts = {}
    for c in ["total_pnl", "max_dd_pct", "win_rate", "profit_factor",
              "avg_pnl", "per_trade_sharpe"]:
        pcts[c] = {
            "p05": round(float(dist[c].quantile(0.05)), 2),
            "p50": round(float(dist[c].quantile(0.50)), 2),
            "p95": round(float(dist[c].quantile(0.95)), 2),
        }

    # Where does the observed result rank in the distribution?
    # (High rank = observed result was lucky)
    obs_rank = {}
    for c in ["total_pnl", "avg_pnl", "per_trade_sharpe"]:
        obs_rank[c] = round(100 * float((dist[c] < observed[c]).mean()), 1)
    # For DD: LOW rank means observed DD was worse than most sims
    obs_rank["max_dd_pct"] = round(100 * float((dist["max_dd_pct"] < observed["max_dd_pct"]).mean()), 1)

    return {
        "observed":      observed,
        "distribution":  dist,
        "percentiles":   pcts,
        "observed_rank": obs_rank,
        "n_trades":      n,
        "n_iter":        n_iter,
    }


# =====================================================================
# RANDOM-SIGNAL PERMUTATION
# =====================================================================
def _characterize_real_signals(trades_df: pd.DataFrame,
                               bars: dict) -> dict:
    """
    Extract statistical fingerprint of the real strategy's trades:
      - trades per date (histogram)
      - BUY / SELL ratio
      - entry-hour distribution (which 5-min slots does it enter?)
      - symbol pool (which symbols got traded)
    """
    if trades_df.empty:
        raise ValueError("Empty trades")
    df = trades_df.copy()
    df["entry_dt"] = pd.to_datetime(df["entry_time"])
    df["date"]    = df["entry_dt"].dt.date
    df["hour"]    = df["entry_dt"].dt.hour + df["entry_dt"].dt.minute / 60.0

    trades_per_date = df.groupby("date").size()
    buy_ratio = float((df["side"] == "BUY").mean())

    # Sample entry timestamps (hour+min bucket) to replicate the intraday
    # timing distribution the strategy exhibits
    entry_slots = df["entry_dt"].apply(lambda x: (x.hour, x.minute)).tolist()

    universe = sorted(bars.keys())
    traded_universe = sorted(df["symbol"].unique().tolist())

    return {
        "trades_per_date":  trades_per_date.to_dict(),
        "buy_ratio":        buy_ratio,
        "entry_slots":      entry_slots,
        "universe":         universe,
        "traded_universe":  traded_universe,
        "avg_trades_per_day": float(trades_per_date.mean()),
    }


def _synth_backtest(bars: dict, char: dict, nifty_trend_map: dict,
                    nifty_gate: bool, nifty_strict: bool,
                    rng: np.random.Generator) -> dict:
    """
    Run ONE permutation:
      - For each date in the real trade set, randomly pick that many
        symbols from the universe, randomly assign BUY/SELL matching
        the real ratio, and enter at a random 5-min bar drawn from
        the real entry-slot distribution.
      - Same SL / target / cost model as bh.
      - Same NIFTY gate.
    """
    # Reuse the exact SL/target/quantity logic from the scanner
    from backtest_harness import (
        _apply_costs, _simulate_bar, SQUAREOFF_TIME, INITIAL_CAPITAL,
    )

    equity = float(INITIAL_CAPITAL)
    trades = []
    universe = char["universe"]
    slots = char["entry_slots"]
    buy_ratio = char["buy_ratio"]
    max_open = scn.MAX_OPEN_POSITIONS

    # Group NIFTY trend map keys by date for fast per-date lookup
    n_by_date: dict = {}
    if nifty_trend_map:
        for ts, tv in nifty_trend_map.items():
            n_by_date.setdefault(ts.date(), []).append((ts, tv))
        for d in n_by_date:
            n_by_date[d].sort(key=lambda x: x[0])

    for date_val, n_trades_that_day in char["trades_per_date"].items():
        # Random symbol picks (without replacement — one entry per symbol/day)
        pool = [s for s in universe
                if any(ts.date() == date_val for ts in bars[s]["ts"])]
        if not pool:
            continue
        picks = rng.choice(pool, size=min(int(n_trades_that_day), len(pool)),
                           replace=False)

        open_pos = {}
        for sym in picks:
            df_sym = bars[sym]
            day_df = df_sym[df_sym["ts"].dt.date == date_val].reset_index(drop=True)
            if len(day_df) < 3:
                continue
            # Random slot from the empirical distribution
            hh, mm = slots[rng.integers(0, len(slots))]
            target_time = pd.Timestamp(datetime(date_val.year, date_val.month,
                                                date_val.day, hh, mm), tz=IST)
            # Find the closest bar >= target_time within the day
            after = day_df[day_df["ts"] >= target_time]
            if after.empty or after.iloc[0]["ts"].time() >= dtime(14, 30):
                continue
            entry_bar = after.iloc[0]
            entry_idx = day_df.index[day_df["ts"] == entry_bar["ts"]][0]
            if entry_idx + 1 >= len(day_df):
                continue
            next_bar = day_df.iloc[entry_idx + 1]
            entry_px = float(next_bar["open"])

            # Random direction matching real ratio
            direction = 1 if rng.random() < buy_ratio else -1

            # NIFTY gate check (same as live/backtest)
            if nifty_gate and n_by_date.get(date_val):
                # Find NIFTY trend at entry_bar time
                nt = 0
                for nts, ntv in n_by_date[date_val]:
                    if nts > entry_bar["ts"]:
                        break
                    nt = ntv
                if nifty_strict:
                    if not ((direction > 0 and nt == +1) or
                            (direction < 0 and nt == -1)):
                        continue
                else:
                    if (direction > 0 and nt < 0) or (direction < 0 and nt > 0):
                        continue

            # ATR for SL sizing — approximate from recent bars
            recent = day_df.iloc[:entry_idx + 1]
            if len(recent) < 5:
                continue
            atr_val = scn.wilder_atr(recent, 14) if len(recent) >= 15 else None

            sl, tgt, sl_dist = scn.compute_sl_target(entry_px, direction, atr_val)
            qty = scn.compute_quantity(entry_px, sl_dist)
            if qty <= 0:
                continue

            side = "BUY" if direction > 0 else "SELL"
            if len(open_pos) >= max_open:
                continue

            # Walk forward day_df from entry_idx+1 to resolve
            outcome, exit_px = "SQUAREOFF", float(day_df.iloc[-1]["close"])
            for j in range(entry_idx + 1, len(day_df)):
                r = _simulate_bar(day_df.iloc[j], entry_px, sl, tgt, side)
                if r is not None:
                    exit_px, outcome = r
                    break

            pnl = _apply_costs(entry_px, exit_px, qty, side)
            equity += pnl
            trades.append({
                "symbol": sym, "side": side, "outcome": outcome,
                "entry_price": entry_px, "exit_price": exit_px,
                "qty": qty, "pnl_net": pnl,
                "r_multiple": pnl / max(sl_dist * qty, 1e-9),
            })

    if not trades:
        return {"trades": 0, "expectancy_R": 0.0, "total_pnl": 0.0,
                "win_rate": 0.0, "profit_factor": 0.0}

    tdf = pd.DataFrame(trades)
    wins = tdf[tdf["pnl_net"] > 0]
    losses = tdf[tdf["pnl_net"] <= 0]
    pf = (float(wins["pnl_net"].sum() / -losses["pnl_net"].sum())
          if not losses.empty and losses["pnl_net"].sum() < 0 else float("inf"))
    return {
        "trades":        int(len(tdf)),
        "expectancy_R":  round(float(tdf["r_multiple"].mean()), 4),
        "total_pnl":     round(float(tdf["pnl_net"].sum()), 2),
        "win_rate":      round(float((tdf["pnl_net"] > 0).mean()) * 100, 2),
        "profit_factor": round(min(pf, 999.0), 2),
    }


def permutation_test(trades_df: pd.DataFrame, bars: dict,
                     nifty_df: pd.DataFrame | None,
                     n_perms: int = 200, nifty_gate: bool = True,
                     nifty_strict: bool = False, seed: int = 42) -> dict:
    """
    Compare observed strategy performance against a null distribution
    of RANDOM entries with the same statistical fingerprint.
    """
    if trades_df.empty:
        raise ValueError("Empty trades")

    log.info("Characterizing real strategy signals ...")
    char = _characterize_real_signals(trades_df, bars)

    trend_map = bh.build_nifty_trend(nifty_df, strict=False) if nifty_df is not None else {}
    gate_on = bool(nifty_gate and trend_map)

    # Observed metrics
    obs = {
        "trades":        int(len(trades_df)),
        "expectancy_R":  round(float(trades_df["r_multiple"].mean()), 4),
        "total_pnl":     round(float(trades_df["pnl_net"].sum()), 2),
        "win_rate":      round(float((trades_df["pnl_net"] > 0).mean()) * 100, 2),
    }

    log.info(f"Running {n_perms} random-signal permutations "
             f"(NIFTY gate: {'ON' if gate_on else 'OFF'}) ...")
    rng = np.random.default_rng(seed)
    rows = []
    t0 = time.time()
    for i in range(n_perms):
        rows.append(_synth_backtest(bars, char, trend_map,
                                    gate_on, nifty_strict, rng))
        if (i + 1) % max(1, n_perms // 10) == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / max(elapsed, 1e-9)
            eta = (n_perms - i - 1) / max(rate, 1e-9)
            log.info(f"  {i+1}/{n_perms} | {rate:.2f} perm/s | ETA {eta:.0f}s")

    null = pd.DataFrame(rows)

    # p-values (one-sided): P(random >= observed)
    p_vals = {}
    for c in ["expectancy_R", "total_pnl", "win_rate", "profit_factor"]:
        if c not in null.columns or c not in obs and c != "profit_factor":
            continue
        obs_v = obs.get(c, float("nan"))
        if c == "profit_factor" and c not in obs:
            # Compute observed PF
            wins = trades_df[trades_df["pnl_net"] > 0]["pnl_net"].sum()
            losses = -trades_df[trades_df["pnl_net"] <= 0]["pnl_net"].sum()
            obs_v = float(wins / losses) if losses > 0 else float("inf")
        p = float((null[c] >= obs_v).mean())
        p_vals[c] = round(p, 4)

    percentiles = {
        c: {
            "p05": round(float(null[c].quantile(0.05)), 3),
            "p50": round(float(null[c].quantile(0.50)), 3),
            "p95": round(float(null[c].quantile(0.95)), 3),
        }
        for c in ["expectancy_R", "total_pnl", "win_rate", "profit_factor"]
        if c in null.columns
    }

    return {
        "observed":     obs,
        "null_dist":    null,
        "percentiles":  percentiles,
        "p_values":     p_vals,
        "n_perms":      n_perms,
    }


# =====================================================================
# REPORTING
# =====================================================================
def print_bootstrap(res: dict):
    print("\n" + "=" * 62)
    print(" BOOTSTRAP TRADE RESAMPLING")
    print("=" * 62)
    print(f"  n_trades      : {res['n_trades']}")
    print(f"  n_iterations  : {res['n_iter']}")
    print("\n  Observed metrics:")
    for k, v in res["observed"].items():
        print(f"    {k:>20}: {v}")
    print("\n  Bootstrap distribution (p05 / p50 / p95):")
    for c, p in res["percentiles"].items():
        print(f"    {c:>20}: {p['p05']:>10} / {p['p50']:>10} / {p['p95']:>10}")
    print("\n  Observed percentile in distribution "
          "(50 = median, >95 = very lucky, <5 = very unlucky):")
    for c, r in res["observed_rank"].items():
        print(f"    {c:>20}: {r}%")


def print_permutation(res: dict):
    print("\n" + "=" * 62)
    print(" RANDOM-SIGNAL PERMUTATION TEST")
    print("=" * 62)
    print(f"  n_permutations : {res['n_perms']}")
    print("\n  Observed metrics:")
    for k, v in res["observed"].items():
        print(f"    {k:>20}: {v}")
    print("\n  Null distribution (random signals, p05 / p50 / p95):")
    for c, p in res["percentiles"].items():
        print(f"    {c:>20}: {p['p05']:>8} / {p['p50']:>8} / {p['p95']:>8}")
    print("\n  p-values (P[random >= observed], one-sided):")
    for c, p in res["p_values"].items():
        flag = "***" if p < 0.01 else "**" if p < 0.05 else "*" if p < 0.10 else ""
        print(f"    {c:>20}: {p:.4f}   {flag}")
    p_exp = res["p_values"].get("expectancy_R", 1.0)
    print("\n  VERDICT:")
    if p_exp < 0.05:
        print("    ✅ Strategy edge is STATISTICALLY SIGNIFICANT (p < 0.05).")
        print("       Random signals would beat this <5% of the time.")
    elif p_exp < 0.20:
        print("    ⚠️  Marginal edge (0.05 <= p < 0.20). Not clearly better")
        print("       than random. Test on more data before going live.")
    else:
        print("    ❌ NO detectable edge (p >= 0.20). Your strategy performs")
        print("       no better than random entries. DO NOT trade this live.")


# =====================================================================
# ENTRY POINT
# =====================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", required=True,
                    help="Path to trades CSV from backtest_harness")
    ap.add_argument("--csv-dir", type=str, default="",
                    help="Bar data folder (needed for --permutation)")
    ap.add_argument("--nifty-csv", type=str, default="",
                    help="Path to NIFTY.csv (used by permutation test)")
    ap.add_argument("--n-boot", type=int, default=2000,
                    help="Bootstrap iterations (0 = skip)")
    ap.add_argument("--n-perms", type=int, default=0,
                    help="Random-signal permutations (0 = skip). "
                         "Recommend 200 for a quick pass, 1000 for rigor.")
    ap.add_argument("--nifty-strict", action="store_true")
    ap.add_argument("--no-nifty-gate", action="store_true")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=str, default="./mc_out")
    args = ap.parse_args()

    # Load trades
    trades_path = Path(args.trades).expanduser().resolve()
    if not trades_path.exists():
        raise SystemExit(f"trades CSV not found: {trades_path}")
    trades = pd.read_csv(trades_path)
    log.info(f"Loaded {len(trades)} trades from {trades_path.name}")

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")

    # ---- BOOTSTRAP ----
    if args.n_boot > 0:
        boot = bootstrap_trades(trades, n_iter=args.n_boot, seed=args.seed)
        print_bootstrap(boot)
        boot["distribution"].to_csv(out_dir / f"bootstrap_{tag}.csv", index=False)
        log.info(f"Saved: {out_dir / f'bootstrap_{tag}.csv'}")

    # ---- PERMUTATION ----
    if args.n_perms > 0:
        if not args.csv_dir:
            raise SystemExit("Permutation test needs --csv-dir")
        csv_dir = Path(args.csv_dir).expanduser().resolve()
        bars = bh.load_csv_bars(csv_dir,
                                skip_names={"NIFTY", "NIFTY50", "NIFTY_50"})
        if not bars:
            raise SystemExit("No CSVs found for permutation test")
        nifty_df = None
        nifty_path = None
        if args.nifty_csv:
            nifty_path = Path(args.nifty_csv).expanduser().resolve()
        else:
            for cand in ("NIFTY.csv", "NIFTY50.csv"):
                p = csv_dir / cand
                if p.exists():
                    nifty_path = p; break
        if nifty_path and nifty_path.exists():
            try:
                nifty_df = bh._read_bar_csv(nifty_path)
            except Exception as e:
                log.warning(f"NIFTY CSV read failed: {e}")

        perm = permutation_test(
            trades, bars, nifty_df,
            n_perms=args.n_perms,
            nifty_gate=not args.no_nifty_gate,
            nifty_strict=args.nifty_strict,
            seed=args.seed,
        )
        print_permutation(perm)
        perm["null_dist"].to_csv(out_dir / f"permutation_{tag}.csv", index=False)
        log.info(f"Saved: {out_dir / f'permutation_{tag}.csv'}")

    if args.n_boot == 0 and args.n_perms == 0:
        raise SystemExit("Nothing to do. Set --n-boot and/or --n-perms.")


if __name__ == "__main__":
    main()
