"""A/B test framework: run backtest variants (module-constant overrides) on a
symbol subset, compare per-strategy economics vs baseline.

Usage:
  python research/ab_test.py --symbols 15
  python research/ab_test.py --symbols 15 --variants orb,st,exits
"""
from __future__ import annotations
import argparse, copy, sys
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import backtest_harness as bh          # noqa: E402
import param_sweep as ps               # noqa: E402
import intraday_pattern_scanner_v2 as scn   # noqa: E402
import multi_strategy_live as msl      # noqa: E402
import strategy_exits as se            # noqa: E402
import vwap_reclaim_strategy as vws    # noqa: E402

# ---------------------------------------------------------------- variants
# Each variant: dict of (module, attr) -> value, applied on top of BASELINE.
# Values are snapshots of the current (baseline) code at import time.

def _profiles(sl_mult, rr):
    """Return a patched copy of strategy_exits._PROFILES."""
    profs = copy.deepcopy(se._PROFILES)
    for name in profs:
        profs[name]["sl_mult"] = sl_mult
        profs[name]["rr"] = rr
    return profs


def variant_exits_tight():   # SL 1.2xATR, RR 1.5  (more targets reachable)
    return [(se, "_PROFILES", _profiles(1.2, 1.5))]

def variant_exits_tight_rr2():  # SL 1.2xATR, RR 2.0
    return [(se, "_PROFILES", _profiles(1.2, 2.0))]

def variant_exits_mid():     # SL 1.5xATR, RR 1.5
    return [(se, "_PROFILES", _profiles(1.5, 1.5))]

def variant_orb():           # Concretum 5-min OR + first-candle direction
    return [(msl, "ORB_END", __import__("datetime").time(9, 20)),
            (msl, "ORB_FIRST_CANDLE_DIR", True),
            (msl, "ORB_MIN_RANGE_PCT", 0.0015),
            (msl, "ORB_MAX_RANGE_PCT", 0.010)]

def variant_vwap():          # research: reclaim edge in first 30-90 min
    return [(vws, "ENTRY_END", __import__("datetime").time(11, 30))]

def variant_st():            # research: intraday Supertrend (7, 2.0)
    return [(msl, "ST_LEN", 7), (msl, "ST_MULT", 2.0)]

def variant_gapgo():         # Vortex: >2% gaps don't extend; edge 09:45-10:30
    return [(msl, "GAPGO_MAX_PCT", 2.0),
            (msl, "GAPGO_ENTRY_START", (9, 45))]

def variant_cs():            # candle patterns: tighter confluence
    return [(msl, "CS_MIN_TA", 3)]

VARIANTS = {
    "exits_tight":   variant_exits_tight,
    "exits_tight_rr2": variant_exits_tight_rr2,
    "exits_mid":     variant_exits_mid,
    "orb":           variant_orb,
    "vwap":          variant_vwap,
    "st":            variant_st,
    "gapgo":         variant_gapgo,
    "cs":            variant_cs,
    # ---- round 2 ----
    "rvol_rank":     lambda: [(bh, "RANK_KEYS", ("vol_ratio", "score", "strength"))],
    "nifty_strict":  lambda: ([], {"nifty_strict": True}),
    "side_block":    lambda: [(msl, "SIDE_BLOCK", {("VWAP_RECLAIM", +1), ("SUPERTREND", -1)})],
    "side_block_cs": lambda: [(msl, "SIDE_BLOCK", {("VWAP_RECLAIM", +1), ("SUPERTREND", -1),
                                                   ("CANDLE_STRUCT", +1)})],
    "orb_relax":     lambda: [(msl, "ORB_VOL_MULT", 1.2), (msl, "ORB_REQUIRE_VWAP", False)],
}

# combination of the individually-winning variants gets tested in round 2
VARIANTS["combo"] = lambda: (variant_exits_tight() + variant_orb() +
                             variant_vwap() + variant_st() + variant_gapgo())
# round-3 combo: only the variants that individually improved the subset
VARIANTS["combo2"] = lambda: (
    [(msl, "SIDE_BLOCK", {("VWAP_RECLAIM", +1), ("SUPERTREND", -1), ("CANDLE_STRUCT", +1)}),
     (msl, "GAPGO_MAX_PCT", 2.0), (msl, "GAPGO_ENTRY_START", (9, 45))],
    {"nifty_strict": True})


def run(bars, nifty, trend, label, overrides, bt_kwargs=None):
    # reset cross-run mutable state: dedup sets are keyed (symbol,date,strategy)
    # and would otherwise suppress signals in every run after the first
    msl._TRADED.clear()
    vws.clear()
    msl.reload_tables()
    saved = []
    try:
        for mod, attr, val in overrides:
            saved.append((mod, attr, getattr(mod, attr)))
            setattr(mod, attr, val)
        if isinstance(overrides, tuple):
            overrides, bt_kwargs = overrides
        bt = {"nifty_strict": False, "nifty_gate_enabled": True}
        bt.update(bt_kwargs or {})
        res = bh.backtest(bars, top_per_bar=20, nifty_trend_map=trend, **bt)
    finally:
        for mod, attr, val in saved:
            setattr(mod, attr, val)
    s = bh.summarize(res, bh.INITIAL_CAPITAL)
    if s.get("trades", 0) == 0:
        print(f"{label:<18} NO TRADES")
        return s
    strat = {k: (v["count"], v["sum"]) for k, v in s.get("by_pattern", {}).items()}
    print(f"{label:<18} n={s['trades']:>3} wr={s['win_rate']:>5.1f}% pf={s['profit_factor']:>5.2f} "
          f"expR={s['expectancy_R']:>6.3f} net=Rs{s['total_pnl']:>8.0f} dd={s['max_drawdown_pct']:>4.1f}% "
          f"tgt={sum(1 for t in res.trades if t.outcome=='TARGET'):>3} sl={sum(1 for t in res.trades if t.outcome=='SL'):>3} "
          f"eod={sum(1 for t in res.trades if t.outcome=='SQUAREOFF'):>3}")
    for k in sorted(strat, key=lambda k: strat[k][1]):
        c, pnl = strat[k]
        print(f"    {k:<24} n={c:>3} net=Rs{pnl:>8.0f}")
    return s


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default="research/data")
    ap.add_argument("--symbols", type=int, default=15)
    ap.add_argument("--variants", default=",".join(VARIANTS.keys()))
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()

    csv_dir = Path(args.csv_dir)
    bars_all = bh.load_csv_bars(csv_dir)
    names = sorted(bars_all.keys())[:args.symbols]
    bars_all = {k: bars_all[k] for k in names}
    nifty = bh._read_bar_csv(csv_dir / "NIFTY.csv")
    if args.start:
        bars_all = ps.filter_bars_by_dates(bars_all, pd.Timestamp(args.start).date(), pd.Timestamp(args.end).date())
        nifty = ps.filter_nifty_by_dates(nifty, pd.Timestamp(args.start).date(), pd.Timestamp(args.end).date())
    trend = bh.build_nifty_trend(nifty, strict=False)
    print(f"symbols={len(bars_all)} {names[:5]}...")

    wanted = [v for v in args.variants.split(",") if v]
    run(bars_all, nifty, trend, "BASELINE", [])
    for v in wanted:
        if v in VARIANTS:
            out = VARIANTS[v]()
            if isinstance(out, tuple):          # (overrides, bt_kwargs)
                run(bars_all, nifty, trend, v, *out)
            else:
                run(bars_all, nifty, trend, v, out)
        else:
            print(f"unknown variant {v}")


if __name__ == "__main__":
    main()
