"""Precompute Gap-Fill metadata per (symbol, date). Keeps gaps 1.0%-3.0%."""
from __future__ import annotations
import argparse
import logging
from pathlib import Path
import numpy as np
import pandas as pd

log = logging.getLogger("precompute_gap")

GAP_MIN = 1.0
GAP_MAX = 3.0
ATR_PERIOD = 14


def aggregate_daily(df):
    d = df.set_index("ts").resample("1D").agg({
        "open":"first","high":"max","low":"min","close":"last","volume":"sum"
    }).dropna().reset_index()
    return d[d["volume"] > 0].reset_index(drop=True)


def wilder_atr(df, period=14):
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    prev = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h-l, np.abs(h-prev), np.abs(l-prev)])
    atr = np.zeros_like(tr)
    if len(tr) < period: return atr
    atr[period-1] = tr[:period].mean()
    for i in range(period, len(tr)):
        atr[i] = (atr[i-1]*(period-1) + tr[i]) / period
    return atr


def process_symbol(df, symbol):
    df = df.copy(); df["date"] = df["ts"].dt.date
    daily = aggregate_daily(df); daily["date"] = daily["ts"].dt.date
    daily["atr14"] = wilder_atr(daily, ATR_PERIOD)
    dclose = dict(zip(daily["date"], daily["close"]))
    datr = dict(zip(daily["date"], daily["atr14"]))
    ddates = sorted(dclose.keys())
    rows = []
    for date_val in sorted(df["date"].unique()):
        prev = [d for d in ddates if d < date_val]
        if not prev: continue
        pd_ = max(prev)
        prev_close = dclose.get(pd_); atr = datr.get(pd_)
        if not prev_close or prev_close <= 0 or not atr or atr <= 0: continue
        day = df[df["date"] == date_val].sort_values("ts")
        if day.empty: continue
        topen = float(day["open"].iloc[0])
        if topen <= 0: continue
        gap = abs(topen - prev_close) / prev_close * 100
        if gap < GAP_MIN or gap > GAP_MAX: continue
        rows.append({"symbol":symbol,"date":date_val.isoformat(),
            "prev_close":round(prev_close,2),"today_open":round(topen,2),
            "gap_pct":round(gap,3),"gap_dir":"UP" if topen>prev_close else "DOWN",
            "daily_atr":round(atr,3)})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    csv_dir = Path(args.csv_dir).expanduser().resolve()
    skip = {"NIFTY","NIFTY50","NIFTY_50","gap_data","ob_data","orb_data","cpr_data","pairs"}
    all_rows = []
    for fp in sorted(csv_dir.glob("*.csv")):
        sym = fp.stem.upper()
        if sym in skip: continue
        try:
            df = pd.read_csv(fp); df["ts"] = pd.to_datetime(df["ts"])
            df = df.dropna(subset=["open","high","low","close"])
            df = df[df["volume"] > 0].sort_values("ts").reset_index(drop=True)
        except Exception as e:
            log.warning(f"Skip {fp.name}: {e}"); continue
        if df.empty: continue
        rows = process_symbol(df, sym)
        all_rows.extend(rows)
        if rows: log.info(f"  {sym}: {len(rows)} gap-days")
    if not all_rows: raise SystemExit("No tradeable gaps (1-3%) found.")
    out_df = pd.DataFrame(all_rows)
    out_df.to_csv(Path(args.out).expanduser().resolve(), index=False)
    log.info(f"Saved {len(out_df)} gap-days | UP:{(out_df['gap_dir']=='UP').sum()} "
             f"DOWN:{(out_df['gap_dir']=='DOWN').sum()}")


if __name__ == "__main__":
    main()
