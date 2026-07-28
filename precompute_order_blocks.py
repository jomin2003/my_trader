"""Precompute Order Blocks from 5-min data (aggregates to 15-min for OB detection)."""
from __future__ import annotations
import argparse
import logging
from pathlib import Path
import pandas as pd
import numpy as np

log = logging.getLogger("precompute_ob")

ATR_PERIOD = 20
BREAK_ATR_MULT = 1.0
LOOKBACK_HIGH = 10
OB_MIN_HOUR = 9
OB_MAX_HOUR = 11
OB_MAX_MIN  = 0


def aggregate_15m(df):
    df = df.set_index("ts").copy()
    agg = df.resample("15min", origin="start_day", closed="left", label="left").agg({
        "open":"first","high":"max","low":"min","close":"last","volume":"sum"
    }).dropna().reset_index()
    agg = agg[agg["ts"].dt.time.between(pd.Timestamp("09:15").time(),
                                         pd.Timestamp("15:15").time())]
    return agg.reset_index(drop=True)


def wilder_atr(df, period=20):
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    prev = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h-l, np.abs(h-prev), np.abs(l-prev)])
    atr = np.zeros_like(tr)
    if len(tr) < period: return atr
    atr[period-1] = tr[:period].mean()
    for i in range(period, len(tr)):
        atr[i] = (atr[i-1]*(period-1) + tr[i]) / period
    return atr


def find_order_blocks(df15, symbol):
    obs = []
    if len(df15) < ATR_PERIOD + 5:
        return obs
    atr = wilder_atr(df15, ATR_PERIOD)
    df15 = df15.copy()
    df15["date"] = df15["ts"].dt.date
    for i in range(ATR_PERIOD, len(df15) - 3):
        bar = df15.iloc[i]
        t = bar["ts"]
        if not (OB_MIN_HOUR <= t.hour and (t.hour, t.minute) <= (OB_MAX_HOUR, OB_MAX_MIN)):
            continue
        if atr[i] <= 0: continue
        next3 = df15.iloc[i+1:i+4]
        prior10 = df15.iloc[max(0, i-LOOKBACK_HIGH):i]
        if len(prior10) < 3 or len(next3) < 3: continue
        move_up = next3["close"].max() - bar["close"]
        if (move_up > BREAK_ATR_MULT * atr[i]) and (next3["high"].max() > prior10["high"].max()):
            if bar["close"] < bar["open"]:
                obs.append({"symbol":symbol,"date":bar["date"].isoformat(),"ob_type":"BULL",
                    "ob_time":bar["ts"].strftime("%H:%M"),
                    "ob_body_high":round(max(bar["open"],bar["close"]),4),
                    "ob_body_low":round(min(bar["open"],bar["close"]),4),
                    "ob_high":round(bar["high"],4),"ob_low":round(bar["low"],4)})
                continue
        move_down = bar["close"] - next3["close"].min()
        if (move_down > BREAK_ATR_MULT * atr[i]) and (next3["low"].min() < prior10["low"].min()):
            if bar["close"] > bar["open"]:
                obs.append({"symbol":symbol,"date":bar["date"].isoformat(),"ob_type":"BEAR",
                    "ob_time":bar["ts"].strftime("%H:%M"),
                    "ob_body_high":round(max(bar["open"],bar["close"]),4),
                    "ob_body_low":round(min(bar["open"],bar["close"]),4),
                    "ob_high":round(bar["high"],4),"ob_low":round(bar["low"],4)})
    return obs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    csv_dir = Path(args.csv_dir).expanduser().resolve()
    skip = {"NIFTY","NIFTY50","NIFTY_50","ob_data","gap_data","orb_data","cpr_data","pairs"}
    all_obs = []
    for fp in sorted(csv_dir.glob("*.csv")):
        sym = fp.stem.upper()
        if sym in skip: continue
        try:
            df = pd.read_csv(fp); df["ts"] = pd.to_datetime(df["ts"])
            df = df.dropna(subset=["open","high","low","close"])
            df = df[df["volume"] > 0].reset_index(drop=True)
        except Exception as e:
            log.warning(f"Skip {fp.name}: {e}"); continue
        if df.empty: continue
        obs = find_order_blocks(aggregate_15m(df), sym)
        all_obs.extend(obs)
        log.info(f"  {sym}: {len(obs)} OBs")
    out_df = pd.DataFrame(all_obs)
    if out_df.empty: raise SystemExit("No OBs found.")
    out_df.to_csv(Path(args.out).expanduser().resolve(), index=False)
    log.info(f"Saved {len(out_df)} OBs")
    log.info(f"  Bull: {(out_df['ob_type']=='BULL').sum()}, Bear: {(out_df['ob_type']=='BEAR').sum()}")


if __name__ == "__main__":
    main()
