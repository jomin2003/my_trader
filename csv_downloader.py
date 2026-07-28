"""
=====================================================================
 CSV DATA DOWNLOADER for intraday_pattern_scanner_v2 pipeline
---------------------------------------------------------------------
 Fetches 5-min OHLCV bars for NSE F&O underlyings + NIFTY 50 index
 and saves them as <SYMBOL>.csv files in the exact format that the
 backtest harness and parameter sweep expect:

     ts,open,high,low,close,volume     # ts is IST-aware ISO datetime

 Two data sources:

 1. yfinance  (default, FREE, no auth)
    * Gives up to ~60 calendar days of 5-min bars per symbol.
    * NSE stocks use suffix .NS  (e.g. RELIANCE.NS).
    * NIFTY 50 index is ^NSEI.
    * Best for building a 2-3 month base dataset in one shot.

 2. dhan  (uses your DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN)
    * Only ~5 trading days of intraday history per call.
    * BUT run this daily and it APPENDS to your CSVs, letting you
      accumulate history that yfinance can't provide (e.g. tick-
      accurate NSE data past 60 days).
    * Uses the same instrument master + F&O filter as the scanner.

 Universe options:
   * --preset nifty50   : the 50 NIFTY constituents  (default)
   * --preset fno       : all NSE F&O underlyings  (~180 symbols)
   * --symbols-file <p> : one symbol per line (plain NSE tickers,
                         no .NS suffix, e.g. RELIANCE)

 Key features:
   * MERGE + DEDUPE: existing <SYMBOL>.csv is loaded, new bars are
     appended, duplicate timestamps are dropped -> safe to run daily.
   * IST-aware timestamps everywhere.
   * Market-hours filter (09:15-15:30 IST).
   * Retries + backoff on transient failures.
   * Progress logging.
   * Outputs everything into --out (default: ./data).

 Usage:
   # First-time bulk download of last ~60 days (NIFTY50 + index):
   pip install yfinance
   python csv_downloader.py --mode yfinance --preset nifty50 --out ./data

   # Full F&O universe (~180 symbols) via yfinance:
   python csv_downloader.py --mode yfinance --preset fno --out ./data

   # Daily top-up via Dhan (needs env vars):
   export DHAN_CLIENT_ID=... DHAN_ACCESS_TOKEN=...
   python csv_downloader.py --mode dhan --preset nifty50 --out ./data

   # Custom symbol list:
   python csv_downloader.py --mode yfinance \\
       --symbols-file ./mysyms.txt --out ./data
=====================================================================
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN  = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

INSTRUMENT_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("downloader")


# =====================================================================
# NIFTY 50 CONSTITUENTS — fetched dynamically from Dhan instrument
# master. Falls back to hardcoded list if fetch fails.
# =====================================================================
_NIFTY50_FALLBACK = [
    "RELIANCE","HDFCBANK","ICICIBANK","INFY","TCS","BHARTIARTL","SBIN",
    "ITC","HINDUNILVR","LT","AXISBANK","KOTAKBANK","BAJFINANCE","ASIANPAINT",
    "MARUTI","SUNPHARMA","TITAN","ULTRACEMCO","M&M","HCLTECH","NTPC",
    "TATAMOTORS","POWERGRID","NESTLEIND","JSWSTEEL","BAJAJFINSV","TATASTEEL",
    "WIPRO","ONGC","GRASIM","TECHM","ADANIENT","COALINDIA","ADANIPORTS",
    "HINDALCO","BAJAJ-AUTO","INDUSINDBK","BRITANNIA","CIPLA","DRREDDY",
    "EICHERMOT","HEROMOTOCO","APOLLOHOSP","LTIM","DIVISLAB","SBILIFE",
    "HDFCLIFE","BPCL","TATACONSUM","SHRIRAMFIN",
]


def load_nifty50_validated() -> list[str]:
    """
    Return NIFTY 50 constituents, pruned against Dhan's live instrument
    master so delisted / renamed names drop out automatically.

    The master does not label index membership, so we start from the
    known constituent list and keep only those still present as NSE F&O
    underlyings (a good proxy for large-cap tradability). Falls back to
    the full static list if the fetch fails.
    """
    try:
        resp = requests.get(INSTRUMENT_MASTER_URL, timeout=30)
        resp.raise_for_status()
        df = pd.read_csv(io.StringIO(resp.text), low_memory=False)
        cols = {c.upper(): c for c in df.columns}
        C = lambda n: cols[n.upper()]
        fno = df[
            (df[C("SEM_EXM_EXCH_ID")].astype(str).str.upper() == "NSE") &
            (df[C("SEM_INSTRUMENT_NAME")].astype(str).str.upper().isin(["FUTSTK", "OPTSTK"]))
        ]
        live_unders = set(
            fno[C("SEM_TRADING_SYMBOL")].astype(str)
            .str.split("-").str[0].str.upper().unique().tolist()
        )
        validated = [s for s in _NIFTY50_FALLBACK if s.upper() in live_unders]
        dropped = [s for s in _NIFTY50_FALLBACK if s.upper() not in live_unders]
        if len(validated) >= 40:
            if dropped:
                log.info(f"NIFTY 50: dropped {len(dropped)} stale names {dropped}")
            log.info(f"NIFTY 50 validated: {len(validated)} live constituents")
            return validated
        log.warning(f"Only {len(validated)} validated — using static fallback")
    except Exception as e:
        log.warning(f"Failed to validate NIFTY 50 from master: {e}")
    return _NIFTY50_FALLBACK[:]


# =====================================================================
# UTILITIES
# =====================================================================
def _to_ist(series: pd.Series) -> pd.Series:
    """Convert any datetime series to IST-aware."""
    s = pd.to_datetime(series)
    if s.dt.tz is None:
        # Assume input is in UTC if naive (yfinance default)
        s = s.dt.tz_localize("UTC").dt.tz_convert(IST)
    else:
        s = s.dt.tz_convert(IST)
    return s


def _in_market_hours(ts_series: pd.Series) -> pd.Series:
    t = ts_series.dt.time
    return (t >= MARKET_OPEN) & (t <= MARKET_CLOSE)


def _standardize(df: pd.DataFrame) -> pd.DataFrame:
    """
    Enforce the canonical schema expected by the backtest:
        ts (IST tz-aware), open, high, low, close, volume
    Drops rows outside market hours, sorts, dedupes on ts.
    """
    keep = ["ts", "open", "high", "low", "close", "volume"]
    for c in keep:
        if c not in df.columns:
            df[c] = np.nan
    df = df[keep].copy()
    df["ts"] = _to_ist(df["ts"])
    df = df.dropna(subset=["ts", "open", "high", "low", "close"])
    df = df[_in_market_hours(df["ts"])]
    df["volume"] = df["volume"].fillna(0).astype(int)
    df = (df.sort_values("ts")
             .drop_duplicates(subset=["ts"], keep="last")
             .reset_index(drop=True))
    return df


def merge_and_save(symbol: str, new_df: pd.DataFrame, out_dir: Path) -> tuple[int, int]:
    """
    Append new bars to any existing CSV, dedupe on ts, save.
    Returns (rows_added, total_rows).
    """
    fp = out_dir / f"{symbol}.csv"
    old = None
    if fp.exists():
        try:
            old = pd.read_csv(fp)
            old["ts"] = _to_ist(old["ts"])
            combined = pd.concat([old, new_df], ignore_index=True)
        except Exception as e:
            log.warning(f"[{symbol}] existing CSV unreadable, overwriting: {e}")
            combined = new_df
    else:
        combined = new_df

    combined = _standardize(combined)
    if combined.empty:
        return 0, 0

    # Write ts as ISO string (parseable back to tz-aware)
    combined_out = combined.copy()
    combined_out["ts"] = combined_out["ts"].dt.strftime("%Y-%m-%d %H:%M:%S%z")
    combined_out.to_csv(fp, index=False)

    added = len(combined) - (len(old) if old is not None and isinstance(old, pd.DataFrame) else 0)
    return max(0, added), len(combined)


# =====================================================================
# UNIVERSE
# =====================================================================
def load_universe_fno_from_dhan_master() -> list[str]:
    """Fetch F&O underlyings from Dhan's public instrument master (no auth)."""
    log.info("Downloading Dhan instrument master for F&O universe ...")
    resp = requests.get(INSTRUMENT_MASTER_URL, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), low_memory=False)
    cols = {c.upper(): c for c in df.columns}
    C = lambda n: cols[n.upper()]
    fno = df[
        (df[C("SEM_EXM_EXCH_ID")].astype(str).str.upper() == "NSE") &
        (df[C("SEM_INSTRUMENT_NAME")].astype(str).str.upper().isin(["FUTSTK", "OPTSTK"]))
    ]
    unders = (fno[C("SEM_TRADING_SYMBOL")].astype(str)
              .str.split("-").str[0].str.upper().unique().tolist())
    unders = sorted({u for u in unders if u.isalnum() or "&" in u or "-" in u})
    log.info(f"F&O underlyings found: {len(unders)}")
    return unders


def resolve_universe(preset: str, symbols_file: str | None) -> list[str]:
    if symbols_file:
        p = Path(symbols_file).expanduser().resolve()
        syms = [s.strip().upper() for s in p.read_text().splitlines() if s.strip()]
        log.info(f"Loaded {len(syms)} symbols from {p.name}")
        return syms
    if preset == "nifty50":
        return load_nifty50_validated()
    if preset == "fno":
        return load_universe_fno_from_dhan_master()
    raise SystemExit(f"Unknown preset: {preset}")


# =====================================================================
# YFINANCE SOURCE
# =====================================================================
def _yf_ticker(sym: str) -> str:
    """Map NSE symbol -> yfinance ticker."""
    if sym.upper() in ("NIFTY", "NIFTY50", "^NSEI"):
        return "^NSEI"
    # yfinance uses "-" oddly; handle a couple of known irregulars
    fixes = {
        "M&M":       "M%26M.NS",
        "BAJAJ-AUTO": "BAJAJ-AUTO.NS",
    }
    return fixes.get(sym.upper(), f"{sym.upper()}.NS")


def fetch_yf(sym: str, period_days: int, retries: int = 2) -> pd.DataFrame | None:
    """
    Fetch 5-min bars for the last `period_days` (max ~60 for 5m interval).
    Uses yfinance. Returns a DataFrame in canonical schema or None.
    """
    try:
        import yfinance as yf
    except ImportError:
        raise SystemExit("yfinance not installed. Run: pip install yfinance")

    ticker = _yf_ticker(sym)
    # yfinance caps 5m interval at ~60 days; clamp to be safe
    period = f"{min(period_days, 59)}d"

    last_err = None
    for attempt in range(retries + 1):
        try:
            df = yf.download(
                tickers=ticker, period=period, interval="5m",
                auto_adjust=False, prepost=False, progress=False,
                threads=False,
            )
            if df is None or df.empty:
                return None
            # yfinance may return MultiIndex cols for single ticker in newer versions
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df.reset_index()
            # 'Datetime' column comes back tz-aware in UTC (usually)
            ts_col = "Datetime" if "Datetime" in df.columns else df.columns[0]
            out = pd.DataFrame({
                "ts":     df[ts_col],
                "open":   df["Open"],
                "high":   df["High"],
                "low":    df["Low"],
                "close":  df["Close"],
                "volume": df.get("Volume", 0),
            })
            return _standardize(out)
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    log.debug(f"[{sym}] yfinance failed after retries: {last_err}")
    return None


# =====================================================================
# DHAN SOURCE
# =====================================================================
def _dhan_client():
    try:
        from dhanhq import DhanContext, dhanhq
    except ImportError:
        raise SystemExit("dhanhq not installed. Run: pip install dhanhq")
    cid = os.getenv("DHAN_CLIENT_ID")
    tok = os.getenv("DHAN_ACCESS_TOKEN")
    if not cid or not tok:
        raise SystemExit("Set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN env vars")
    return dhanhq(DhanContext(cid, tok))


def build_dhan_symbol_map() -> tuple[dict[str, str], dict[str, str]]:
    """
    Returns:
      equity_map:  SYMBOL -> security_id  (NSE_EQ / EQUITY)
      index_map:   'NIFTY' -> '13'         (IDX_I / INDEX)
    """
    resp = requests.get(INSTRUMENT_MASTER_URL, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), low_memory=False)
    cols = {c.upper(): c for c in df.columns}
    C = lambda n: cols[n.upper()]
    eq = df[
        (df[C("SEM_EXM_EXCH_ID")].astype(str).str.upper() == "NSE") &
        (df[C("SEM_SEGMENT")].astype(str).str.upper() == "E") &
        (df[C("SEM_INSTRUMENT_NAME")].astype(str).str.upper() == "EQUITY")
    ]
    eq_map = dict(zip(
        eq[C("SEM_TRADING_SYMBOL")].astype(str).str.upper(),
        eq[C("SEM_SMST_SECURITY_ID")].astype(str),
    ))
    idx_map = {"NIFTY": "13"}   # NIFTY 50 spot
    return eq_map, idx_map


def fetch_dhan(dhan, sym: str, security_id: str, is_index: bool,
               days: int) -> pd.DataFrame | None:
    to_d = datetime.now(IST).strftime("%Y-%m-%d")
    from_d = (datetime.now(IST) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        resp = dhan.intraday_minute_data(
            security_id=security_id,
            exchange_segment="IDX_I" if is_index else "NSE_EQ",
            instrument_type="INDEX" if is_index else "EQUITY",
            from_date=from_d, to_date=to_d, interval=5,
        )
    except Exception as e:
        log.debug(f"[{sym}] dhan fetch error: {e}")
        return None

    data = resp.get("data", resp) if isinstance(resp, dict) else {}
    if not isinstance(data, dict) or not data.get("open"):
        return None

    n = len(data["open"])
    out = pd.DataFrame({
        "ts":     data.get("timestamp") or data.get("startTime") or [None]*n,
        "open":   data["open"],
        "high":   data["high"],
        "low":    data["low"],
        "close":  data["close"],
        "volume": data.get("volume", [0]*n),
    })
    # timestamps come back as unix-seconds
    try:
        out["ts"] = pd.to_datetime(out["ts"], unit="s", utc=True).dt.tz_convert(IST)
    except Exception:
        return None
    return _standardize(out)


# =====================================================================
# MAIN LOOP
# =====================================================================
def run_yfinance(symbols: list[str], out_dir: Path, days: int, sleep: float):
    log.info(f"yfinance mode: {len(symbols)} symbols, period={days}d, out={out_dir}")
    kept = 0
    for i, sym in enumerate(symbols, 1):
        df = fetch_yf(sym, days)
        if df is None or df.empty:
            log.info(f"  [{i}/{len(symbols)}] {sym}: no data")
        else:
            added, total = merge_and_save(sym, df, out_dir)
            log.info(f"  [{i}/{len(symbols)}] {sym}: +{added} new, {total} total rows")
            kept += 1
        time.sleep(sleep)

    # NIFTY index (always fetch when using presets)
    ndf = fetch_yf("^NSEI", days)
    if ndf is not None and not ndf.empty:
        added, total = merge_and_save("NIFTY", ndf, out_dir)
        log.info(f"  NIFTY index: +{added} new, {total} total rows")

    log.info(f"Done. Kept {kept}/{len(symbols)} symbols.")


def run_dhan(symbols: list[str], out_dir: Path, days: int, sleep: float):
    dhan = _dhan_client()
    log.info("Building Dhan security-id map ...")
    eq_map, idx_map = build_dhan_symbol_map()

    log.info(f"dhan mode: {len(symbols)} symbols, days={days}, out={out_dir}")
    kept = 0
    for i, sym in enumerate(symbols, 1):
        sec_id = eq_map.get(sym.upper())
        if not sec_id:
            log.info(f"  [{i}/{len(symbols)}] {sym}: not in Dhan master, skip")
            continue
        df = fetch_dhan(dhan, sym, sec_id, is_index=False, days=days)
        if df is None or df.empty:
            log.info(f"  [{i}/{len(symbols)}] {sym}: no data")
        else:
            added, total = merge_and_save(sym, df, out_dir)
            log.info(f"  [{i}/{len(symbols)}] {sym}: +{added} new, {total} total rows")
            kept += 1
        time.sleep(sleep)

    # NIFTY
    ndf = fetch_dhan(dhan, "NIFTY", idx_map["NIFTY"], is_index=True, days=days)
    if ndf is not None and not ndf.empty:
        added, total = merge_and_save("NIFTY", ndf, out_dir)
        log.info(f"  NIFTY index: +{added} new, {total} total rows")

    log.info(f"Done. Kept {kept}/{len(symbols)} symbols.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["yfinance", "dhan"], default="yfinance")
    ap.add_argument("--preset", choices=["nifty50", "fno"], default="nifty50")
    ap.add_argument("--symbols-file", type=str, default="",
                    help="Optional: text file with one NSE symbol per line")
    ap.add_argument("--out", type=str, default="./data",
                    help="Output directory for <SYMBOL>.csv files")
    ap.add_argument("--days", type=int, default=59,
                    help="How many trailing days to fetch. yfinance max=59, dhan max~5")
    ap.add_argument("--sleep", type=float, default=0.4,
                    help="Delay (seconds) between symbol requests")
    args = ap.parse_args()

    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    symbols = resolve_universe(args.preset, args.symbols_file or None)

    # Clamp days per source
    if args.mode == "yfinance" and args.days > 59:
        log.warning(f"yfinance caps 5-min data at ~60 days; clamping days={args.days}->59")
        args.days = 59
    if args.mode == "dhan" and args.days > 5:
        log.warning(f"Dhan intraday only returns ~5 days; clamping days={args.days}->5")
        args.days = 5

    if args.mode == "yfinance":
        run_yfinance(symbols, out_dir, args.days, args.sleep)
    else:
        run_dhan(symbols, out_dir, args.days, args.sleep)

    # Summary of what we now have
    csvs = sorted(out_dir.glob("*.csv"))
    log.info(f"\n{out_dir} now contains {len(csvs)} CSV file(s).")
    if csvs:
        # Quick health check on 3 random files
        import random
        sample = random.sample(csvs, min(3, len(csvs)))
        for p in sample:
            try:
                df = pd.read_csv(p)
                if not df.empty:
                    df["ts"] = pd.to_datetime(df["ts"])
                    log.info(f"  {p.name}: {len(df)} rows | "
                             f"{df['ts'].min()} -> {df['ts'].max()}")
            except Exception:
                pass


if __name__ == "__main__":
    main()
