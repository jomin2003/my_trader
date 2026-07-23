"""
=====================================================================
 BACKTEST HARNESS v3 (PATCHED) — for intraday_pattern_scanner_v2
---------------------------------------------------------------------
 Fixes vs v2:
   1. Uses live module references (scn.CONSTANT) instead of frozen
      imports. param_sweep can now actually change parameters between
      runs.
   2. Enforces MAX_OPEN_POSITIONS via a global counter (was ignored
      when >MAX_OPEN_POSITIONS signals ranked highly on the same bar).
   3. Realistic Dhan cost model: ₹0 brokerage on equity intraday MIS
      (Dhan's flat plan) instead of ₹20/leg.
   4. SL clamp raised so costs don't exceed max possible risk:
      MIN_SL_PCT=0.005 (0.5%) — matches actual intraday realism.
   5. Per-symbol daily cooldown fixed (was recording symbol even if
      the trade didn't actually enter due to position cap).

 This file is a DROP-IN REPLACEMENT for backtest_harness.py.
=====================================================================
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
import logging
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
import intraday_pattern_scanner_v2 as scn   # ← live reference, not frozen constants

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backtest")


# =====================================================================
# BACKTEST-ONLY CONSTANTS (not overridden by sweep)
# =====================================================================
INITIAL_CAPITAL      = 100_000
SQUAREOFF_TIME       = dtime(15, 15)

# ---- PATCHED: realistic Dhan MIS equity costs ----
BROKERAGE_PER_TRADE  = 0        # Dhan is ZERO brokerage on equity intraday
SLIPPAGE_BPS         = 3        # 0.03% each way — realistic for liquid F&O names
TAXES_BPS_ONEWAY     = 6        # STT+GST+SEBI+stamp ≈ 0.06%

# NIFTY gate
NIFTY_SECURITY_ID    = "13"
NIFTY_EXCH_SEGMENT   = "IDX_I"
NIFTY_INSTR_TYPE     = "INDEX"
NIFTY_EMA_SPAN       = 20

IST = ZoneInfo("Asia/Kolkata")


# =====================================================================
# DATA STRUCTS
# =====================================================================
@dataclass
class Trade:
    symbol: str; pattern: str; side: str
    entry_time: str; entry_price: float
    exit_time: str;  exit_price: float
    qty: int; sl_price: float; tgt_price: float
    outcome: str
    pnl_gross: float; pnl_net: float
    r_multiple: float; score: int; vol_ratio: float


@dataclass
class BacktestResult:
    trades:       list[Trade] = field(default_factory=list)
    equity_curve: list[tuple] = field(default_factory=list)
    stats:        dict        = field(default_factory=dict)


# =====================================================================
# CSV LOADERS
# =====================================================================
def _read_bar_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"])
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize(IST)
    else:
        df["ts"] = df["ts"].dt.tz_convert(IST)
    return df.sort_values("ts").reset_index(drop=True)


def load_csv_bars(csv_dir: Path, skip_names: set[str] | None = None) -> dict[str, pd.DataFrame]:
    out = {}
    skip = {s.upper() for s in (skip_names or set())}
    for p in sorted(Path(csv_dir).glob("*.csv")):
        sym = p.stem.upper()
        if sym in skip:
            continue
        try:
            df = _read_bar_csv(p)
            # ---- PATCHED: drop zero-volume bars (yfinance dirty data) ----
            df = df[df["volume"] > 0].reset_index(drop=True)
            if len(df) >= scn.MIN_CANDLES_NEEDED:
                out[sym] = df
        except Exception as e:
            log.warning(f"skip {p.name}: {e}")
    log.info(f"Loaded {len(out)} CSV symbols from {csv_dir}")
    return out


# =====================================================================
# NIFTY TREND GATE
# =====================================================================
def build_nifty_trend(nifty: pd.DataFrame, strict: bool = False) -> dict:
    if nifty is None or nifty.empty:
        return {}
    df = nifty.copy().reset_index(drop=True)
    df["date"] = df["ts"].dt.date

    tp  = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].astype(float).clip(lower=0)
    vwap_vals = np.full(len(df), np.nan)
    for _, grp_idx in df.groupby("date").groups.items():
        idx = list(grp_idx)
        t_g = tp.loc[idx]
        v_g = vol.loc[idx]
        if v_g.sum() <= 0:
            vwap_vals[idx] = t_g.expanding().mean().values
        else:
            cum_tp_v = (t_g * v_g).cumsum()
            cum_v    = v_g.cumsum().replace(0, np.nan).ffill()
            vwap_vals[idx] = (cum_tp_v / cum_v).values
    df["vwap"] = vwap_vals
    df["ema"]  = df["close"].ewm(span=NIFTY_EMA_SPAN, adjust=False).mean()

    trend = {}
    for _, r in df.iterrows():
        c, v, e = r["close"], r["vwap"], r["ema"]
        if pd.isna(v) or pd.isna(e):
            trend[r["ts"]] = 0
            continue
        if c > v and c > e:
            trend[r["ts"]] = +1
        elif c < v and c < e:
            trend[r["ts"]] = -1
        else:
            trend[r["ts"]] = 0
    return trend


def nifty_trend_at(ts, trend_map, sorted_ts):
    if not trend_map:
        return 0
    if ts in trend_map:
        return trend_map[ts]
    import bisect
    idx = bisect.bisect_right(sorted_ts, ts) - 1
    return trend_map[sorted_ts[idx]] if idx >= 0 else 0


def passes_nifty_gate(direction, ntrend, strict):
    if strict:
        return (direction > 0 and ntrend == +1) or (direction < 0 and ntrend == -1)
    return (direction > 0 and ntrend >= 0) or (direction < 0 and ntrend <= 0)


# =====================================================================
# COSTS & INTRA-BAR EXIT
# =====================================================================
def _apply_costs(entry_px, exit_px, qty, side):
    slip = SLIPPAGE_BPS / 10_000
    if side == "BUY":
        eff_entry = entry_px * (1 + slip)
        eff_exit  = exit_px  * (1 - slip)
        gross = (eff_exit - eff_entry) * qty
    else:
        eff_entry = entry_px * (1 - slip)
        eff_exit  = exit_px  * (1 + slip)
        gross = (eff_entry - eff_exit) * qty
    taxes = (entry_px + exit_px) * qty * (TAXES_BPS_ONEWAY / 10_000)
    return gross - taxes - BROKERAGE_PER_TRADE * 2


def _simulate_bar(bar, entry_price, sl, tgt, side):
    if bar["ts"].time() >= SQUAREOFF_TIME:
        return float(bar["close"]), "SQUAREOFF"
    h, l = bar["high"], bar["low"]
    if side == "BUY":
        sl_hit, tgt_hit = l <= sl, h >= tgt
        if sl_hit and tgt_hit: return sl, "SL"    # pessimistic
        if sl_hit:  return sl, "SL"
        if tgt_hit: return tgt, "TARGET"
    else:
        sl_hit, tgt_hit = h >= sl, l <= tgt
        if sl_hit and tgt_hit: return sl, "SL"
        if sl_hit:  return sl, "SL"
        if tgt_hit: return tgt, "TARGET"
    return None


def _confirmation_ok(hits, hist):
    if not scn.REQUIRE_CONFIRMATION or len(hist) < 2:
        return hits
    prev_h, prev_l = hist["high"].iloc[-2], hist["low"].iloc[-2]
    close_now = hist["close"].iloc[-1]
    return [h for h in hits
            if (h[0] > 0 and close_now > prev_h) or
               (h[0] < 0 and close_now < prev_l) or h[0] == 0]


def _in_entry_window(ts):
    t = ts.time()
    return scn.SCAN_START <= t <= scn.NO_ENTRY_AFTER


# =====================================================================
# CORE BACKTEST
# =====================================================================
def backtest(symbol_bars: dict[str, pd.DataFrame],
             top_per_bar: int = 30,
             nifty_trend_map: dict | None = None,
             nifty_strict: bool = False,
             nifty_gate_enabled: bool = True) -> BacktestResult:

    result = BacktestResult()
    equity = float(INITIAL_CAPITAL)

    sorted_nts = sorted(nifty_trend_map.keys()) if nifty_trend_map else []
    gate = bool(nifty_gate_enabled and sorted_nts)
    stats = {"raw_signals": 0, "nifty_rejected": 0,
             "position_cap_hits": 0, "cooldown_skips": 0}

    all_ts = sorted({ts for df in symbol_bars.values() for ts in df["ts"]})
    log.info(f"Backtesting {len(symbol_bars)} symbols across {len(all_ts)} bars "
             f"| NIFTY gate: {'ON' if gate else 'OFF'} "
             f"({'strict' if nifty_strict else 'soft'}) "
             f"| MAX_POS={scn.MAX_OPEN_POSITIONS} "
             f"| MIN_SCORE={scn.MIN_SCORE_TO_TRADE}")

    ts_index = {sym: {ts: i for i, ts in enumerate(df["ts"])}
                for sym, df in symbol_bars.items()}

    open_pos = {}      # symbol -> position dict
    traded = {}        # date -> set of symbols (cooldown)
    peak = equity

    for bar_ts in all_ts:
        dkey = bar_ts.date()
        traded.setdefault(dkey, set())

        # ---- (A) Resolve any open positions on this bar ----
        for sym in list(open_pos.keys()):
            pos = open_pos[sym]
            if bar_ts not in ts_index[sym]:
                continue
            j = ts_index[sym][bar_ts]
            if j < pos["entry_bar_idx"]:
                continue
            df = symbol_bars[sym]
            res = _simulate_bar(df.iloc[j], pos["entry_price"], pos["sl"],
                                pos["tgt"], pos["side"])
            if res is not None:
                exit_px, outcome = res
                pnl = _apply_costs(pos["entry_price"], exit_px, pos["qty"], pos["side"])
                equity += pnl
                rmult = pnl / max(pos["risk_amount"], 1e-9)
                sign = 1 if pos["side"] == "BUY" else -1
                result.trades.append(Trade(
                    symbol=sym, pattern=pos["pattern"], side=pos["side"],
                    entry_time=pos["entry_ts"].strftime("%Y-%m-%d %H:%M"),
                    entry_price=round(pos["entry_price"], 2),
                    exit_time=bar_ts.strftime("%Y-%m-%d %H:%M"),
                    exit_price=round(exit_px, 2),
                    qty=pos["qty"], sl_price=pos["sl"], tgt_price=pos["tgt"],
                    outcome=outcome,
                    pnl_gross=round((exit_px - pos["entry_price"]) * pos["qty"] * sign, 2),
                    pnl_net=round(pnl, 2), r_multiple=round(rmult, 2),
                    score=pos["score"], vol_ratio=pos["vol_ratio"],
                ))
                del open_pos[sym]

        if not _in_entry_window(bar_ts):
            result.equity_curve.append((bar_ts, equity))
            continue

        # ---- (B) Signal generation across all symbols ----
        sigs = []
        for sym, df in symbol_bars.items():
            if sym in open_pos or sym in traded[dkey]:
                continue
            if bar_ts not in ts_index[sym]:
                continue
            i = ts_index[sym][bar_ts]
            if i + 1 < scn.MIN_CANDLES_NEEDED:
                continue
            hist = df.iloc[: i + 1]
            hist = hist[hist["ts"].dt.date == dkey].reset_index(drop=True)
            if len(hist) < scn.MIN_CANDLES_NEEDED:
                continue
            hits = scn.detect_patterns(hist)
            if not hits:
                continue
            hits = _confirmation_ok(hits, hist)
            if not hits:
                continue
            sigs.extend(scn.score_signals(sym, sym, hist, hits))

        if not sigs:
            result.equity_curve.append((bar_ts, equity))
            continue

        stats["raw_signals"] += len(sigs)

        # ---- (C) NIFTY gate ----
        if gate:
            nt = nifty_trend_at(bar_ts, nifty_trend_map, sorted_nts)
            kept = []
            for s in sigs:
                if passes_nifty_gate(s["direction"], nt, nifty_strict):
                    kept.append(s)
                else:
                    stats["nifty_rejected"] += 1
            sigs = kept
        if not sigs:
            result.equity_curve.append((bar_ts, equity))
            continue

        ranked = (pd.DataFrame(sigs)
                  .sort_values(["score", "vol_ratio", "strength"],
                               ascending=[False, False, False])
                  .drop_duplicates("symbol", keep="first")
                  .head(top_per_bar))

        # ---- (D) Take entries at NEXT bar's OPEN, respecting position cap ----
        for _, sig in ranked.iterrows():
            if sig["score"] < scn.MIN_SCORE_TO_TRADE:
                continue

            # PATCHED: enforce position cap HERE (was checked but easily bypassed)
            if len(open_pos) >= scn.MAX_OPEN_POSITIONS:
                stats["position_cap_hits"] += 1
                break   # no more entries this bar

            sym = sig["symbol"]
            if sym in open_pos or sym in traded[dkey]:
                stats["cooldown_skips"] += 1
                continue

            i = ts_index[sym][bar_ts]
            df = symbol_bars[sym]
            if i + 1 >= len(df):
                continue
            nxt = df.iloc[i + 1]
            if nxt["ts"].date() != dkey:
                continue

            entry_px = float(nxt["open"])
            direction = 1 if sig["signal"] == "BUY" else -1
            sl, tgt, sl_dist = scn.compute_sl_target(entry_px, direction, sig.get("atr"))
            qty = scn.compute_quantity(entry_px, sl_dist)
            if qty <= 0:
                continue

            # PATCHED: only mark as traded if we actually enter
            open_pos[sym] = {
                "pattern": sig["pattern"], "side": sig["signal"],
                "entry_ts": nxt["ts"], "entry_price": entry_px,
                "sl": sl, "tgt": tgt, "qty": qty,
                "entry_bar_idx": i + 1, "risk_amount": sl_dist * qty,
                "score": int(sig["score"]), "vol_ratio": float(sig["vol_ratio"]),
            }
            traded[dkey].add(sym)

        peak = max(peak, equity)
        result.equity_curve.append((bar_ts, equity))

    result.stats = stats

    # Force-close remaining positions at last known bar
    for sym, pos in list(open_pos.items()):
        df = symbol_bars[sym]
        last = df.iloc[-1]
        pnl = _apply_costs(pos["entry_price"], float(last["close"]),
                           pos["qty"], pos["side"])
        sign = 1 if pos["side"] == "BUY" else -1
        result.trades.append(Trade(
            symbol=sym, pattern=pos["pattern"], side=pos["side"],
            entry_time=pos["entry_ts"].strftime("%Y-%m-%d %H:%M"),
            entry_price=round(pos["entry_price"], 2),
            exit_time=last["ts"].strftime("%Y-%m-%d %H:%M"),
            exit_price=round(float(last["close"]), 2),
            qty=pos["qty"], sl_price=pos["sl"], tgt_price=pos["tgt"],
            outcome="EOD_FORCED",
            pnl_gross=round((float(last["close"]) - pos["entry_price"]) *
                            pos["qty"] * sign, 2),
            pnl_net=round(pnl, 2),
            r_multiple=round(pnl / max(pos["risk_amount"], 1e-9), 2),
            score=pos["score"], vol_ratio=pos["vol_ratio"],
        ))
    return result


# =====================================================================
# SUMMARY
# =====================================================================
def summarize(res: BacktestResult, initial_capital=INITIAL_CAPITAL) -> dict:
    if not res.trades:
        return {"trades": 0, "note": "no trades", "stats": res.stats}
    df = pd.DataFrame([asdict(t) for t in res.trades])
    wins, losses = df[df["pnl_net"] > 0], df[df["pnl_net"] <= 0]
    pf = (float(wins["pnl_net"].sum() / -losses["pnl_net"].sum())
          if not losses.empty and losses["pnl_net"].sum() < 0 else float("inf"))
    eq = pd.DataFrame(res.equity_curve, columns=["ts", "equity"])
    if not eq.empty:
        eq["dd"] = (eq["equity"].cummax() - eq["equity"]) / eq["equity"].cummax()
        max_dd = eq["dd"].max()
        eq["date"] = pd.to_datetime(eq["ts"]).dt.date
        daily = eq.groupby("date")["equity"].last().pct_change().dropna()
        sharpe = (daily.mean()/daily.std()*(252**0.5)) if daily.std() > 0 else 0.0
    else:
        max_dd, sharpe = 0.0, 0.0
    return {
        "trades":          len(df),
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate":        round(len(wins)/len(df)*100, 2),
        "profit_factor":   round(min(pf, 999.0), 2),
        "expectancy_R":    round(df["r_multiple"].mean(), 3),
        "expectancy_rs":   round(df["pnl_net"].mean(), 2),
        "total_pnl":       round(df["pnl_net"].sum(), 2),
        "return_pct":      round(df["pnl_net"].sum()/initial_capital*100, 2),
        "max_drawdown_pct": round(max_dd*100, 2),
        "sharpe_annual":   round(sharpe, 2),
        "by_pattern":      df.groupby("pattern")["pnl_net"].agg(["count","sum","mean"]).round(2).to_dict("index"),
        "by_outcome":      df["outcome"].value_counts().to_dict(),
        "stats":           res.stats,
    }


# =====================================================================
# ENTRY POINT
# =====================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["csv"], required=True,
                    help="csv only in patched version (Dhan mode unchanged; use previous file)")
    ap.add_argument("--csv-dir", type=str, required=True)
    ap.add_argument("--nifty-csv", type=str, default="")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--no-nifty-gate", action="store_true")
    ap.add_argument("--nifty-strict", action="store_true")
    ap.add_argument("--out", type=str, default="./backtest_out")
    args = ap.parse_args()

    csv_dir = Path(args.csv_dir).expanduser().resolve()
    bars = load_csv_bars(csv_dir, skip_names={"NIFTY","NIFTY50","NIFTY_50"})
    if not bars:
        raise SystemExit("No bar CSVs found.")

    nifty_df = None
    nifty_path = Path(args.nifty_csv) if args.nifty_csv else csv_dir / "NIFTY.csv"
    if nifty_path.exists():
        nifty_df = _read_bar_csv(nifty_path)
        log.info(f"Loaded NIFTY: {len(nifty_df)} rows")

    trend_map = build_nifty_trend(nifty_df) if nifty_df is not None else {}
    result = backtest(bars, top_per_bar=args.top,
                      nifty_trend_map=trend_map,
                      nifty_strict=args.nifty_strict,
                      nifty_gate_enabled=(not args.no_nifty_gate) and bool(trend_map))

    summary = summarize(result)
    print("\n" + "="*50)
    print("BACKTEST SUMMARY")
    print("="*50)
    for k, v in summary.items():
        if isinstance(v, dict):
            print(f"\n{k}:")
            for kk, vv in v.items():
                print(f"  {kk}: {vv}")
        else:
            print(f"{k:>20}: {v}")

    # Save outputs
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = datetime.now().strftime("%Y%m%d_%H%M")
    if result.trades:
        pd.DataFrame([asdict(t) for t in result.trades]).to_csv(
            out_dir / f"trades_{tag}.csv", index=False)
    pd.DataFrame(result.equity_curve, columns=["ts","equity"]).to_csv(
        out_dir / f"equity_{tag}.csv", index=False)
    log.info(f"Saved trades + equity to {out_dir}")


if __name__ == "__main__":
    main()
