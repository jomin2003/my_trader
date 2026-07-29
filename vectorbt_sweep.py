"""
=====================================================================
TIER-1 #2 — VECTORBT FAST PARAMETER SWEEP  (research tool)
=====================================================================
Runs THOUSANDS of SL/target/threshold combinations in seconds against
your existing CSV bar data (the ./data folder csv_downloader.py fills),
so you can tune SL-width / RR regions far faster than the event-driven
param_sweep.py.

⚠️ THE GOLDEN RULE (every 2026 source repeats it):
   VECTORISED = RESEARCH.  EVENT-DRIVEN = REAL MONEY.
   Vectorized backtests are OPTIMISTIC about fills/slippage. Use this to
   NARROW the search space, then CONFIRM the winners in your event-driven
   backtest_harness.py (which models costs) before trusting anything.

What it sweeps (a simple, transparent MA-crossover proxy so the sweep is
fast and framework-agnostic):
   * FAST_MA x SLOW_MA crossover entries (stand-in for "a signal fired")
   * SL as ATR multiple            (KEXIT-style stop width)
   * TP as RR multiple of the SL   (target distance)
This is NOT your 4 strategies — it's a fast SURROGATE to find good
SL-width / RR regions per symbol, which you then port into live_config.

Output: sweep_vbt_<date>.csv ranked by a composite fitness + top-N table.

Usage (RESEARCH — Colab / GitHub Actions, NOT the Render bot):
    pip install "vectorbt>=0.26.0" pandas numpy
    python vectorbt_sweep.py --csv-dir ./data --top 20
    python vectorbt_sweep.py --csv-dir ./data --symbols RELIANCE,SBIN,INFY
"""
from __future__ import annotations

import argparse
import glob
import logging
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("vbt_sweep")

# ---- parameter grids (edit freely) ----
FAST_MA = [5, 9, 12]
SLOW_MA = [20, 30, 50]
ATR_LEN = 14
SL_ATR  = [1.0, 1.5, 2.0, 2.5]      # stop width in ATR
RR      = [1.2, 1.5, 2.0, 2.5, 3.0] # target = RR * stop
INIT_CASH = 100_000
FEES = 0.0006      # 6 bps ~ your taxes model
SLIPPAGE = 0.0003  # 3 bps each side


def _load_csv(path: Path) -> pd.DataFrame | None:
    try:
        df = pd.read_csv(path)
        df["ts"] = pd.to_datetime(df["ts"])
        df = df.dropna(subset=["open", "high", "low", "close"])
        df = df[df["volume"] > 0].sort_values("ts").reset_index(drop=True)
        return df if len(df) > max(SLOW_MA) + ATR_LEN + 5 else None
    except Exception as e:
        log.warning(f"skip {path.name}: {e}")
        return None


def _atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    prev = c.shift(1).fillna(c)
    tr = pd.concat([(h - l), (h - prev).abs(), (l - prev).abs()], axis=1).max(axis=1)
    return tr.rolling(length).mean()


def sweep_symbol(vbt, sym: str, df: pd.DataFrame) -> list[dict]:
    """Run the FAST_MA x SLOW_MA x SL_ATR x RR grid on one symbol."""
    close = df["close"].reset_index(drop=True)
    atr = _atr(df, ATR_LEN).reset_index(drop=True)
    rows = []
    for f in FAST_MA:
        for s in SLOW_MA:
            if f >= s:
                continue
            fast = close.rolling(f).mean()
            slow = close.rolling(s).mean()
            long_entries = (fast > slow) & (fast.shift(1) <= slow.shift(1))
            long_exits   = (fast < slow) & (fast.shift(1) >= slow.shift(1))
            for sl_mult in SL_ATR:
                sl_frac = (sl_mult * atr / close).clip(0.001, 0.05).fillna(0.01)
                for rr in RR:
                    tp_frac = (sl_frac * rr).clip(0.001, 0.2)
                    try:
                        pf = vbt.Portfolio.from_signals(
                            close,
                            entries=long_entries.fillna(False),
                            exits=long_exits.fillna(False),
                            sl_stop=sl_frac.values,
                            tp_stop=tp_frac.values,
                            init_cash=INIT_CASH,
                            fees=FEES, slippage=SLIPPAGE,
                            freq="5min",
                        )
                        trades = pf.trades
                        n = int(trades.count())
                        if n < 5:
                            continue
                        total_ret = float(pf.total_return())
                        sharpe = float(pf.sharpe_ratio()) if n > 2 else 0.0
                        wr = float(trades.win_rate()) if n else 0.0
                        rows.append({
                            "symbol": sym, "fast": f, "slow": s,
                            "sl_atr": sl_mult, "rr": rr,
                            "trades": n, "win_rate": round(wr * 100, 2),
                            "total_return_pct": round(total_ret * 100, 3),
                            "sharpe": round(sharpe, 3),
                        })
                    except Exception:
                        continue
    return rows


def fitness(row: dict) -> float:
    """Composite: reward return + sharpe + win-rate, penalise tiny samples."""
    n = row["trades"]
    if n < 5:
        return -999.0
    sample_pen = min(1.0, n / 30.0)
    return round(
        (row["total_return_pct"] * 0.5)
        + (row["sharpe"] * 10.0)
        + (row["win_rate"] * 0.1), 3
    ) * sample_pen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default="./data")
    ap.add_argument("--symbols", default="", help="comma list; default = all csvs")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--out", default=".")
    args = ap.parse_args()

    try:
        import vectorbt as vbt
    except Exception:
        raise SystemExit('vectorbt not installed. Run: pip install "vectorbt>=0.26.0"')

    csv_dir = Path(args.csv_dir)
    skip = {"NIFTY", "NIFTY50", "NIFTY_50", "gap_data", "ob_data",
            "orb_data", "cpr_data", "pairs"}
    if args.symbols:
        want = {s.strip().upper() for s in args.symbols.split(",")}
        files = [csv_dir / f"{s}.csv" for s in want]
    else:
        files = [Path(p) for p in glob.glob(str(csv_dir / "*.csv"))]

    all_rows = []
    for fp in sorted(files):
        sym = fp.stem.upper()
        if sym in skip or not fp.exists():
            continue
        df = _load_csv(fp)
        if df is None:
            continue
        rows = sweep_symbol(vbt, sym, df)
        all_rows.extend(rows)
        if rows:
            log.info(f"{sym}: {len(rows)} combos")

    if not all_rows:
        raise SystemExit("No results — check --csv-dir has bar CSVs.")

    res = pd.DataFrame(all_rows)
    res["fitness"] = res.apply(fitness, axis=1)
    res = res.sort_values("fitness", ascending=False).reset_index(drop=True)

    out = Path(args.out) / f"sweep_vbt_{datetime.now():%Y%m%d_%H%M}.csv"
    res.to_csv(out, index=False)

    agg = (res[res["trades"] >= 10]
           .groupby(["sl_atr", "rr"])
           .agg(mean_fitness=("fitness", "mean"),
                mean_ret=("total_return_pct", "mean"),
                mean_wr=("win_rate", "mean"),
                n=("symbol", "count"))
           .sort_values("mean_fitness", ascending=False)
           .head(10).round(2))

    print("\n===== TOP CONFIGS (per symbol) =====")
    cols = ["symbol", "fast", "slow", "sl_atr", "rr", "trades",
            "win_rate", "total_return_pct", "sharpe", "fitness"]
    print(res[cols].head(args.top).to_string(index=False))
    print("\n===== BEST SL_ATR / RR REGIONS (avg across symbols) =====")
    print(agg.to_string())
    print(f"\nSaved: {out}")
    print("\n⚠️  Confirm any winner in backtest_harness.py (event-driven, "
          "cost-modeled) before porting to live_config.")


if __name__ == "__main__":
    main()

