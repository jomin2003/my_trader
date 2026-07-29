"""
=====================================================================
TIER-1 #3 — QUANTSTATS TEAR-SHEET  (professional performance analytics)
=====================================================================
Turns your bot's trade blotter into an institutional-grade HTML report:
Sharpe, Sortino, max drawdown, monthly-returns heatmap, win rate, best/
worst, exposure — so you can MEASURE whether Kronos + adaptive exits are
actually helping your paper P&L.

Feed it trades via:
  1) A trades CSV (columns: exit_time/time + pnl) exported from your
     _COMPLETED_TRADES blotter or backtest_harness output.
  2) --demo to preview the format with synthetic data.

Bonus cohort splits (the whole point for you):
  --by-kexit  -> compare adaptive-exit trades vs the rest
  --by-kronos -> compare Kronos-agree trades vs the rest

Usage (RESEARCH — local / GitHub Actions, NOT the Render bot):
    pip install quantstats pandas numpy
    python quantstats_report.py --trades trades.csv --out report.html
    python quantstats_report.py --trades trades.csv --by-kexit --by-kronos
    python quantstats_report.py --demo

CSV expectations (auto-detected):
    time column : one of [exit_time, time, timestamp, date, close_time]
    pnl  column : one of [pnl, pnl_net, net, profit, pnl_rs]
    (optional)  : kexit, kronos, outcome, symbol   for cohort splits
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("qs_report")

INIT_CAPITAL = 100_000

_TIME_COLS = ["exit_time", "time", "timestamp", "date", "close_time"]
_PNL_COLS  = ["pnl", "pnl_net", "net", "profit", "pnl_rs"]


def _pick(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    low = {c.lower(): c for c in df.columns}
    for c in candidates:
        if c.lower() in low:
            return low[c.lower()]
    return None


def trades_to_daily_returns(df: pd.DataFrame,
                            init_capital: float = INIT_CAPITAL) -> pd.Series:
    """Build a DAILY returns series from a trades table (time + pnl)."""
    tcol = _pick(df, _TIME_COLS)
    pcol = _pick(df, _PNL_COLS)
    if pcol is None:
        raise SystemExit(f"No PnL column found. Looked for {_PNL_COLS}. "
                         f"Have: {list(df.columns)}")
    d = df.copy()
    d[pcol] = pd.to_numeric(d[pcol], errors="coerce").fillna(0.0)

    if tcol is not None:
        d[tcol] = pd.to_datetime(d[tcol], errors="coerce")
        d = d.dropna(subset=[tcol])
        if d.empty:
            raise SystemExit("All timestamps unparseable.")
        d["day"] = d[tcol].dt.date
    else:
        log.warning("No time column; synthesising 1 day per trade.")
        base = datetime.now().date()
        d["day"] = [base - timedelta(days=(len(d) - i)) for i in range(len(d))]

    daily_pnl = d.groupby("day")[pcol].sum().sort_index()
    equity = init_capital + daily_pnl.cumsum()
    prev = equity.shift(1).fillna(init_capital)
    rets = (equity - prev) / prev
    rets.index = pd.to_datetime(rets.index)
    rets.name = "returns"
    return rets


def _print_quick_stats(rets: pd.Series, label: str = ""):
    "Fallback text stats if quantstats isn't installed."
    if rets.empty:
        print(f"[{label}] no data"); return
    total = float((1 + rets).prod() - 1) * 100
    ann = float(rets.mean() * 252) * 100
    vol = float(rets.std() * np.sqrt(252)) * 100
    sharpe = (rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0
    eq = (1 + rets).cumprod()
    dd = float(((eq - eq.cummax()) / eq.cummax()).min()) * 100
    wr = float((rets > 0).mean()) * 100
    print(f"\n===== QUICK STATS {label} =====")
    print(f"  days           : {len(rets)}")
    print(f"  total return   : {total:+.2f}%")
    print(f"  annualised     : {ann:+.2f}%")
    print(f"  volatility ann : {vol:.2f}%")
    print(f"  sharpe (ann)   : {sharpe:.2f}")
    print(f"  max drawdown   : {dd:.2f}%")
    print(f"  win-day rate   : {wr:.1f}%")


def make_report(rets: pd.Series, out_html: str, title: str):
    try:
        import quantstats as qs
    except Exception:
        log.warning("quantstats not installed — printing quick stats instead.")
        _print_quick_stats(rets, title)
        return False
    try:
        qs.reports.html(rets, output=out_html, title=title)
        log.info(f"Wrote {out_html}")
        return True
    except Exception as e:
        log.warning(f"quantstats html failed ({e}); quick stats fallback.")
        _print_quick_stats(rets, title)
        return False


def _demo_trades(n: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    base = datetime.now().date() - timedelta(days=n)
    rows = []
    for i in range(n):
        pnl = rng.normal(60, 400)
        rows.append({
            "exit_time": (base + timedelta(days=i)).isoformat(),
            "pnl": round(pnl, 2),
            "kexit": "cap@0.30%" if i % 2 == 0 else "",
            "kronos": "agree(0.80)" if i % 3 == 0 else "na",
            "outcome": "TARGET" if pnl > 0 else "SL",
        })
    return pd.DataFrame(rows)


def _cohort_split(df: pd.DataFrame, col: str):
    """Yield (label, subframe) for a set/unset split on `col`."""
    if col not in df.columns:
        log.warning(f"column '{col}' not in trades — skipping split")
        return
    s = df[col].astype(str).str.lower()
    is_set = (df[col].astype(str).str.len() > 0) & (~s.isin(["na", "nan", "none", ""]))
    has = df[is_set]
    non = df[~is_set]
    yield f"{col}=SET ({len(has)})", has
    yield f"{col}=UNSET ({len(non)})", non


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", help="CSV with time + pnl columns")
    ap.add_argument("--out", default="quantstats_report.html")
    ap.add_argument("--title", default="Trading Bot — Paper Performance")
    ap.add_argument("--capital", type=float, default=INIT_CAPITAL)
    ap.add_argument("--by-kexit", action="store_true",
                    help="split metrics: adaptive-exit vs fixed")
    ap.add_argument("--by-kronos", action="store_true",
                    help="split metrics: Kronos-agree cohort vs rest")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo:
        df = _demo_trades()
        log.info("Using DEMO trades.")
    elif args.trades:
        df = pd.read_csv(args.trades)
        log.info(f"Loaded {len(df)} trades from {args.trades}")
    else:
        raise SystemExit("Pass --trades <csv> or --demo")

    rets = trades_to_daily_returns(df, args.capital)
    ok = make_report(rets, args.out, args.title)
    if not ok:
        _print_quick_stats(rets, "ALL")

    splits = []
    if args.by_kexit:
        splits.append("kexit")
    if args.by_kronos:
        splits.append("kronos")
    for col in splits:
        for label, sub in _cohort_split(df, col):
            if sub is None or sub.empty:
                continue
            try:
                r = trades_to_daily_returns(sub, args.capital)
                _print_quick_stats(r, label)
            except SystemExit:
                pass

    print("\nDone. Open the HTML in a browser for the full tear-sheet.")


if __name__ == "__main__":
    main()

