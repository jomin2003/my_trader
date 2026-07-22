"""
=====================================================================
 BACKTEST HARNESS for intraday_pattern_scanner_v2  (+ NIFTY gate)
---------------------------------------------------------------------
 What it does:
   * Loads historical 5-min OHLCV bars per symbol (Dhan API OR local CSV).
   * Loads NIFTY 50 index 5-min bars for the market-regime filter.
   * Walks BAR-BY-BAR (no look-ahead) and applies the EXACT same rules
     as the live scanner: detect_patterns + score_signals + VWAP +
     EMA20 + confirmation + liquidity + session gates + NIFTY gate.
   * Simulates entry at the NEXT bar's OPEN (realistic, no fill on
     the pattern bar itself).
   * Resolves SL/Target intra-bar with conservative assumptions:
       - If a single bar's [low..high] contains BOTH SL and Target,
         assume the WORST case (SL hit first).  This is the industry-
         standard pessimistic assumption for 5-min bars.
       - Otherwise, whichever level is inside the bar's range fills.
       - If neither hits until 15:15 IST, the trade is force-squared
         at the 15:15 bar's close (MIS auto-squareoff).
   * Enforces MAX_OPEN_POSITIONS, per-symbol cooldown, session filter.
   * Produces: trade blotter CSV, equity curve, and a summary report
     (win rate, expectancy, profit factor, max DD, Sharpe).

 Data sources:
   1. DHAN  -> intraday_minute_data() for last 5 trading days.
              NIFTY 50 spot is fetched with security_id="13",
              exchange_segment="IDX_I", instrument_type="INDEX".
   2. CSV   -> point --csv-dir at a folder of <SYMBOL>.csv files
              with columns: ts,open,high,low,close,volume  (ts = IST).
              Provide NIFTY as --nifty-csv <path>/NIFTY.csv (same schema;
              volume may be zero for the index — VWAP will fall back
              to typical price when needed).

 NIFTY trend gate (market-regime filter):
   * At every 5-min bar we compute NIFTY's INTRADAY VWAP (resets daily)
     and its 20-EMA on close.
   * Trend rules (default "soft"):
       - Bullish  (+1) : NIFTY close > VWAP AND close > EMA20
       - Bearish  (-1) : NIFTY close < VWAP AND close < EMA20
       - Neutral  ( 0) : mixed
   * BUY signals require trend >= 0 ; SELL signals require trend <= 0.
   * --nifty-strict makes it demand trend == +/-1 (rejects Neutral too).

 Usage:
   # Dhan mode  (auto-fetches NIFTY)
   python backtest_harness.py --mode dhan --days 5 --top 30

   # CSV mode
   python backtest_harness.py --mode csv --csv-dir ./data \\
       --nifty-csv ./data/NIFTY.csv --top 30

   # Disable the gate for A/B comparison
   python backtest_harness.py --mode csv --csv-dir ./data --no-nifty-gate
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

# Reuse the exact scanner logic so backtest & live can never drift.
# Assumes intraday_pattern_scanner_v2.py sits next to this file.
sys.path.insert(0, str(Path(__file__).parent))
from intraday_pattern_scanner_v2 import (            # noqa: E402
    detect_patterns, score_signals, wilder_atr,
    rolling_vwap, ema, compute_sl_target, compute_quantity,
    MIN_CANDLES_NEEDED, MIN_SCORE_TO_TRADE, RISK_REWARD_RATIO,
    MAX_RISK_PER_TRADE, MAX_CAPITAL_PER_TRADE, MAX_OPEN_POSITIONS,
    REQUIRE_CONFIRMATION, MIN_TURNOVER_LAKHS,
    SCAN_START, NO_ENTRY_AFTER, IST,
    INSTRUMENT_MASTER_URL, USE_FNO_UNIVERSE_ONLY,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backtest")

# ---------------------------------------------------------------------
# Backtest constants (override live only where realistic for backtest)
# ---------------------------------------------------------------------
INITIAL_CAPITAL      = 100_000        # ₹ starting equity
SQUAREOFF_TIME       = dtime(15, 15)  # MIS auto-squareoff
BROKERAGE_PER_TRADE  = 20             # ₹ flat (per leg entry+exit=40)
SLIPPAGE_BPS         = 3              # 3 basis points on entry & exit (0.03%)
# STT + exch txn + GST + SEBI + stamp ≈ 0.06% one-way for intraday equity
TAXES_BPS_ONEWAY     = 6

# ---- NIFTY trend gate ----
NIFTY_SECURITY_ID    = "13"           # NIFTY 50 spot on Dhan
NIFTY_EXCH_SEGMENT   = "IDX_I"
NIFTY_INSTR_TYPE     = "INDEX"
NIFTY_EMA_SPAN       = 20
NIFTY_GATE_DEFAULT   = True           # can be flipped from CLI --no-nifty-gate


# =====================================================================
# DATA STRUCTS
# =====================================================================
@dataclass
class Trade:
    symbol:        str
    pattern:       str
    side:          str
    entry_time:    str
    entry_price:   float
    exit_time:     str
    exit_price:    float
    qty:           int
    sl_price:      float
    tgt_price:     float
    outcome:       str          # "TARGET" | "SL" | "SQUAREOFF"
    pnl_gross:     float
    pnl_net:       float
    r_multiple:    float
    score:         int
    vol_ratio:     float


@dataclass
class BacktestResult:
    trades:        list[Trade]  = field(default_factory=list)
    equity_curve:  list[tuple]  = field(default_factory=list)  # (ts, equity)
    stats:         dict         = field(default_factory=dict)   # filter counters


# =====================================================================
# DATA LOADERS
# =====================================================================
def _dhan_client():
    from dhanhq import DhanContext, dhanhq
    cid = os.getenv("DHAN_CLIENT_ID")
    tok = os.getenv("DHAN_ACCESS_TOKEN")
    if not cid or not tok:
        raise SystemExit("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN env vars")
    return dhanhq(DhanContext(cid, tok))


def load_universe_dhan(max_stocks: int) -> pd.DataFrame:
    log.info("Downloading Dhan instrument master ...")
    resp = requests.get(INSTRUMENT_MASTER_URL, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), low_memory=False)
    cols = {c.upper(): c for c in df.columns}
    C = lambda n: cols[n.upper()]

    eq = df[
        (df[C("SEM_EXM_EXCH_ID")].astype(str).str.upper() == "NSE") &
        (df[C("SEM_SEGMENT")].astype(str).str.upper() == "E") &
        (df[C("SEM_INSTRUMENT_NAME")].astype(str).str.upper() == "EQUITY")
    ].copy()

    if USE_FNO_UNIVERSE_ONLY:
        fno = df[
            (df[C("SEM_EXM_EXCH_ID")].astype(str).str.upper() == "NSE") &
            (df[C("SEM_INSTRUMENT_NAME")].astype(str).str.upper().isin(["FUTSTK", "OPTSTK"]))
        ]
        under = fno[C("SEM_TRADING_SYMBOL")].astype(str).str.split("-").str[0].str.upper().unique()
        eq = eq[eq[C("SEM_TRADING_SYMBOL")].astype(str).str.upper().isin(under)]

    eq = eq.drop_duplicates(subset=[C("SEM_TRADING_SYMBOL")]).head(max_stocks)
    return pd.DataFrame({
        "security_id": eq[C("SEM_SMST_SECURITY_ID")].astype(str),
        "symbol":      eq[C("SEM_TRADING_SYMBOL")].astype(str),
    }).reset_index(drop=True)


def fetch_dhan_bars(dhan, security_id: str, from_d: str, to_d: str,
                    interval: int = 5) -> pd.DataFrame | None:
    try:
        resp = dhan.intraday_minute_data(
            security_id=security_id,
            exchange_segment="NSE_EQ",
            instrument_type="EQUITY",
            from_date=from_d, to_date=to_d,
            interval=interval,
        )
    except Exception as e:
        log.debug(f"[{security_id}] dhan fetch fail: {e}")
        return None

    if not isinstance(resp, dict):
        return None
    data = resp.get("data") if resp.get("data") else resp
    if not isinstance(data, dict) or not data.get("open"):
        return None

    df = pd.DataFrame({
        "open":  data["open"], "high": data["high"],
        "low":   data["low"],  "close": data["close"],
        "volume": data.get("volume", [0] * len(data["open"])),
    })
    ts = data.get("timestamp") or data.get("start_Time") or data.get("startTime")
    if ts:
        df["ts"] = pd.to_datetime(ts, unit="s", utc=True).tz_convert(IST)
    else:
        return None
    df = df.dropna(subset=["open", "high", "low", "close"]).sort_values("ts").reset_index(drop=True)
    return df


def load_csv_bars(csv_dir: Path, skip_names: set[str] | None = None) -> dict[str, pd.DataFrame]:
    out = {}
    skip_names = {s.upper() for s in (skip_names or set())}
    for p in sorted(csv_dir.glob("*.csv")):
        sym = p.stem.upper()
        if sym in skip_names:
            continue
        try:
            df = _read_bar_csv(p)
            out[sym] = df
        except Exception as e:
            log.warning(f"skip {p.name}: {e}")
    log.info(f"Loaded {len(out)} CSV symbols from {csv_dir}")
    return out


def _read_bar_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"])
    if df["ts"].dt.tz is None:
        df["ts"] = df["ts"].dt.tz_localize(IST)
    else:
        df["ts"] = df["ts"].dt.tz_convert(IST)
    return df.sort_values("ts").reset_index(drop=True)


# =====================================================================
# NIFTY TREND GATE
# =====================================================================
def load_nifty_bars_dhan(dhan, from_d: str, to_d: str,
                         interval: int = 5) -> pd.DataFrame | None:
    """Fetch NIFTY 50 spot 5-min bars from Dhan."""
    try:
        resp = dhan.intraday_minute_data(
            security_id=NIFTY_SECURITY_ID,
            exchange_segment=NIFTY_EXCH_SEGMENT,
            instrument_type=NIFTY_INSTR_TYPE,
            from_date=from_d, to_date=to_d,
            interval=interval,
        )
    except Exception as e:
        log.error(f"NIFTY fetch failed: {e}")
        return None
    if not isinstance(resp, dict):
        return None
    data = resp.get("data") if resp.get("data") else resp
    if not isinstance(data, dict) or not data.get("open"):
        return None
    df = pd.DataFrame({
        "open":  data["open"], "high": data["high"],
        "low":   data["low"],  "close": data["close"],
        "volume": data.get("volume", [0] * len(data["open"])),
    })
    ts = data.get("timestamp") or data.get("start_Time") or data.get("startTime")
    if not ts:
        return None
    df["ts"] = pd.to_datetime(ts, unit="s", utc=True).tz_convert(IST)
    return df.sort_values("ts").reset_index(drop=True)


def build_nifty_trend(nifty: pd.DataFrame, strict: bool = False) -> dict:
    """
    Precompute a per-timestamp trend map for NIFTY.
      +1 = bullish  (close > intraday VWAP AND close > EMA20)
      -1 = bearish  (close < intraday VWAP AND close < EMA20)
       0 = neutral
    Intraday VWAP resets at the start of each session.
    Returns: {pd.Timestamp -> int}
    """
    if nifty is None or nifty.empty:
        return {}

    df = nifty.copy().reset_index(drop=True)
    df["date"] = df["ts"].dt.date

    # Intraday VWAP that resets each day, resilient to single-date input
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
    # EMA on close, computed continuously (not per-day) so it carries
    # momentum across sessions
    df["ema"] = df["close"].ewm(span=NIFTY_EMA_SPAN, adjust=False).mean()

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

    if strict:
        return trend
    return trend


def nifty_trend_at(ts: pd.Timestamp, trend_map: dict,
                   sorted_ts: list) -> int:
    """
    Look up NIFTY trend for a given timestamp. If the exact ts is not
    present (e.g. stock bar slightly off), use the most recent NIFTY bar
    at or before ts. Returns 0 (neutral) if none.
    """
    if not trend_map:
        return 0
    if ts in trend_map:
        return trend_map[ts]
    # bisect for the last ts <= given ts
    import bisect
    idx = bisect.bisect_right(sorted_ts, ts) - 1
    if idx < 0:
        return 0
    return trend_map[sorted_ts[idx]]


def passes_nifty_gate(direction: int, ntrend: int, strict: bool) -> bool:
    """
    direction: +1 for BUY, -1 for SELL
    strict=True  -> demand ntrend has the SAME sign
    strict=False -> reject only if ntrend has the OPPOSITE sign
    """
    if strict:
        return (direction > 0 and ntrend == +1) or (direction < 0 and ntrend == -1)
    else:
        return (direction > 0 and ntrend >= 0) or (direction < 0 and ntrend <= 0)


# =====================================================================
# TRANSACTION COSTS
# =====================================================================
def _apply_costs(entry_px: float, exit_px: float, qty: int, side: str) -> float:
    """Return NET PnL after slippage, taxes, and flat brokerage."""
    # Slippage: entry fills worse, exit fills worse
    slip = SLIPPAGE_BPS / 10_000
    if side == "BUY":
        eff_entry = entry_px * (1 + slip)
        eff_exit  = exit_px  * (1 - slip)
        gross = (eff_exit - eff_entry) * qty
    else:
        eff_entry = entry_px * (1 - slip)
        eff_exit  = exit_px  * (1 + slip)
        gross = (eff_entry - eff_exit) * qty

    turnover = (entry_px + exit_px) * qty
    taxes = turnover * (TAXES_BPS_ONEWAY / 10_000)
    brokerage = BROKERAGE_PER_TRADE * 2   # entry + exit
    return gross - taxes - brokerage


# =====================================================================
# CORE BACKTEST ENGINE
# =====================================================================
def simulate_trade_bars(df: pd.DataFrame, entry_idx: int,
                        entry_price: float, sl: float, tgt: float,
                        side: str) -> tuple[int, float, str]:
    """
    Walk forward from entry_idx and return (exit_idx, exit_price, outcome).
    Conservative intra-bar assumption: if a bar's [low, high] contains
    BOTH sl and tgt, assume SL hit first (worst-case fill).
    """
    for j in range(entry_idx, len(df)):
        bar_h = df["high"].iloc[j]
        bar_l = df["low"].iloc[j]
        bar_ts = df["ts"].iloc[j]

        # Force squareoff at/after 15:15 IST
        if bar_ts.time() >= SQUAREOFF_TIME:
            return j, float(df["close"].iloc[j]), "SQUAREOFF"

        if side == "BUY":
            sl_hit  = bar_l <= sl
            tgt_hit = bar_h >= tgt
            if sl_hit and tgt_hit:
                return j, sl, "SL"       # pessimistic
            if sl_hit:
                return j, sl, "SL"
            if tgt_hit:
                return j, tgt, "TARGET"
        else:  # SELL
            sl_hit  = bar_h >= sl
            tgt_hit = bar_l <= tgt
            if sl_hit and tgt_hit:
                return j, sl, "SL"
            if sl_hit:
                return j, sl, "SL"
            if tgt_hit:
                return j, tgt, "TARGET"

    # ran out of bars -> exit on last close
    j = len(df) - 1
    return j, float(df["close"].iloc[j]), "SQUAREOFF"


def _in_entry_window(ts: pd.Timestamp) -> bool:
    t = ts.time()
    return SCAN_START <= t <= NO_ENTRY_AFTER


def _confirmation_ok(hits, df_slice: pd.DataFrame) -> list:
    """Same confirmation rule as live scanner."""
    if not REQUIRE_CONFIRMATION or len(df_slice) < 2:
        return hits
    prev_h = df_slice["high"].iloc[-2]
    prev_l = df_slice["low"].iloc[-2]
    close_now = df_slice["close"].iloc[-1]
    return [h for h in hits
            if (h[0] > 0 and close_now > prev_h) or
               (h[0] < 0 and close_now < prev_l) or
               h[0] == 0]


def backtest(symbol_bars: dict[str, pd.DataFrame],
             top_per_bar: int = 30,
             nifty_trend_map: dict | None = None,
             nifty_strict: bool = False,
             nifty_gate_enabled: bool = True) -> BacktestResult:
    """
    Cross-sectional backtest:
      * Group all bars across all symbols by their close-timestamp.
      * At each 5-min close, run the same detect+score+filter pipeline
        on EVERY symbol using only data up to that bar (no look-ahead).
      * Rank signals, take top-N, enforce MAX_OPEN_POSITIONS + cooldown.
      * Enter next bar's OPEN.
    """
    result = BacktestResult()
    equity = float(INITIAL_CAPITAL)

    # Pre-sort NIFTY timestamps for asof lookup
    sorted_ntrend_ts = sorted(nifty_trend_map.keys()) if nifty_trend_map else []
    gate_active = bool(nifty_gate_enabled and sorted_ntrend_ts)

    # Stats counters for the filter's effectiveness
    stats = {"raw_signals": 0, "nifty_rejected": 0, "nifty_bullish_bars": 0,
             "nifty_bearish_bars": 0, "nifty_neutral_bars": 0}

    # Build a master timeline of unique bar closes across all symbols
    all_ts = sorted({ts for df in symbol_bars.values() for ts in df["ts"]})
    log.info(f"Backtesting {len(symbol_bars)} symbols across {len(all_ts)} 5-min bars "
             f"| NIFTY gate: {'ON' if gate_active else 'OFF'} "
             f"({'strict' if nifty_strict else 'soft'})")

    # Pre-index each symbol df by ts for O(1) slicing
    ts_index: dict[str, dict[pd.Timestamp, int]] = {}
    for sym, df in symbol_bars.items():
        ts_index[sym] = {ts: i for i, ts in enumerate(df["ts"])}

    open_positions: dict[str, dict] = {}   # symbol -> position state
    traded_today: dict[str, set] = {}       # date -> {symbols}
    peak_equity = equity
    max_dd = 0.0

    for k, bar_ts in enumerate(all_ts):
        date_key = bar_ts.date()
        traded_today.setdefault(date_key, set())

        # ---- (A) Resolve any open positions on this bar ----
        for sym in list(open_positions.keys()):
            pos = open_positions[sym]
            if sym not in ts_index or bar_ts not in ts_index[sym]:
                continue
            j = ts_index[sym][bar_ts]
            df = symbol_bars[sym]
            # Only look at bars AT/AFTER entry+1
            if j < pos["entry_bar_idx"]:
                continue
            # Check this single bar for SL/TGT/squareoff
            exit_idx, exit_px, outcome = simulate_trade_bars(
                df.iloc[j:j + 1].reset_index(drop=True), 0,
                pos["entry_price"], pos["sl"], pos["tgt"], pos["side"],
            )
            if outcome in ("SL", "TARGET", "SQUAREOFF"):
                pnl_net = _apply_costs(pos["entry_price"], exit_px, pos["qty"], pos["side"])
                r_mult = pnl_net / max(pos["risk_amount"], 1e-9)
                equity += pnl_net
                result.trades.append(Trade(
                    symbol=sym, pattern=pos["pattern"], side=pos["side"],
                    entry_time=pos["entry_ts"].strftime("%Y-%m-%d %H:%M"),
                    entry_price=round(pos["entry_price"], 2),
                    exit_time=bar_ts.strftime("%Y-%m-%d %H:%M"),
                    exit_price=round(exit_px, 2),
                    qty=pos["qty"], sl_price=pos["sl"], tgt_price=pos["tgt"],
                    outcome=outcome,
                    pnl_gross=round((exit_px - pos["entry_price"]) *
                                    pos["qty"] * (1 if pos["side"] == "BUY" else -1), 2),
                    pnl_net=round(pnl_net, 2),
                    r_multiple=round(r_mult, 2),
                    score=pos["score"], vol_ratio=pos["vol_ratio"],
                ))
                del open_positions[sym]

        # ---- (B) Signal generation on this bar close ----
        if not _in_entry_window(bar_ts):
            result.equity_curve.append((bar_ts, equity))
            continue

        signals_this_bar: list[dict] = []
        for sym, df in symbol_bars.items():
            if sym in open_positions or sym in traded_today[date_key]:
                continue
            if bar_ts not in ts_index[sym]:
                continue
            i = ts_index[sym][bar_ts]
            if i + 1 < MIN_CANDLES_NEEDED:      # not enough history yet
                continue
            # Slice up to and INCLUDING this bar (this bar is now "closed")
            hist = df.iloc[: i + 1].copy()
            # Restrict to intraday session (avoid overnight gap confusion)
            hist = hist[hist["ts"].dt.date == date_key].reset_index(drop=True)
            if len(hist) < MIN_CANDLES_NEEDED:
                continue

            hits = detect_patterns(hist)
            if not hits:
                continue
            hits = _confirmation_ok(hits, hist)
            if not hits:
                continue
            scored = score_signals(sym, sym, hist, hits)
            if scored:
                signals_this_bar.extend(scored)

        if not signals_this_bar:
            result.equity_curve.append((bar_ts, equity))
            continue

        stats["raw_signals"] += len(signals_this_bar)

        # ---- NIFTY trend gate ----
        if gate_active:
            ntrend = nifty_trend_at(bar_ts, nifty_trend_map, sorted_ntrend_ts)
            if ntrend > 0:   stats["nifty_bullish_bars"] += 1
            elif ntrend < 0: stats["nifty_bearish_bars"] += 1
            else:            stats["nifty_neutral_bars"] += 1

            kept = []
            for s in signals_this_bar:
                if passes_nifty_gate(s["direction"], ntrend, nifty_strict):
                    s["nifty_trend"] = ntrend
                    kept.append(s)
                else:
                    stats["nifty_rejected"] += 1
            signals_this_bar = kept

        if not signals_this_bar:
            result.equity_curve.append((bar_ts, equity))
            continue

        ranked = (pd.DataFrame(signals_this_bar)
                  .sort_values(["score", "vol_ratio", "strength"], ascending=[False, False, False])
                  .drop_duplicates(subset=["symbol"], keep="first")
                  .head(top_per_bar))

        # ---- (C) Take entries at NEXT bar's OPEN ----
        for _, sig in ranked.iterrows():
            if sig["score"] < MIN_SCORE_TO_TRADE:
                continue
            if len(open_positions) >= MAX_OPEN_POSITIONS:
                break
            sym = sig["symbol"]
            if sym in open_positions or sym in traded_today[date_key]:
                continue
            i = ts_index[sym][bar_ts]
            df = symbol_bars[sym]
            if i + 1 >= len(df):
                continue
            next_bar = df.iloc[i + 1]
            if next_bar["ts"].date() != date_key:  # would spill overnight
                continue

            entry_px = float(next_bar["open"])
            side = sig["signal"]           # BUY / SELL
            direction = 1 if side == "BUY" else -1
            sl, tgt, sl_dist = compute_sl_target(entry_px, direction, sig.get("atr"))
            qty = compute_quantity(entry_px, sl_dist)
            if qty <= 0:
                continue

            open_positions[sym] = {
                "pattern": sig["pattern"], "side": side,
                "entry_ts": next_bar["ts"], "entry_price": entry_px,
                "sl": sl, "tgt": tgt, "qty": qty,
                "entry_bar_idx": i + 1, "risk_amount": sl_dist * qty,
                "score": int(sig["score"]), "vol_ratio": float(sig["vol_ratio"]),
            }
            traded_today[date_key].add(sym)

        # ---- (D) Equity / drawdown tracking ----
        peak_equity = max(peak_equity, equity)
        dd = (peak_equity - equity) / peak_equity if peak_equity else 0
        max_dd = max(max_dd, dd)
        result.equity_curve.append((bar_ts, equity))

    result.stats = stats

    # Force-close any leftover positions at last available bar (safety)
    for sym, pos in list(open_positions.items()):
        df = symbol_bars[sym]
        last = df.iloc[-1]
        pnl_net = _apply_costs(pos["entry_price"], float(last["close"]), pos["qty"], pos["side"])
        result.trades.append(Trade(
            symbol=sym, pattern=pos["pattern"], side=pos["side"],
            entry_time=pos["entry_ts"].strftime("%Y-%m-%d %H:%M"),
            entry_price=round(pos["entry_price"], 2),
            exit_time=last["ts"].strftime("%Y-%m-%d %H:%M"),
            exit_price=round(float(last["close"]), 2),
            qty=pos["qty"], sl_price=pos["sl"], tgt_price=pos["tgt"],
            outcome="EOD_FORCED",
            pnl_gross=round((float(last["close"]) - pos["entry_price"]) *
                            pos["qty"] * (1 if pos["side"] == "BUY" else -1), 2),
            pnl_net=round(pnl_net, 2),
            r_multiple=round(pnl_net / max(pos["risk_amount"], 1e-9), 2),
            score=pos["score"], vol_ratio=pos["vol_ratio"],
        ))

    return result


# =====================================================================
# REPORTING
# =====================================================================
def summarize(res: BacktestResult, initial_capital: float) -> dict:
    if not res.trades:
        return {"trades": 0, "note": "No trades taken"}

    df = pd.DataFrame([asdict(t) for t in res.trades])
    wins = df[df["pnl_net"] > 0]
    losses = df[df["pnl_net"] <= 0]

    total_pnl = df["pnl_net"].sum()
    win_rate = len(wins) / len(df)
    avg_win = wins["pnl_net"].mean() if not wins.empty else 0
    avg_loss = losses["pnl_net"].mean() if not losses.empty else 0
    profit_factor = (wins["pnl_net"].sum() / -losses["pnl_net"].sum()
                     if not losses.empty and losses["pnl_net"].sum() < 0 else float("inf"))
    expectancy_r = df["r_multiple"].mean()
    expectancy_rs = df["pnl_net"].mean()

    # Equity curve stats
    eq = pd.DataFrame(res.equity_curve, columns=["ts", "equity"])
    if not eq.empty:
        eq["dd"] = (eq["equity"].cummax() - eq["equity"]) / eq["equity"].cummax()
        max_dd = eq["dd"].max()
        # Sharpe on daily equity changes
        eq["date"] = pd.to_datetime(eq["ts"]).dt.date
        daily = eq.groupby("date")["equity"].last().pct_change().dropna()
        sharpe = (daily.mean() / daily.std() * (252 ** 0.5)) if daily.std() > 0 else 0.0
    else:
        max_dd, sharpe = 0.0, 0.0

    # NIFTY filter stats (if the gate was active)
    st = res.stats or {}
    raw = st.get("raw_signals", 0)
    rej = st.get("nifty_rejected", 0)
    nifty_stats = {
        "raw_signals":       raw,
        "rejected_by_nifty": rej,
        "rejection_rate_%":  round(100 * rej / raw, 2) if raw else 0.0,
        "bullish_bars":      st.get("nifty_bullish_bars", 0),
        "bearish_bars":      st.get("nifty_bearish_bars", 0),
        "neutral_bars":      st.get("nifty_neutral_bars", 0),
    }

    return {
        "trades":          len(df),
        "wins":            len(wins),
        "losses":          len(losses),
        "win_rate":        round(win_rate * 100, 2),
        "profit_factor":   round(profit_factor, 2),
        "expectancy_R":    round(expectancy_r, 3),
        "expectancy_rs":   round(expectancy_rs, 2),
        "total_pnl":       round(total_pnl, 2),
        "return_pct":      round(total_pnl / initial_capital * 100, 2),
        "avg_win":         round(avg_win, 2),
        "avg_loss":        round(avg_loss, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "sharpe_annual":   round(sharpe, 2),
        "by_pattern":      df.groupby("pattern")["pnl_net"].agg(["count", "sum", "mean"]).round(2).to_dict("index"),
        "by_outcome":      df["outcome"].value_counts().to_dict(),
        "nifty_gate":      nifty_stats,
    }


def save_reports(res: BacktestResult, out_dir: Path, summary: dict) -> tuple[Path, Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts_tag = datetime.now().strftime("%Y%m%d_%H%M")

    trades_csv = out_dir / f"trades_{ts_tag}.csv"
    if res.trades:
        pd.DataFrame([asdict(t) for t in res.trades]).to_csv(trades_csv, index=False)

    eq_csv = out_dir / f"equity_{ts_tag}.csv"
    pd.DataFrame(res.equity_curve, columns=["ts", "equity"]).to_csv(eq_csv, index=False)

    summary_txt = out_dir / f"summary_{ts_tag}.txt"
    with open(summary_txt, "w") as f:
        f.write("========== BACKTEST SUMMARY ==========\n")
        for k, v in summary.items():
            if isinstance(v, dict):
                f.write(f"\n{k}:\n")
                for kk, vv in v.items():
                    f.write(f"  {kk}: {vv}\n")
            else:
                f.write(f"{k:>20}: {v}\n")
    return trades_csv, eq_csv, summary_txt


# =====================================================================
# ENTRY POINT
# =====================================================================
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["dhan", "csv"], required=True)
    ap.add_argument("--csv-dir", type=str, default="./data",
                    help="folder with <SYMBOL>.csv files (ts,open,high,low,close,volume)")
    ap.add_argument("--days", type=int, default=5,
                    help="Dhan mode only: trailing calendar days to fetch (max ~5)")
    ap.add_argument("--top", type=int, default=30,
                    help="Take top-N ranked signals per 5-min bar")
    ap.add_argument("--max-symbols", type=int, default=80)
    ap.add_argument("--out", type=str, default="./backtest_out")
    ap.add_argument("--sleep", type=float, default=0.22,
                    help="Dhan mode: delay between symbol fetches (rate-limit safe)")

    # ---- NIFTY trend gate flags ----
    ap.add_argument("--no-nifty-gate", action="store_true",
                    help="Disable the NIFTY 50 trend filter (for A/B comparison)")
    ap.add_argument("--nifty-strict", action="store_true",
                    help="Strict gate: require NIFTY trend to match direction exactly "
                         "(reject Neutral). Default is soft: reject only opposite trend.")
    ap.add_argument("--nifty-csv", type=str, default="",
                    help="CSV mode only: path to NIFTY.csv (ts,open,high,low,close,volume). "
                         "If omitted in CSV mode, we look for '<csv-dir>/NIFTY.csv'.")
    args = ap.parse_args()

    # ---- Load bars per symbol + NIFTY ----
    nifty_df: pd.DataFrame | None = None

    if args.mode == "dhan":
        dhan = _dhan_client()
        universe = load_universe_dhan(args.max_symbols)
        to_d = datetime.now(IST).strftime("%Y-%m-%d")
        from_d = (datetime.now(IST) - timedelta(days=args.days)).strftime("%Y-%m-%d")

        log.info("Fetching NIFTY 50 index bars ...")
        nifty_df = load_nifty_bars_dhan(dhan, from_d, to_d)
        if nifty_df is None:
            log.warning("NIFTY fetch failed -> gate will be disabled")

        log.info(f"Fetching Dhan bars {from_d} -> {to_d} for {len(universe)} symbols")
        bars: dict[str, pd.DataFrame] = {}
        for i, row in enumerate(universe.itertuples(index=False)):
            df = fetch_dhan_bars(dhan, row.security_id, from_d, to_d)
            if df is not None and len(df) >= MIN_CANDLES_NEEDED:
                bars[row.symbol] = df
            time.sleep(args.sleep)
            if (i + 1) % 25 == 0:
                log.info(f"  fetched {i + 1}/{len(universe)}  (kept {len(bars)})")
        log.info(f"Usable symbols: {len(bars)}")
    else:
        csv_dir = Path(args.csv_dir).expanduser().resolve()
        if not csv_dir.exists():
            raise SystemExit(f"CSV dir does not exist: {csv_dir}")
        # Skip NIFTY.csv in symbol universe (loaded separately below)
        bars = load_csv_bars(csv_dir, skip_names={"NIFTY", "NIFTY50", "NIFTY_50"})
        if not bars:
            raise SystemExit("No CSVs loaded.")

        # Resolve NIFTY CSV path
        nifty_path: Path | None = None
        if args.nifty_csv:
            nifty_path = Path(args.nifty_csv).expanduser().resolve()
        else:
            for cand in ("NIFTY.csv", "NIFTY50.csv", "NIFTY_50.csv"):
                p = csv_dir / cand
                if p.exists():
                    nifty_path = p; break
        if nifty_path and nifty_path.exists():
            try:
                nifty_df = _read_bar_csv(nifty_path)
                log.info(f"Loaded NIFTY bars from {nifty_path.name} ({len(nifty_df)} rows)")
            except Exception as e:
                log.warning(f"NIFTY CSV read failed: {e}")
        else:
            log.warning("No NIFTY.csv found -> gate will be disabled")

    if not bars:
        raise SystemExit("No symbol data available. Aborting.")

    # ---- Build NIFTY trend map ----
    nifty_gate_enabled = (not args.no_nifty_gate) and (nifty_df is not None)
    nifty_trend_map = build_nifty_trend(nifty_df, strict=args.nifty_strict) if nifty_df is not None else {}
    if nifty_gate_enabled:
        log.info(f"NIFTY trend map built: {len(nifty_trend_map)} bars, "
                 f"mode={'strict' if args.nifty_strict else 'soft'}")

    # ---- Run backtest ----
    log.info("Running backtest engine ...")
    result = backtest(
        bars,
        top_per_bar=args.top,
        nifty_trend_map=nifty_trend_map,
        nifty_strict=args.nifty_strict,
        nifty_gate_enabled=nifty_gate_enabled,
    )

    # ---- Report ----
    summary = summarize(result, INITIAL_CAPITAL)
    print("\n============ BACKTEST SUMMARY ============")
    for k, v in summary.items():
        if isinstance(v, dict):
            print(f"\n{k}:")
            for kk, vv in v.items():
                print(f"  {kk}: {vv}")
        else:
            print(f"{k:>20}: {v}")

    out_dir = Path(args.out).expanduser().resolve()
    trades_csv, eq_csv, summary_txt = save_reports(result, out_dir, summary)
    log.info(f"Saved: {trades_csv}")
    log.info(f"Saved: {eq_csv}")
    log.info(f"Saved: {summary_txt}")


if __name__ == "__main__":
    main()
