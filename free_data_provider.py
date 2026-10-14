"""
free_data_provider.py — Fast, Free Market Data Provider & Hybrid Dhan Wrapper
=============================================================================
Provides zero-cost, low-latency NSE intraday bars and real-time quotes via
Yahoo Finance's v8 chart HTTP API (with automatic `yfinance` fallback),
exposing a Dhan-SDK-compatible interface:
  * intraday_minute_data(security_id, exchange_segment, instrument_type,
                         from_date, to_date, interval=15)
  * quote_data(securities={exchange_segment: [int_or_str_sec_id, ...]})
  * quoteData(securities=...)  # alias for SDK compatibility

Also provides `HybridMarketClient`, which wraps a real Dhan SDK client (when
credentials are configured) so order placement (`place_order`, `cancel_order`,
`get_order_by_id`) uses Dhan while market data requests either:
  * use the free provider directly (`DATA_PROVIDER=yahoo` / `free`), or
  * try Dhan first and seamlessly fall back to Yahoo Finance on empty/error
    responses (`DATA_PROVIDER=auto`, the default when Dhan Data API is not
    subscribed).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, date as ddate, time as dtime, timedelta
from typing import Any
from urllib.parse import quote as urlquote
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("free_data")

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)

_YAHOO_CHART_URLS = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
    "https://query2.finance.yahoo.com/v8/finance/chart/{ticker}",
)

# Built-in NIFTY 50 + key F&O fallback mapping (symbol <-> Dhan NSE_EQ security_id)
# Used when the public scrip master hasn't been loaded yet or is unreachable.
FALLBACK_SYMBOL_TO_SECID: dict[str, str] = {
    "RELIANCE": "2885",
    "HDFCBANK": "1333",
    "ICICIBANK": "4963",
    "INFY": "1594",
    "TCS": "11536",
    "BHARTIARTL": "10604",
    "SBIN": "3045",
    "ITC": "1660",
    "HINDUNILVR": "1394",
    "LT": "11483",
    "AXISBANK": "5900",
    "KOTAKBANK": "1922",
    "BAJFINANCE": "317",
    "ASIANPAINT": "236",
    "MARUTI": "10999",
    "SUNPHARMA": "3351",
    "TITAN": "3506",
    "ULTRACEMCO": "11532",
    "M&M": "2031",
    "HCLTECH": "7229",
    "NTPC": "11630",
    "TMPV": "3456",
    "POWERGRID": "14977",
    "NESTLEIND": "17963",
    "JSWSTEEL": "11723",
    "BAJAJFINSV": "16675",
    "TATASTEEL": "3499",
    "WIPRO": "3787",
    "ONGC": "2475",
    "GRASIM": "1232",
    "TECHM": "13538",
    "ADANIENT": "25",
    "COALINDIA": "20374",
    "ADANIPORTS": "15083",
    "HINDALCO": "1363",
    "BAJAJ-AUTO": "16669",
    "INDUSINDBK": "5258",
    "BRITANNIA": "547",
    "CIPLA": "694",
    "DRREDDY": "881",
    "EICHERMOT": "910",
    "HEROMOTOCO": "1348",
    "APOLLOHOSP": "157",
    "DIVISLAB": "10940",
    "SBILIFE": "21808",
    "HDFCLIFE": "467",
    "BPCL": "526",
    "TATACONSUM": "3432",
    "SHRIRAMFIN": "4306",
    "ETERNAL": "5097",
    "BEL": "383",
    "JIOFIN": "18143",
    "TRENT": "1964",
}

# Known NSE corporate-action / ticker aliases on Yahoo Finance
_SYMBOL_ALIASES: dict[str, str] = {
    "TATAMOTORS": "TMPV",
    "ZOMATO": "ETERNAL",
    "LTIM": "LTIM",
}

_MAP_LOCK = threading.Lock()
_SECID_TO_SYM: dict[str, str] = {
    "13": "ABB",
    **{v: k for k, v in FALLBACK_SYMBOL_TO_SECID.items()},
}
_SYM_TO_SECID: dict[str, str] = {
    "^NSEI": "13",
    "NIFTY": "13",
    "NIFTY50": "13",
    "ABB": "13",
    **FALLBACK_SYMBOL_TO_SECID,
}


def register_universe_mapping(df: pd.DataFrame | None) -> None:
    """Register `symbol` <-> `security_id` pairs from the scanner's universe DataFrame."""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return
    if "symbol" not in df.columns or "security_id" not in df.columns:
        return
    with _MAP_LOCK:
        for sym, sid in zip(df["symbol"].astype(str), df["security_id"].astype(str)):
            s_clean = sym.strip().upper()
            id_clean = sid.strip()
            if s_clean and id_clean:
                _SECID_TO_SYM[id_clean] = s_clean
                _SYM_TO_SECID[s_clean] = id_clean


def register_symbol(symbol: str, security_id: str | int) -> None:
    """Register a single `symbol` <-> `security_id` pair."""
    s_clean = str(symbol or "").strip().upper()
    id_clean = str(security_id or "").strip()
    if not s_clean or not id_clean:
        return
    with _MAP_LOCK:
        _SECID_TO_SYM[id_clean] = s_clean
        _SYM_TO_SECID[s_clean] = id_clean


def resolve_symbol(
    security_id_or_symbol: str | int,
    exchange_segment: str = "NSE_EQ",
    instrument_type: str = "EQUITY",
) -> str | None:
    """Resolve a Dhan `security_id` (or plain symbol) to an NSE symbol / Yahoo index."""
    key = str(security_id_or_symbol or "").strip()
    if not key:
        return None
    if (
        exchange_segment == "IDX_I"
        or str(instrument_type).upper() == "INDEX"
        or key.upper() in ("^NSEI", "NIFTY", "NIFTY50")
    ):
        return "^NSEI"
    with _MAP_LOCK:
        if key in _SECID_TO_SYM:
            return _SECID_TO_SYM[key]
        u = key.upper()
        if u in _SYM_TO_SECID:
            return u
    if key == "13":
        return "^NSEI"
    # If the caller passed an alphabetic symbol directly (e.g. "RELIANCE" or "M&M")
    if any(ch.isalpha() for ch in key):
        return key.upper().removesuffix(".NS")
    return None


def to_yahoo_ticker(symbol: str) -> str:
    """Convert an NSE symbol to a Yahoo Finance ticker string."""
    s = str(symbol or "").strip().upper()
    if s in ("^NSEI", "NIFTY", "NIFTY50", "13"):
        return "^NSEI"
    if s.endswith(".NS") or s.endswith(".BO") or s.startswith("^"):
        return s
    s = _SYMBOL_ALIASES.get(s, s)
    return f"{s}.NS"


def get_fallback_universe_df() -> pd.DataFrame:
    """Return a ready-to-use universe DataFrame when the Dhan scrip master is unreachable."""
    rows = [
        {"symbol": sym, "security_id": sid}
        for sym, sid in sorted(FALLBACK_SYMBOL_TO_SECID.items())
    ]
    df = pd.DataFrame(rows, columns=["symbol", "security_id"])
    register_universe_mapping(df)
    return df


def _parse_date(d: Any) -> ddate | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, ddate):
        return d
    s = str(d).strip()
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        return None


def _interval_str(interval: int | str) -> str:
    try:
        mins = int(interval)
    except Exception:
        s = str(interval).strip().lower()
        return s if s.endswith(("m", "h", "d")) else "15m"
    if mins <= 1:
        return "1m"
    if mins <= 2:
        return "2m"
    if mins <= 5:
        return "5m"
    if mins <= 15:
        return "15m"
    if mins <= 30:
        return "30m"
    if mins <= 60:
        return "60m"
    return "1d"


def _range_for_dates(from_date: Any, to_date: Any, interval_str: str) -> str:
    today = datetime.now(IST).date()
    d_from = _parse_date(from_date) or today
    days_back = max(1, (today - d_from).days + 2)
    if interval_str == "1m":
        return f"{min(days_back, 7)}d"
    if days_back <= 5:
        return "5d"
    if days_back <= 30:
        return "1mo"
    return "60d"


class FreeMarketDataClient:
    """
    Fast, zero-auth market data provider backed by Yahoo Finance v8 chart API.

    Features:
      * Connection-pooled `requests.Session` (up to 32 keep-alive connections).
      * Thread-safe TTL cache for chart responses so repeated calls within a
        scan cycle or OCO check are instantaneous.
      * Multi-URL failover (`query1` -> `query2` -> `yfinance` library).
      * Full Dhan SDK method compatibility (`intraday_minute_data`,
        `quote_data`, `quoteData`, plus paper-mode order stubs).
    """

    is_free_provider: bool = True

    def __init__(self, cache_ttl_sec: float | None = None, timeout_sec: float = 8.0):
        if cache_ttl_sec is None:
            try:
                cache_ttl_sec = float(os.getenv("FREE_DATA_CACHE_TTL_SEC", "20"))
            except Exception:
                cache_ttl_sec = 20.0
        self.cache_ttl_sec = max(0.0, float(cache_ttl_sec))
        self.timeout_sec = float(timeout_sec)
        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=1)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)
        self._session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
        })
        self._cache_lock = threading.Lock()
        # (yahoo_ticker, interval_str, range_str) -> (monotonic_ts, df, meta_dict)
        self._chart_cache: dict[tuple[str, str, str], tuple[float, pd.DataFrame, dict[str, Any]]] = {}

    def clear_cache(self) -> None:
        with self._cache_lock:
            self._chart_cache.clear()

    def _fetch_yahoo_chart(
        self,
        yahoo_ticker: str,
        interval_str: str = "15m",
        range_str: str = "5d",
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        cache_key = (yahoo_ticker, interval_str, range_str)
        now_m = time.monotonic()
        if self.cache_ttl_sec > 0:
            with self._cache_lock:
                cached = self._chart_cache.get(cache_key)
                if cached is not None and (now_m - cached[0]) <= self.cache_ttl_sec:
                    return cached[1].copy(), dict(cached[2])

        encoded = urlquote(yahoo_ticker, safe="^.")
        params = {
            "interval": interval_str,
            "range": range_str,
            "includePrePost": "false",
        }
        last_err: Exception | None = None
        for tmpl in _YAHOO_CHART_URLS:
            url = tmpl.format(ticker=encoded)
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout_sec)
                if resp.status_code != 200:
                    continue
                payload = resp.json()
                chart = payload.get("chart") or {}
                results = chart.get("result")
                if not results:
                    break
                res0 = results[0] or {}
                meta = res0.get("meta") or {}
                timestamps = res0.get("timestamp") or []
                quotes = ((res0.get("indicators") or {}).get("quote") or [{}])[0] or {}
                if not timestamps or not quotes.get("close"):
                    df = pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
                else:
                    n = len(timestamps)
                    df = pd.DataFrame({
                        "ts": pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(IST),
                        "open": (quotes.get("open") or [None] * n)[:n],
                        "high": (quotes.get("high") or [None] * n)[:n],
                        "low": (quotes.get("low") or [None] * n)[:n],
                        "close": (quotes.get("close") or [None] * n)[:n],
                        "volume": (quotes.get("volume") or [0] * n)[:n],
                    })
                    df = df.dropna(subset=["open", "high", "low", "close"])
                    if not df.empty:
                        t_col = df["ts"].dt.time
                        df = df[(t_col >= MARKET_OPEN) & (t_col <= MARKET_CLOSE)]
                        df["volume"] = df["volume"].fillna(0).astype(np.int64)
                        df = (
                            df.sort_values("ts")
                            .drop_duplicates(subset=["ts"], keep="last")
                            .reset_index(drop=True)
                        )
                with self._cache_lock:
                    self._chart_cache[cache_key] = (time.monotonic(), df.copy(), dict(meta))
                return df, meta
            except Exception as e:
                last_err = e
                continue

        # Secondary fallback: yfinance library if installed
        df_yf, meta_yf = self._fetch_via_yfinance(yahoo_ticker, interval_str, range_str)
        if not df_yf.empty or meta_yf:
            with self._cache_lock:
                self._chart_cache[cache_key] = (time.monotonic(), df_yf.copy(), dict(meta_yf))
            return df_yf, meta_yf

        if last_err:
            log.debug(f"[FREE_DATA] chart fetch failed for {yahoo_ticker}: {last_err}")
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"]), {}

    @staticmethod
    def _fetch_via_yfinance(
        yahoo_ticker: str,
        interval_str: str,
        range_str: str,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        try:
            import yfinance as yf
            raw = yf.Ticker(yahoo_ticker).history(
                period=range_str,
                interval=interval_str,
                auto_adjust=False,
                prepost=False,
            )
            if raw is None or raw.empty:
                return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"]), {}
            raw = raw.reset_index()
            ts_col = "Datetime" if "Datetime" in raw.columns else ("Date" if "Date" in raw.columns else raw.columns[0])
            ts = pd.to_datetime(raw[ts_col])
            if ts.dt.tz is None:
                ts = ts.dt.tz_localize("UTC").dt.tz_convert(IST)
            else:
                ts = ts.dt.tz_convert(IST)
            df = pd.DataFrame({
                "ts": ts,
                "open": raw["Open"].astype(float),
                "high": raw["High"].astype(float),
                "low": raw["Low"].astype(float),
                "close": raw["Close"].astype(float),
                "volume": raw["Volume"].fillna(0).astype(np.int64),
            }).dropna(subset=["open", "high", "low", "close"])
            t_col = df["ts"].dt.time
            df = df[(t_col >= MARKET_OPEN) & (t_col <= MARKET_CLOSE)].reset_index(drop=True)
            meta = {"regularMarketPrice": float(df["close"].iloc[-1])} if not df.empty else {}
            return df, meta
        except Exception:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"]), {}

    def fetch_bars_df(
        self,
        symbol_or_secid: str | int,
        interval: int | str = 15,
        days_back: int = 5,
        exchange_segment: str = "NSE_EQ",
    ) -> pd.DataFrame:
        """Convenience method returning a clean IST DataFrame (`ts,open,high,low,close,volume`)."""
        sym = resolve_symbol(symbol_or_secid, exchange_segment=exchange_segment)
        if not sym:
            return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
        ticker = to_yahoo_ticker(sym)
        iv_str = _interval_str(interval)
        today = datetime.now(IST).date()
        from_d = today - timedelta(days=max(1, int(days_back)))
        rng = _range_for_dates(from_d, today, iv_str)
        df, _ = self._fetch_yahoo_chart(ticker, interval_str=iv_str, range_str=rng)
        return df

    def intraday_minute_data(
        self,
        security_id: str | int,
        exchange_segment: str = "NSE_EQ",
        instrument_type: str = "EQUITY",
        from_date: str | None = None,
        to_date: str | None = None,
        interval: int = 15,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Dhan-SDK-compatible `intraday_minute_data` implementation."""
        sym = resolve_symbol(security_id, exchange_segment=exchange_segment)
        if not sym:
            return {"status": "failure", "remarks": f"Unknown security_id={security_id}", "data": {}}

        ticker = to_yahoo_ticker(sym)
        iv_str = _interval_str(interval)
        rng = _range_for_dates(from_date, to_date, iv_str)
        df, _ = self._fetch_yahoo_chart(ticker, interval_str=iv_str, range_str=rng)
        if df.empty:
            return {"status": "failure", "remarks": "No data returned", "data": {}}

        d_from = _parse_date(from_date)
        d_to = _parse_date(to_date)
        filtered = df
        if d_from and d_to:
            dates = filtered["ts"].dt.date
            filtered = filtered[(dates >= d_from) & (dates <= d_to)]
            # If today has no bars yet (e.g. weekend/holiday/pre-open) and caller
            # requested a single day, fall back to the most recent trading session
            # so NIFTY gate / scanner tests / diagnostics still get valid bars.
            if filtered.empty and d_from == d_to and not df.empty:
                last_session = df["ts"].dt.date.max()
                filtered = df[df["ts"].dt.date == last_session]

        if filtered.empty:
            return {"status": "failure", "remarks": "No bars in date range", "data": {}}

        epoch_ts = [int(t.timestamp()) for t in filtered["ts"]]
        return {
            "status": "success",
            "data": {
                "open": [round(float(x), 4) for x in filtered["open"]],
                "high": [round(float(x), 4) for x in filtered["high"]],
                "low": [round(float(x), 4) for x in filtered["low"]],
                "close": [round(float(x), 4) for x in filtered["close"]],
                "volume": [int(x) for x in filtered["volume"]],
                "timestamp": epoch_ts,
                "start_Time": epoch_ts,
            },
        }

    def _quote_for_secid(self, sec_id: str | int, exchange_segment: str = "NSE_EQ") -> dict[str, Any] | None:
        sym = resolve_symbol(sec_id, exchange_segment=exchange_segment)
        if not sym:
            return None
        ticker = to_yahoo_ticker(sym)
        df, meta = self._fetch_yahoo_chart(ticker, interval_str="5m", range_str="5d")
        ltp = meta.get("regularMarketPrice")
        if ltp is None and not df.empty:
            ltp = float(df["close"].iloc[-1])
        if ltp is None or float(ltp) <= 0:
            return None
        ltp_f = float(ltp)
        if not df.empty:
            last_Date = df["ts"].dt.date.max()
            day_df = df[df["ts"].dt.date == last_Date]
            day_open = float(day_df["open"].iloc[0])
            day_high = max(float(day_df["high"].max()), ltp_f)
            day_low = min(float(day_df["low"].min()), ltp_f)
            day_close = float(day_df["close"].iloc[-1])
            vol = int(day_df["volume"].sum())
        else:
            day_open = day_high = day_low = day_close = ltp_f
            vol = int(meta.get("regularMarketVolume") or 0)

        return {
            "last_price": ltp_f,
            "LTP": ltp_f,
            "volume": vol,
            "ohlc": {
                "open": day_open,
                "high": day_high,
                "low": day_low,
                "close": day_close,
            },
        }

    def quote_data(
        self,
        securities: dict[str, list[Any]] | None = None,
        *,
        security_id: str | int | None = None,
        exchange_segment: str = "NSE_EQ",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Dhan-SDK-compatible `quote_data` / `quoteData` implementation.

        Supports both calling conventions:
          1. `quote_data({"NSE_EQ": [2885, 11536]})` (Dhan v2 SDK)
          2. `quoteData(security_id="2885", exchange_segment="NSE_EQ")` (scanner `_latest_bar`)
        """
        if securities is None and security_id is not None:
            securities = {str(exchange_segment or "NSE_EQ"): [security_id]}
        if not isinstance(securities, dict):
            return {"status": "failure", "data": {}}
        out_data: dict[str, dict[str, Any]] = {}
        flat_by_sid: dict[str, Any] = {}
        tasks: list[tuple[str, str]] = []
        for seg, id_list in securities.items():
            out_data[seg] = {}
            for raw_id in (id_list or []):
                tasks.append((seg, str(raw_id)))

        def _fetch_one(item: tuple[str, str]):
            seg, sid = item
            return seg, sid, self._quote_for_secid(sid, exchange_segment=seg)

        if len(tasks) == 1:
            seg, sid, q = _fetch_one(tasks[0])
            if q is not None:
                out_data[seg][sid] = q
                flat_by_sid[sid] = q
        elif tasks:
            with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as pool:
                for seg, sid, q in pool.map(_fetch_one, tasks):
                    if q is not None:
                        out_data[seg][sid] = q
                        flat_by_sid[sid] = q

        single_rec = next(iter(flat_by_sid.values()), {}) if len(flat_by_sid) == 1 else {}
        return {
            "status": "success",
            "data": {**single_rec, "data": out_data, **out_data, **flat_by_sid},
        }

    # Alias for callers checking `dhan.quoteData`
    quoteData = quote_data

    # Safety stubs if order methods are ever invoked on a pure free-data client
    def place_order(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "status": "failure",
            "remarks": "Live order placement requires DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN",
            "data": {},
        }

    def get_order_by_id(self, order_id: str) -> dict[str, Any]:
        return {"status": "failure", "data": {}}

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        return {"status": "failure", "data": {}}


class HybridMarketClient:
    """
    Combines a live Dhan SDK client (for order execution & optionally primary
    market data) with `FreeMarketDataClient` (for fast, free market data or
    automatic fallback when Dhan Data API subscription is absent/expired).
    """

    def __init__(
        self,
        dhan_client: Any | None,
        free_client: FreeMarketDataClient | None = None,
        mode: str = "auto",
    ):
        self._dhan = dhan_client
        self._free = free_client or FreeMarketDataClient()
        self.mode = (mode or "auto").strip().lower()
        # Once Dhan Data API returns empty/401/403 (e.g. no paid data plan),
        # short-circuit directly to the free provider for a cooldown window so
        # we don't pay failed-request latency on every symbol in the scan loop.
        self._dhan_data_backoff_until: float = 0.0
        self._backoff_sec: float = 900.0  # 15 minutes

    @property
    def is_free_provider(self) -> bool:
        return (
            self._dhan is None
            or self.mode in ("yahoo", "free", "yfinance")
            or time.monotonic() < self._dhan_data_backoff_until
        )

    @property
    def _dhan_data_failed(self) -> bool:
        return time.monotonic() < self._dhan_data_backoff_until

    def _should_try_dhan_data(self) -> bool:
        if self._dhan is None:
            return False
        if self.mode in ("yahoo", "free", "yfinance"):
            return False
        if self.mode == "dhan":
            return True
        return time.monotonic() >= self._dhan_data_backoff_until

    def intraday_minute_data(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if self._should_try_dhan_data():
            try:
                resp = self._dhan.intraday_minute_data(*args, **kwargs)
                data = resp.get("data", resp) if isinstance(resp, dict) else {}
                if isinstance(data, dict) and data.get("close"):
                    return resp
                if self.mode == "dhan":
                    return resp
                # Dhan returned no data (commonly missing Data API subscription)
                self._dhan_data_backoff_until = time.monotonic() + self._backoff_sec
                log.info("[HYBRID_DATA] Dhan intraday_minute_data empty/unavailable; switching to Yahoo Finance fallback")
            except Exception as e:
                if self.mode == "dhan":
                    raise
                self._dhan_data_backoff_until = time.monotonic() + self._backoff_sec
                log.info(f"[HYBRID_DATA] Dhan intraday_minute_data error ({e}); switching to Yahoo Finance fallback")
        return self._free.intraday_minute_data(*args, **kwargs)

    def quote_data(
        self,
        securities: dict[str, list[Any]] | None = None,
        *,
        security_id: str | int | None = None,
        exchange_segment: str = "NSE_EQ",
        **kwargs: Any,
    ) -> dict[str, Any]:
        if securities is None and security_id is not None:
            securities = {str(exchange_segment or "NSE_EQ"): [security_id]}
        if self._should_try_dhan_data() and isinstance(securities, dict):
            try:
                fn = getattr(self._dhan, "quote_data", None) or getattr(self._dhan, "quoteData", None)
                if fn is not None:
                    try:
                        resp = fn(securities)
                    except TypeError:
                        resp = fn(security_id=security_id, exchange_segment=exchange_segment)
                    data = (resp or {}).get("data", {})
                    if isinstance(data, dict) and "data" in data:
                        data = data["data"]
                    if isinstance(data, dict) and any(bool(v) for v in data.values()):
                        return resp
                    if self.mode == "dhan":
                        return resp
                    self._dhan_data_backoff_until = time.monotonic() + self._backoff_sec
            except Exception:
                if self.mode == "dhan":
                    raise
                self._dhan_data_backoff_until = time.monotonic() + self._backoff_sec
        return self._free.quote_data(securities, security_id=security_id, exchange_segment=exchange_segment, **kwargs)

    quoteData = quote_data

    def __getattr__(self, name: str) -> Any:
        if self._dhan is not None and hasattr(self._dhan, name):
            return getattr(self._dhan, name)
        return getattr(self._free, name)


_SINGLETON_LOCK = threading.Lock()
_FREE_SINGLETON: FreeMarketDataClient | None = None


def get_free_client() -> FreeMarketDataClient:
    global _FREE_SINGLETON
    with _SINGLETON_LOCK:
        if _FREE_SINGLETON is None:
            _FREE_SINGLETON = FreeMarketDataClient()
        return _FREE_SINGLETON


def build_market_client(
    client_id: str | None = None,
    access_token: str | None = None,
    provider: str | None = None,
) -> FreeMarketDataClient | HybridMarketClient:
    """
    Factory that builds the optimal market client for the current configuration:
      * `DATA_PROVIDER=yahoo` / `free`: uses `FreeMarketDataClient` for all data
        (and still attaches Dhan SDK for order execution if credentials exist).
      * `DATA_PROVIDER=auto` (default): uses Dhan if credentials exist with
        instant fallback to `FreeMarketDataClient` when Dhan Data API is not
        subscribed or credentials are missing.
      * `DATA_PROVIDER=dhan`: strict Dhan mode (raises if credentials missing).
    """
    mode = (
        provider
        or os.getenv("MARKET_DATA_PROVIDER")
        or os.getenv("DATA_PROVIDER")
        or "auto"
    ).strip().lower()
    cid = (client_id if client_id is not None else os.getenv("DHAN_CLIENT_ID", "")).strip()
    tok = (access_token if access_token is not None else os.getenv("DHAN_ACCESS_TOKEN", "")).strip()
    has_creds = bool(cid and tok and not cid.startswith("YOUR_") and not tok.startswith("YOUR_"))

    free_client = get_free_client()
    if mode in ("yahoo", "free", "yfinance") and not has_creds:
        return free_client

    dhan_raw = None
    if has_creds:
        try:
            from dhanhq import DhanContext, dhanhq
            ctx = DhanContext(cid, tok)
            dhan_raw = dhanhq(ctx)
        except ImportError:
            try:
                from dhanhq import dhanhq as dhanhq_v1
                dhan_raw = dhanhq_v1(cid, tok)
            except Exception as e:
                log.warning(f"[FREE_DATA] dhanhq init failed ({e}); using free provider")
        except Exception as e:
            log.warning(f"[FREE_DATA] dhanhq init failed ({e}); using free provider")

    if dhan_raw is None:
        if mode == "dhan":
            raise RuntimeError("DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN missing while DATA_PROVIDER=dhan")
        return free_client

    return HybridMarketClient(dhan_client=dhan_raw, free_client=free_client, mode=mode)
