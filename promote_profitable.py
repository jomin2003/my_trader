"""
promote_profitable.py — THE profitable-only engine.
Runs the per-strategy backtest gate and PRINTS the suggested live allowlist
(set it on Render as ALLOWED_STRATEGIES; this script writes no files).

Gate (all required):
  PF >= 1.3, expectancy > +0.15R, n >= 30, 2/3 walk-forward folds PF > 1.0.
Usage:
  python promote_profitable.py --csv-dir data --min-trades 30
  ALLOWED_STRATEGIES="CANDLE_STRUCT" python -m pytest tests -q  # live honors it
Env overrides for Render:
  ALLOWED_STRATEGIES=CANDLE_STRUCT  (comma-separated, canonical names)
  PROFITABLE_ONLY=1
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import pandas as pd
import intraday_pattern_scanner_v2 as scn
import multi_strategy_live as msl
from backtest_harness import load_csv_bars, backtest, build_nifty_trend, _read_bar_csv

def canon(p: str) -> str:
    p = str(p)
    if "ORB" in p: return "ORB"
    if "GapFill" in p: return "GAPFILL"
    if "GapGo" in p: return "GAPGO"
    if "OB Bear" in p: return "OB_SHORTS"
    if "VWAP Reclaim" in p: return "VWAP_RECLAIM"
    if "VWAP Pullback" in p: return "VWAP_PULLBACK"
    if "Super" in p: return "SUPERTREND"
    return "CANDLE_STRUCT"

def evaluate(csv_dir: Path, min_trades: int):
    msl.reload_tables()
    bars = load_csv_bars(csv_dir, skip_names={"NIFTY"})
    nifty = _read_bar_csv(csv_dir/"NIFTY.csv")
    trend = build_nifty_trend(nifty)
    scn.MIN_SCORE_TO_TRADE = 8; scn.ADX_MIN_THRESHOLD = 25; msl.ADX_MIN_THRESHOLD = 25
    scn.MAX_OPEN_POSITIONS = 3
    res = backtest(bars, top_per_bar=30, nifty_trend_map=trend, nifty_gate_enabled=False)
    df = pd.DataFrame([t.__dict__ for t in res.trades]) if res.trades else pd.DataFrame()
    if df.empty:
        return {}, df
    df["strat"] = df["pattern"].apply(canon)
    out = {}
    for strat, g in df.groupby("strat"):
        n = len(g); wr = (g["pnl_net"] > 0).mean()
        expR = g["r_multiple"].mean()
        wsum = g[g.pnl_net > 0].pnl_net.sum(); lsum = g[g.pnl_net <= 0].pnl_net.sum()
        pf = (wsum / -lsum) if lsum < 0 else float("inf")
        # walk-forward: 3 date folds
        dates = sorted(pd.to_datetime(g["entry_time"]).dt.date.unique())
        folds = [dates[:len(dates)//3], dates[len(dates)//3:2*len(dates)//3], dates[2*len(dates)//3:]] if len(dates) >= 6 else []
        folds_ok = 0
        for dd in folds:
            gf = g[pd.to_datetime(g["entry_time"]).dt.date.isin(set(dd))]
            if len(gf) < 5: continue
            w2 = gf[gf.pnl_net > 0].pnl_net.sum(); l2 = gf[gf.pnl_net <= 0].pnl_net.sum()
            pf2 = (w2 / -l2) if l2 < 0 else float("inf")
            if pf2 > 1.0: folds_ok += 1
        ok = (pf >= 1.3) and (expR > 0.15) and (n >= min_trades) and (folds_ok >= 2)
        out[strat] = {"n": int(n), "wr": round(float(wr)*100, 1), "pf": round(float(pf), 2),
                      "expR": round(float(expR), 3), "pnl": round(float(g["pnl_net"].sum()), 0),
                      "folds_pf_gt1": folds_ok, "KEEP": bool(ok)}
    return out, df

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default="data")
    ap.add_argument("--min-trades", type=int, default=30)
    a = ap.parse_args()
    stats, _ = evaluate(Path(a.csv_dir), a.min_trades)
    if not stats:
        print("No trades — nothing to promote."); return
    print(f"{'strat':14s} {'n':>5s} {'WR%':>6s} {'PF':>6s} {'expR':>7s} {'pnl':>9s} folds verdict")
    keep = []
    for s, r in sorted(stats.items(), key=lambda kv: -kv[1]["pnl"]):
        v = "KEEP" if r["KEEP"] else "PRUNE"
        if r["KEEP"]: keep.append(s)
        print(f"{s:14s} {r['n']:5d} {r['wr']:6.1f} {r['pf']:6.2f} {r['expR']:+7.3f} {r['pnl']:+9.0f} {r['folds_pf_gt1']}/3 {v}")
    print("\nSet on Render: ALLOWED_STRATEGIES=" + (",".join(keep) if keep else "(empty — stay paper)"))

if __name__ == "__main__":
    main()
