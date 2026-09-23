"""Walk-forward A/B evaluator: runs backtest_harness on a date slice and
prints per-strategy economics. Usage:
  python research/eval_split.py --csv-dir research/data --start 2026-06-23 --end 2026-08-10 [--tag train]
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import backtest_harness as bh          # noqa: E402
import param_sweep as ps               # noqa: E402  (reuses date splitting)
import intraday_pattern_scanner_v2 as scn  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default="research/data")
    ap.add_argument("--start", required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument("--end", required=True, help="YYYY-MM-DD inclusive")
    ap.add_argument("--tag", default="")
    ap.add_argument("--strict", action="store_true", help="NIFTY strict gate")
    ap.add_argument("--side-block", default="", help='e.g. "VWAP_RECLAIM:+1,SUPERTREND:-1"')
    args = ap.parse_args()

    if args.side_block:
        import multi_strategy_live as msl
        for part in args.side_block.split(","):
            name, d = part.strip().rsplit(":", 1)
            msl.SIDE_BLOCK.add((msl.StrategyName.normalize(name), int(float(d))))

    csv_dir = Path(args.csv_dir)
    bars = bh.load_csv_bars(csv_dir)
    nifty = None
    nifty_path = csv_dir / "NIFTY.csv"
    if nifty_path.exists():
        nifty = bh._read_bar_csv(nifty_path)

    start, end = pd.Timestamp(args.start).date(), pd.Timestamp(args.end).date()
    keep = {d for df in bars.values() for d in
            (ts.date() for ts in df["ts"]) if start <= d <= end}
    bars = ps.filter_bars_by_dates(bars, keep)
    if nifty is not None:
        nifty = ps.filter_nifty_by_dates(nifty, keep)
    n_days = len({d for df in bars.values() for d in df["ts"].dt.date.unique()})
    print(f"[{args.tag or 'slice'}] {start}..{end}: {len(bars)} symbols, {n_days} days")

    trend = bh.build_nifty_trend(nifty, strict=False) if nifty is not None else {}
    res = bh.backtest(bars, top_per_bar=20, nifty_trend_map=trend,
                      nifty_strict=args.strict, nifty_gate_enabled=True)
    s = bh.summarize(res, bh.INITIAL_CAPITAL)

    print(f"  trades={s['trades']} wr={s['win_rate']:.1f}% pf={s['profit_factor']:.2f} "
          f"expR={s['expectancy_R']:.3f} net=Rs{s['total_pnl']:.0f} dd={s['max_drawdown_pct']:.1f}%")
    print("  by strategy:")
    for strat, st in sorted(s.get("by_pattern", {}).items(), key=lambda kv: -kv[1]["count"]):
        print(f"    {strat:<22} n={st['count']:>3} avg=Rs{st['mean']:>7.2f} "
              f"net=Rs{st['sum']:>8.0f}")
    # save trades for deeper diagnostics
    out = BASE / "research" / f"trades_{args.tag or 'slice'}.csv"
    if res.trades:
        pd.DataFrame([t.__dict__ for t in res.trades]).to_csv(out, index=False)
        print(f"  trades -> {out}")


if __name__ == "__main__":
    main()
