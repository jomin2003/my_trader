"""Seed TODAY's gap rows into gap_data.csv at ~09:20 IST (cron via /trigger/seed-gap).

Why this exists: the post-market precompute (15:35 IST) only knows gaps through
yesterday, and GapFill/GapGo look up zones keyed on TODAY's date — so without this
seed both strategies could never fire intraday (audit finding #4, HIGH).

Method: for each symbol, fetch two days of 5-min bars (previous trading day + today),
compute today's gap = (today's first open − previous close) / previous close, and
take daily_atr from the symbol's most recent historical row in gap_data.csv (a two-day
window cannot produce a stable Wilder ATR). Idempotent: re-running replaces today's
rows. No look-ahead: at seed time only pre-open/first-bar prices are used, which are
already public.
"""
from __future__ import annotations
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

import config_registry as cfgm

log = logging.getLogger("seed_gap")

GAP_MIN = 0.25   # same bounds as precompute_gapfill
GAP_MAX = 3.0


def _prev_trading_day(today):
    y = today - timedelta(days=1)
    while y.weekday() >= 5:  # skip Sat/Sun
        y -= timedelta(days=1)
    return y


def _bars_to_df(data):
    df = pd.DataFrame({
        "open": data["open"], "high": data["high"], "low": data["low"],
        "close": data["close"],
        "volume": data.get("volume", [0] * len(data["open"])),
    })
    ts = data.get("timestamp") or data.get("start_Time") or data.get("startTime")
    if not ts:
        return None
    try:
        df["ts"] = pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Kolkata")
    except Exception:
        return None
    df = df.dropna(subset=["open", "high", "low", "close"])
    return df[df["volume"] > 0].sort_values("ts").reset_index(drop=True)


def seed_today(dhan, universe, gap_csv_path: str | None = None) -> int:
    """Seed today's gap rows + PDHL levels. Returns the number of gap rows written."""
    from intraday_pattern_scanner_v2 import CANDLE_INTERVAL_MIN

    out_path = Path(gap_csv_path) if gap_csv_path else _gap_path()
    today = datetime.now().astimezone().date()
    yday = _prev_trading_day(today)

    # daily_atr per symbol from the most recent historical row
    atr_map: dict[str, float] = {}
    if out_path.exists():
        try:
            old = pd.read_csv(out_path)
            old["date"] = pd.to_datetime(old["date"]).dt.date
            old = old[old["date"] < today].sort_values("date")
            for r in old.itertuples(index=False):
                atr_map[str(r.symbol).upper()] = float(r.daily_atr)
        except Exception as e:
            log.warning(f"seed_gap: could not read historical ATRs: {e}")

    new_rows = []
    pdhl_rows = []
    for _, urow in universe.iterrows():
        sym = str(urow["symbol"]).upper()
        sid = str(urow["security_id"])
        try:
            resp = dhan.intraday_minute_data(
                security_id=sid, exchange_segment="NSE_EQ", instrument_type="EQUITY",
                from_date=yday.isoformat(), to_date=today.isoformat(),
                interval=CANDLE_INTERVAL_MIN)
            data = resp.get("data") if isinstance(resp, dict) else None
            if not data or not data.get("open"):
                continue
            df = _bars_to_df(data)
            if df is None or df.empty:
                continue
            df["d"] = df["ts"].dt.date
            prev_bars = df[df["d"] < today]
            today_bars = df[df["d"] == today].sort_values("ts")
            if prev_bars.empty or today_bars.empty:
                continue
            prev_close = float(prev_bars["close"].iloc[-1])
            prev_high = float(prev_bars["high"].max())
            prev_low = float(prev_bars["low"].min())
            topen = float(today_bars["open"].iloc[0])
            if prev_close <= 0 or topen <= 0:
                continue
            atr = atr_map.get(sym)
            if not atr or atr <= 0:
                continue
            # PDHL levels: previous day's extremes are today's breakout levels.
            # atr is the daily-scale ATR carried from the gap table (informational —
            # live exits use the scanner's own 5-min ATR).
            pdhl_rows.append({
                "symbol": sym, "date": today.isoformat(),
                "pdh": round(prev_high, 2), "pdl": round(prev_low, 2),
                "prev_close": round(prev_close, 2),
                "prev_range": round(prev_high - prev_low, 2),
                "atr": round(atr, 3),
            })
            gap = abs(topen - prev_close) / prev_close * 100
            if gap < GAP_MIN or gap > GAP_MAX:
                continue
            new_rows.append({
                "symbol": sym, "date": today.isoformat(),
                "prev_close": round(prev_close, 2), "today_open": round(topen, 2),
                "gap_pct": round(gap, 3),
                "gap_dir": "UP" if topen > prev_close else "DOWN",
                "daily_atr": round(atr, 3),
            })
        except Exception as e:
            log.warning(f"seed_gap skip {sym}: {e}")
        time.sleep(cfgm.get("REQUEST_SLEEP_SEC") or 0.22)

    _write_rows(gap_csv_path or str(_gap_path()), new_rows, today)
    _write_rows(str(_pdhl_path()), pdhl_rows, today)
    log.info(f"seed_gap: wrote {len(new_rows)} gap rows, {len(pdhl_rows)} pdhl rows for {today}")
    return len(new_rows)


def _gap_path():
    from multi_strategy_live import GAP_DATA_PATH
    return Path(GAP_DATA_PATH)


def _pdhl_path():
    from multi_strategy_live import PDHL_DATA_PATH
    return Path(PDHL_DATA_PATH)


def _write_rows(path: str, rows: list, today):
    """Replace today's rows with `rows`, keep everything else. No-op when empty."""
    p = Path(path)
    if not rows:
        return
    if p.exists():
        try:
            old = pd.read_csv(p)
            old["date"] = pd.to_datetime(old["date"]).dt.date
            old = old[old["date"] != today]
        except Exception:
            old = pd.DataFrame()
        merged = pd.concat([old, pd.DataFrame(rows)], ignore_index=True)
    else:
        merged = pd.DataFrame(rows)
    merged.to_csv(p, index=False)
