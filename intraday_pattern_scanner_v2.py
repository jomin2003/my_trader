"""
=====================================================================
 DHAN INTRADAY CANDLESTICK SCANNER + AUTO-TRADER + TELEGRAM  (v2)
---------------------------------------------------------------------
 Fixes vs v1:
   * Continuous 5-min loop (was single-shot).
   * Uses the last CLOSED bar for pattern detection (was forming bar).
   * IST-aware time gates (was naive local time).
   * VWAP + EMA20 trend filter for high-probability entries.
   * Confirmation-candle option (enter only if next bar breaks
     pattern high/low).
   * True OCO: polls broker; when SL or Target fills, the other
     is cancelled automatically. Position dict is cleaned.
   * Same-day cooldown per symbol (no re-fires).
   * Session filter (09:30 - 14:30 IST) + liquidity floor.
   * Wilder ATR, capped SL distance (0.3% - 1.5% of price).
   * Volume ratio excludes current bar.
   * Uses Dhan SDK constants (dhan.INTRA / dhan.SLM / dhan.NSE ...).
   * Correct nested error handling for TypeError fallback.
   * Rate-limit-aware backoff on 429 / DH-904.
   * Universe filtered to F&O underlyings, then trimmed by symbol.

 AUTO_TRADE_ENABLED = False by default. Paper-trade first.
=====================================================================
"""
from __future__ import annotations

import io
import os
import time
import logging
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

# ---------------------------------------------------------------------
# Dhan SDK
# ---------------------------------------------------------------------
try:
    from dhanhq import DhanContext, dhanhq
    DHAN_SDK_V2 = True
except ImportError:  # pragma: no cover
    from dhanhq import dhanhq              # type: ignore
    DhanContext = None                     # type: ignore
    DHAN_SDK_V2 = False

# =====================================================================
# CONFIG
# =====================================================================
CLIENT_ID     = os.getenv("DHAN_CLIENT_ID",    "YOUR_CLIENT_ID")
ACCESS_TOKEN  = os.getenv("DHAN_ACCESS_TOKEN", "YOUR_ACCESS_TOKEN")

TELEGRAM_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TG_CHAT_ID",   "YOUR_TELEGRAM_CHAT_ID")
TELEGRAM_ENABLED   = True

# ---- Auto-Trading ----
AUTO_TRADE_ENABLED    = False   # keep False until you have paper-traded a week
MIN_SCORE_TO_TRADE    = 7       # strength 5 + vol boost 1 + trend boost 1
RISK_REWARD_RATIO     = 2.0
MAX_RISK_PER_TRADE    = 500     # ₹
MAX_CAPITAL_PER_TRADE = 25000   # ₹
MAX_OPEN_POSITIONS    = 5
USE_ATR_STOP          = True
ATR_MULTIPLIER        = 1.5
FALLBACK_SL_PERCENT   = 0.005
MIN_SL_PCT            = 0.003   # SL cannot be tighter than 0.3%
MAX_SL_PCT            = 0.015   # SL cannot be wider than 1.5%
REQUIRE_CONFIRMATION  = True    # only enter after next bar breaks pattern H/L

# ---- Universe / data ----
USE_FNO_UNIVERSE_ONLY = True
MAX_STOCKS            = 180
CANDLE_INTERVAL_MIN   = 5
MIN_CANDLES_NEEDED    = 20       # need ATR(14) + VWAP + 3-bar pattern
TOP_N_RESULTS         = 20
MIN_TURNOVER_LAKHS    = 25       # per bar avg (₹ turnover) - liquidity floor

# ---- NIFTY trend gate (market-regime filter) ----
NIFTY_GATE_ENABLED    = True     # +1/-1/0 gate on all signals
NIFTY_STRICT          = False    # True = require exact-sign match (reject neutral)
_NIFTY_SEC_ID         = "13"
_NIFTY_EXCH_SEG       = "IDX_I"
_NIFTY_INSTR_TYPE     = "INDEX"

# ---- Rate limiting (Data API = 5/sec, 100k/day) ----
REQUEST_SLEEP_SEC     = 0.22
BACKOFF_ON_ERROR_SEC  = 2.0

# ---- Market timings (IST) ----
IST             = ZoneInfo("Asia/Kolkata")
MARKET_OPEN     = dtime(9, 15)
SCAN_START      = dtime(9, 30)   # skip opening 15 min noise
NO_ENTRY_AFTER  = dtime(14, 30)  # broker MIS auto-squareoff ~15:15-15:20
MARKET_CLOSE    = dtime(15, 20)

# ---- Loop cadence ----
SCAN_EVERY_SEC        = CANDLE_INTERVAL_MIN * 60   # one full pass per 5-min bar
POSITION_POLL_SEC     = 20                         # OCO monitor cadence

INSTRUMENT_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("dhan_scanner")


# =====================================================================
# HELPERS
# =====================================================================
def now_ist() -> datetime:
    return datetime.now(IST)


def in_session_for_entries() -> bool:
    t = now_ist().time()
    return SCAN_START <= t <= NO_ENTRY_AFTER


def market_open_now() -> bool:
    t = now_ist().time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


# =====================================================================
# NIFTY TREND GATE
# =====================================================================
def get_nifty_trend(dhan) -> int:
    """
    Compute NIFTY 50 regime once per scan.
      +1 bullish   : close > intraday-VWAP AND close > EMA20
      -1 bearish   : close < intraday-VWAP AND close < EMA20
       0 neutral   : mixed
    Returns 0 (neutral) on any failure so gate never blocks trading.
    """
    today = now_ist().strftime("%Y-%m-%d")
    try:
        resp = dhan.intraday_minute_data(
            security_id=_NIFTY_SEC_ID,
            exchange_segment=_NIFTY_EXCH_SEG,
            instrument_type=_NIFTY_INSTR_TYPE,
            from_date=today, to_date=today,
            interval=CANDLE_INTERVAL_MIN,
        )
    except Exception as e:
        log.debug(f"NIFTY fetch failed: {e}")
        return 0

    data = resp.get("data", resp) if isinstance(resp, dict) else {}
    if not isinstance(data, dict) or not data.get("close"):
        return 0

    df = pd.DataFrame({
        "high":  data["high"], "low": data["low"],
        "close": data["close"],
        "volume": data.get("volume", [0] * len(data["close"])),
    })
    # drop forming bar (same reason as stock bars)
    if len(df) >= 2:
        df = df.iloc[:-1]
    if len(df) < 3:
        return 0

    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    if df["volume"].sum() <= 0:
        vwap = float(tp.expanding().mean().iloc[-1])
    else:
        vwap = float((tp * df["volume"]).cumsum().iloc[-1] /
                     max(df["volume"].cumsum().iloc[-1], 1))
    ema20 = float(df["close"].ewm(span=20, adjust=False).mean().iloc[-1])
    c = float(df["close"].iloc[-1])

    if c > vwap and c > ema20: return +1
    if c < vwap and c < ema20: return -1
    return 0


def passes_nifty_gate(direction: int, ntrend: int) -> bool:
    """direction: +1 BUY, -1 SELL. ntrend: +1/-1/0."""
    if not NIFTY_GATE_ENABLED:
        return True
    if NIFTY_STRICT:
        return (direction > 0 and ntrend == +1) or (direction < 0 and ntrend == -1)
    return (direction > 0 and ntrend >= 0) or (direction < 0 and ntrend <= 0)


# =====================================================================
# TELEGRAM
# =====================================================================
def tg_send(text: str, silent: bool = False) -> None:
    if not TELEGRAM_ENABLED:
        return
    if TELEGRAM_BOT_TOKEN.startswith("YOUR_") or TELEGRAM_CHAT_ID.startswith("YOUR_"):
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": silent,
        }, timeout=5)
    except Exception as e:
        log.debug(f"Telegram send failed: {e}")


# =====================================================================
# 1. UNIVERSE
# =====================================================================
def load_intraday_universe() -> pd.DataFrame:
    log.info("Downloading Dhan instrument master ...")
    resp = requests.get(INSTRUMENT_MASTER_URL, timeout=30)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), low_memory=False)
    cols = {c.upper(): c for c in df.columns}
    C = lambda n: cols[n.upper()]

    eq_mask = (
        (df[C("SEM_EXM_EXCH_ID")].astype(str).str.upper() == "NSE") &
        (df[C("SEM_SEGMENT")].astype(str).str.upper() == "E") &
        (df[C("SEM_INSTRUMENT_NAME")].astype(str).str.upper() == "EQUITY")
    )
    equities = df.loc[eq_mask].copy()
    log.info(f"NSE equity rows: {len(equities)}")

    if USE_FNO_UNIVERSE_ONLY:
        fno_mask = (
            (df[C("SEM_EXM_EXCH_ID")].astype(str).str.upper() == "NSE") &
            (df[C("SEM_INSTRUMENT_NAME")].astype(str).str.upper()
                .isin(["FUTSTK", "OPTSTK"]))
        )
        fno_underlyings = (
            df.loc[fno_mask, C("SEM_TRADING_SYMBOL")]
              .astype(str).str.split("-").str[0].str.upper().unique()
        )
        equities = equities[
            equities[C("SEM_TRADING_SYMBOL")]
              .astype(str).str.upper().isin(fno_underlyings)
        ]
        log.info(f"Filtered to F&O underlyings: {len(equities)}")

    equities = (equities
                .drop_duplicates(subset=[C("SEM_TRADING_SYMBOL")])
                .sort_values(by=C("SEM_TRADING_SYMBOL"))
                .head(MAX_STOCKS)
                .copy())

    out = pd.DataFrame({
        "security_id": equities[C("SEM_SMST_SECURITY_ID")].astype(str),
        "symbol":      equities[C("SEM_TRADING_SYMBOL")].astype(str),
    }).reset_index(drop=True)

    log.info(f"Final intraday universe: {len(out)} stocks")
    return out


# =====================================================================
# 2. HISTORICAL DATA (5-min candles, today only)
# =====================================================================
def fetch_intraday(dhan, security_id: str) -> pd.DataFrame | None:
    today = now_ist().strftime("%Y-%m-%d")
    resp = None
    try:
        resp = dhan.intraday_minute_data(
            security_id=security_id,
            exchange_segment="NSE_EQ",
            instrument_type="EQUITY",
            from_date=today, to_date=today,
            interval=CANDLE_INTERVAL_MIN,
        )
    except TypeError:
        try:
            resp = dhan.intraday_minute_data(
                security_id, "NSE_EQ", "EQUITY",
                today, today, CANDLE_INTERVAL_MIN,
            )
        except Exception as e:
            log.debug(f"[{security_id}] fallback fetch error: {e}")
            time.sleep(BACKOFF_ON_ERROR_SEC)
            return None
    except Exception as e:
        log.debug(f"[{security_id}] fetch error: {e}")
        time.sleep(BACKOFF_ON_ERROR_SEC)
        return None

    if not isinstance(resp, dict):
        return None
    data = resp.get("data") if resp.get("data") else resp
    if not isinstance(data, dict) or "open" not in data or not data["open"]:
        return None

    df = pd.DataFrame({
        "open":   data["open"],
        "high":   data["high"],
        "low":    data["low"],
        "close":  data["close"],
        "volume": data.get("volume", [0] * len(data["open"])),
    })

    ts = data.get("timestamp") or data.get("start_Time") or data.get("startTime")
    if ts:
        try:
            df["ts"] = pd.to_datetime(ts, unit="s", utc=True).tz_convert(IST)
        except Exception:
            df["ts"] = pd.NaT

    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    if len(df) < MIN_CANDLES_NEEDED:
        return None

    # CRITICAL: drop the last (still-forming) candle. During live trading
    # the last row from Dhan's intraday endpoint is the in-progress bar.
    df = df.iloc[:-1].reset_index(drop=True)
    return df if len(df) >= MIN_CANDLES_NEEDED - 1 else None


# =====================================================================
# 3. INDICATORS
# =====================================================================
def wilder_atr(df: pd.DataFrame, period: int = 14) -> float | None:
    if len(df) < period + 1:
        return None
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
    # Wilder smoothing
    atr = np.zeros_like(tr)
    atr[period - 1] = tr[:period].mean()
    for i in range(period, len(tr)):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    val = float(atr[-1])
    return val if val > 0 else None


def rolling_vwap(df: pd.DataFrame) -> float | None:
    if len(df) < 3:
        return None
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].replace(0, np.nan)
    if vol.sum() <= 0 or vol.isna().all():
        return None
    return float((tp * df["volume"]).sum() / max(df["volume"].sum(), 1))


def ema(series: pd.Series, span: int) -> float | None:
    if len(series) < span:
        return None
    return float(series.ewm(span=span, adjust=False).mean().iloc[-1])


# =====================================================================
# 4. PATTERN DETECTION
# =====================================================================
def _body(o, c):          return abs(c - o)
def _range(h, l):         return max(h - l, 1e-9)
def _upper_wick(o, h, c): return h - max(o, c)
def _lower_wick(o, l, c): return min(o, c) - l


def detect_patterns(df: pd.DataFrame) -> list[tuple[int, str, int]]:
    """Return list of (direction, name, strength) using last 3 CLOSED bars."""
    if len(df) < 3:
        return []
    o0, h0, l0, c0 = df["open"].iloc[-3], df["high"].iloc[-3], df["low"].iloc[-3], df["close"].iloc[-3]
    o1, h1, l1, c1 = df["open"].iloc[-2], df["high"].iloc[-2], df["low"].iloc[-2], df["close"].iloc[-2]
    o2, h2, l2, c2 = df["open"].iloc[-1], df["high"].iloc[-1], df["low"].iloc[-1], df["close"].iloc[-1]

    hits: list[tuple[int, str, int]] = []
    body0, body1, body2 = _body(o0, c0), _body(o1, c1), _body(o2, c2)
    rng0,  rng1,  rng2  = _range(h0, l0), _range(h1, l1), _range(h2, l2)
    up2, lo2 = _upper_wick(o2, h2, c2), _lower_wick(o2, l2, c2)

    # Engulfing
    if c1 < o1 and c2 > o2 and o2 <= c1 and c2 >= o1 and body2 > body1:
        hits.append((+1, "Bullish Engulfing", 5))
    if c1 > o1 and c2 < o2 and o2 >= c1 and c2 <= o1 and body2 > body1:
        hits.append((-1, "Bearish Engulfing", 5))

    # Hammer / Shooting Star
    if body2 > 0 and lo2 >= 2 * body2 and up2 <= 0.3 * body2 and body2 / rng2 <= 0.35:
        hits.append((+1, "Hammer", 4))
    if body2 > 0 and up2 >= 2 * body2 and lo2 <= 0.3 * body2 and body2 / rng2 <= 0.35:
        hits.append((-1, "Shooting Star", 4))

    # Morning / Evening Star
    mid0 = (o0 + c0) / 2
    if (c0 < o0 and body0 > 0.6 * rng0 and body1 < 0.4 * body0 and
        c2 > o2 and body2 > 0.6 * rng2 and c2 > mid0):
        hits.append((+1, "Morning Star", 5))
    if (c0 > o0 and body0 > 0.6 * rng0 and body1 < 0.4 * body0 and
        c2 < o2 and body2 > 0.6 * rng2 and c2 < mid0):
        hits.append((-1, "Evening Star", 5))

    # Piercing / Dark Cloud
    if c1 < o1 and o2 < l1 and c2 > (o1 + c1) / 2 and c2 < o1:
        hits.append((+1, "Piercing Line", 4))
    if c1 > o1 and o2 > h1 and c2 < (o1 + c1) / 2 and c2 > o1:
        hits.append((-1, "Dark Cloud Cover", 4))

    # Three Soldiers / Crows
    if (c0 > o0 and c1 > o1 and c2 > o2 and c1 > c0 and c2 > c1 and
        o1 > o0 and o2 > o1 and body0 > 0.5 * rng0 and
        body1 > 0.5 * rng1 and body2 > 0.5 * rng2):
        hits.append((+1, "Three White Soldiers", 5))
    if (c0 < o0 and c1 < o1 and c2 < o2 and c1 < c0 and c2 < c1 and
        o1 < o0 and o2 < o1 and body0 > 0.5 * rng0 and
        body1 > 0.5 * rng1 and body2 > 0.5 * rng2):
        hits.append((-1, "Three Black Crows", 5))

    # Marubozu
    if body2 / rng2 >= 0.9 and c2 > o2:
        hits.append((+1, "Bullish Marubozu", 3))
    if body2 / rng2 >= 0.9 and c2 < o2:
        hits.append((-1, "Bearish Marubozu", 3))

    return hits


# =====================================================================
# 5. SIGNAL SCORING (+ trend / VWAP / confirmation filters)
# =====================================================================
def score_signals(symbol: str, security_id: str, df: pd.DataFrame,
                  hits: list[tuple[int, str, int]]) -> list[dict]:
    if not hits:
        return []
    close_now = float(df["close"].iloc[-1])
    high_now  = float(df["high"].iloc[-1])
    low_now   = float(df["low"].iloc[-1])
    vol_now   = float(df["volume"].iloc[-1])

    # avg vol EXCLUDING current bar (unbiased)
    prev_vol = df["volume"].iloc[:-1]
    avg_vol  = float(prev_vol.tail(20).mean()) if len(prev_vol) >= 5 else 0.0
    if avg_vol <= 0:
        return []
    vol_ratio = vol_now / avg_vol

    # liquidity floor (₹ turnover per bar in lakhs)
    turnover_lakhs = (close_now * avg_vol) / 1e5
    if turnover_lakhs < MIN_TURNOVER_LAKHS:
        return []

    atr_val  = wilder_atr(df, 14)
    vwap_val = rolling_vwap(df)
    ema20    = ema(df["close"], 20)

    rows = []
    for signal, name, strength in hits:
        if signal == 0:
            continue

        # ---- Trend / VWAP alignment ----
        trend_boost = 0
        if vwap_val is not None:
            if signal > 0 and close_now > vwap_val:
                trend_boost += 1
            elif signal < 0 and close_now < vwap_val:
                trend_boost += 1
            else:
                # Against VWAP -> reject
                continue
        if ema20 is not None:
            if signal > 0 and close_now > ema20:
                trend_boost += 0  # neutral confirm
            elif signal < 0 and close_now < ema20:
                trend_boost += 0
            else:
                # Against EMA20 -> weaken (but not reject)
                strength = max(1, strength - 1)

        vol_boost = 1 if vol_ratio >= 1.5 else 0

        rows.append({
            "symbol":        symbol,
            "security_id":   security_id,
            "pattern":       name,
            "signal":        "BUY" if signal > 0 else "SELL",
            "direction":     signal,
            "strength":      strength,
            "vol_ratio":     round(vol_ratio, 2),
            "score":         strength + vol_boost + trend_boost,
            "price":         round(close_now, 2),
            "pattern_high":  round(high_now, 2),
            "pattern_low":   round(low_now, 2),
            "atr":           round(atr_val, 2) if atr_val else None,
            "vwap":          round(vwap_val, 2) if vwap_val else None,
            "time":          df["ts"].iloc[-1].strftime("%H:%M") if "ts" in df else "",
        })
    return rows


# =====================================================================
# 6. TRADE MANAGEMENT
# =====================================================================
_OPEN_POSITIONS: dict[str, dict] = {}       # active positions with live OCO
_TRADED_TODAY:  set[str]        = set()     # symbols already traded today (cooldown)


def compute_sl_target(entry: float, direction: int, atr_val: float | None):
    if USE_ATR_STOP and atr_val and atr_val > 0:
        sl_dist = ATR_MULTIPLIER * atr_val
    else:
        sl_dist = FALLBACK_SL_PERCENT * entry

    # Clamp SL distance
    sl_dist = max(MIN_SL_PCT * entry, min(sl_dist, MAX_SL_PCT * entry))

    if direction > 0:
        sl     = round(entry - sl_dist, 2)
        target = round(entry + RISK_REWARD_RATIO * sl_dist, 2)
    else:
        sl     = round(entry + sl_dist, 2)
        target = round(entry - RISK_REWARD_RATIO * sl_dist, 2)
    return sl, target, sl_dist


def compute_quantity(entry: float, sl_dist: float) -> int:
    if sl_dist <= 0 or entry <= 0:
        return 0
    qty_by_risk    = int(MAX_RISK_PER_TRADE // sl_dist)
    qty_by_capital = int(MAX_CAPITAL_PER_TRADE // entry)
    return max(0, min(qty_by_risk, qty_by_capital))


def _order_id(resp) -> str | None:
    if not isinstance(resp, dict):
        return None
    data = resp.get("data") or {}
    return (data.get("orderId") or data.get("order_id") or
            resp.get("orderId") or resp.get("order_id"))


def _order_status(dhan, order_id: str) -> str:
    try:
        r = dhan.get_order_by_id(order_id)
        if isinstance(r, dict):
            d = r.get("data") or r
            return str(d.get("orderStatus") or d.get("status") or "").upper()
    except Exception as e:
        log.debug(f"status fetch fail {order_id}: {e}")
    return ""


def _cancel(dhan, order_id: str | None) -> None:
    if not order_id:
        return
    try:
        dhan.cancel_order(order_id)
    except Exception as e:
        log.debug(f"cancel fail {order_id}: {e}")


def place_bracket_orders(dhan, sig: dict) -> None:
    symbol   = sig["symbol"]
    sec_id   = sig["security_id"]
    entry_px = sig["price"]
    dirn     = sig["direction"]
    atr_val  = sig.get("atr")

    if symbol in _OPEN_POSITIONS or symbol in _TRADED_TODAY:
        return
    if len(_OPEN_POSITIONS) >= MAX_OPEN_POSITIONS:
        log.info(f"Max positions reached -> skip {symbol}")
        return

    sl_px, tgt_px, sl_dist = compute_sl_target(entry_px, dirn, atr_val)
    qty = compute_quantity(entry_px, sl_dist)
    if qty <= 0:
        log.info(f"[{symbol}] qty=0 (sl_dist={sl_dist:.2f}) -> skip")
        return

    side      = "BUY"  if dirn > 0 else "SELL"
    exit_side = "SELL" if dirn > 0 else "BUY"

    tg_txt = (
        f"🎯 <b>ORDER ATTEMPT</b> [{sig['pattern']}]\n"
        f"<b>{symbol}</b> {side} ×{qty}\n"
        f"Entry~₹{entry_px}  SL ₹{sl_px}  TGT ₹{tgt_px} (1:{RISK_REWARD_RATIO})\n"
        f"Score {sig['score']} | Vol×{sig['vol_ratio']} | VWAP {sig.get('vwap')}"
    )

    if not AUTO_TRADE_ENABLED:
        log.info(f"[DRY-RUN] {side} {qty} {symbol} @{entry_px} SL {sl_px} TGT {tgt_px}")
        tg_send("🧪 <b>DRY-RUN</b>\n" + tg_txt)
        _TRADED_TODAY.add(symbol)   # simulate cooldown even in dry run
        return

    # Use SDK constants where possible for forward compatibility
    order_kw = dict(
        security_id=sec_id,
        exchange_segment=getattr(dhan, "NSE", "NSE_EQ"),
        transaction_type=getattr(dhan, side, side),
        quantity=qty,
        product_type=getattr(dhan, "INTRA", "INTRADAY"),
    )

    # 1) Entry MARKET
    try:
        entry_resp = dhan.place_order(
            **order_kw,
            order_type=getattr(dhan, "MARKET", "MARKET"),
            price=0,
        )
    except Exception as e:
        log.error(f"[{symbol}] entry error: {e}")
        tg_send(f"❌ Entry error {symbol}: {e}")
        return

    status = str(entry_resp.get("status", "")).lower() if isinstance(entry_resp, dict) else ""
    if not any(k in status for k in ("success", "pending", "traded")):
        log.error(f"[{symbol}] entry rejected: {entry_resp}")
        tg_send(f"❌ Entry rejected {symbol}\n<code>{entry_resp}</code>")
        return
    entry_id = _order_id(entry_resp)

    exit_kw = dict(
        security_id=sec_id,
        exchange_segment=getattr(dhan, "NSE", "NSE_EQ"),
        transaction_type=getattr(dhan, exit_side, exit_side),
        quantity=qty,
        product_type=getattr(dhan, "INTRA", "INTRADAY"),
    )

    # 2) SL-M
    sl_id = None
    try:
        sl_resp = dhan.place_order(
            **exit_kw,
            order_type=getattr(dhan, "SLM", "STOP_LOSS_MARKET"),
            price=0,
            trigger_price=sl_px,
        )
        sl_id = _order_id(sl_resp)
    except Exception as e:
        log.error(f"[{symbol}] SL fail: {e}")
        tg_send(f"⚠️ SL failed {symbol}: {e}")

    # 3) Target LIMIT
    tgt_id = None
    try:
        tgt_resp = dhan.place_order(
            **exit_kw,
            order_type=getattr(dhan, "LIMIT", "LIMIT"),
            price=tgt_px,
        )
        tgt_id = _order_id(tgt_resp)
    except Exception as e:
        log.error(f"[{symbol}] TGT fail: {e}")
        tg_send(f"⚠️ TGT failed {symbol}: {e}")

    _OPEN_POSITIONS[symbol] = {
        "entry_id": entry_id, "sl_id": sl_id, "tgt_id": tgt_id,
        "qty": qty, "entry": entry_px, "sl": sl_px, "target": tgt_px,
        "side": side, "opened_at": now_ist(),
    }
    _TRADED_TODAY.add(symbol)

    tg_send(
        f"✅ <b>ORDER PLACED</b> [{sig['pattern']}]\n"
        f"<b>{symbol}</b> {side} ×{qty}\n"
        f"Entry~₹{entry_px}  SL ₹{sl_px}  🎯 ₹{tgt_px}\n"
        f"IDs: {entry_id} / {sl_id} / {tgt_id}"
    )


def monitor_oco(dhan) -> None:
    """Cancel the other leg when SL or Target fills. Also cleans dict."""
    if not _OPEN_POSITIONS:
        return
    done = []
    for sym, pos in list(_OPEN_POSITIONS.items()):
        sl_st  = _order_status(dhan, pos["sl_id"])  if pos["sl_id"]  else ""
        tgt_st = _order_status(dhan, pos["tgt_id"]) if pos["tgt_id"] else ""

        if "TRADED" in sl_st or "EXECUTED" in sl_st or "FILLED" in sl_st:
            _cancel(dhan, pos["tgt_id"])
            tg_send(f"🛑 SL hit: <b>{sym}</b> @ ₹{pos['sl']}")
            done.append(sym)
        elif "TRADED" in tgt_st or "EXECUTED" in tgt_st or "FILLED" in tgt_st:
            _cancel(dhan, pos["sl_id"])
            tg_send(f"🎉 Target hit: <b>{sym}</b> @ ₹{pos['target']}")
            done.append(sym)
        elif "REJECTED" in sl_st and "REJECTED" in tgt_st:
            tg_send(f"⚠️ Both exits rejected for <b>{sym}</b>, cleaning up")
            done.append(sym)

    for sym in done:
        _OPEN_POSITIONS.pop(sym, None)


# =====================================================================
# 7. MAIN LOOP (continuous, IST-aware)
# =====================================================================
def wait_until(target_t: dtime) -> None:
    while True:
        now = now_ist()
        if now.time() >= target_t:
            return
        wait_s = (datetime.combine(now.date(), target_t, tzinfo=IST) - now).total_seconds()
        log.info(f"Waiting {int(wait_s)}s until {target_t}")
        time.sleep(min(max(wait_s, 1), 60))


def scan_once(dhan, universe: pd.DataFrame) -> pd.DataFrame:
    signals: list[dict] = []
    log.info(f"Scan pass @ {now_ist().strftime('%H:%M:%S')} on {len(universe)} stocks")

    for i, row in enumerate(universe.itertuples(index=False)):
        if not market_open_now():
            break
        df = fetch_intraday(dhan, row.security_id)
        if df is not None:
            hits = detect_patterns(df)
            if hits:
                # Optional confirmation: last CLOSED bar must break prior bar's H/L
                if REQUIRE_CONFIRMATION and len(df) >= 2:
                    prev_h, prev_l = df["high"].iloc[-2], df["low"].iloc[-2]
                    close_now = df["close"].iloc[-1]
                    hits = [
                        h for h in hits
                        if (h[0] > 0 and close_now > prev_h) or
                           (h[0] < 0 and close_now < prev_l) or
                           h[0] == 0
                    ]
                if hits:
                    signals.extend(score_signals(row.symbol, row.security_id, df, hits))
        time.sleep(REQUEST_SLEEP_SEC)
        if (i + 1) % 25 == 0:
            log.info(f"  processed {i + 1}/{len(universe)}")

    if not signals:
        return pd.DataFrame()

    # keep best-scored signal per symbol (avoid duplicate patterns per bar)
    ranked = (pd.DataFrame(signals)
              .sort_values(["score", "vol_ratio", "strength"], ascending=[False, False, False])
              .drop_duplicates(subset=["symbol"], keep="first")
              .reset_index(drop=True))
    return ranked


def act_on_signals(dhan, ranked: pd.DataFrame) -> None:
    if ranked.empty:
        return
    # Compute NIFTY regime ONCE per scan (not per symbol)
    ntrend = get_nifty_trend(dhan) if NIFTY_GATE_ENABLED else 0
    if NIFTY_GATE_ENABLED:
        log.info(f"NIFTY trend: {ntrend:+d}  (strict={NIFTY_STRICT})")
    for _, sig in ranked.head(TOP_N_RESULTS).iterrows():
        direction = 1 if sig["signal"] == "BUY" else -1
        if not passes_nifty_gate(direction, ntrend):
            log.debug(f"[{sig['symbol']}] rejected by NIFTY gate "
                      f"(dir={direction}, ntrend={ntrend})")
            continue
        tg_send(
            f"📊 <b>{sig['symbol']}</b> {sig['signal']} [{sig['pattern']}]\n"
            f"₹{sig['price']} | Score {sig['score']} | Vol×{sig['vol_ratio']} "
            f"| VWAP {sig.get('vwap')} | {sig['time']}",
            silent=True,
        )
        if sig["score"] >= MIN_SCORE_TO_TRADE and in_session_for_entries():
            place_bracket_orders(dhan, sig.to_dict())


def run() -> None:
    # ---- Auto-apply live_config.json if present (safe no-op otherwise) ----
    try:
        from live_config import apply_live_config
        applied = apply_live_config(
            module_name="intraday_pattern_scanner_v2",
            tg_sender=tg_send,
        )
        if applied:
            log.info("Live config applied successfully.")
    except ImportError:
        log.debug("live_config module not found — using hardcoded defaults.")
    except Exception as e:
        log.warning(f"live_config apply failed ({e}) — using hardcoded defaults.")

    if DHAN_SDK_V2:
        ctx = DhanContext(CLIENT_ID, ACCESS_TOKEN)
        dhan = dhanhq(ctx)
    else:
        dhan = dhanhq(CLIENT_ID, ACCESS_TOKEN)   # type: ignore

    universe = load_intraday_universe()

    # Wait for scan start (skip opening noise)
    wait_until(SCAN_START)

    tg_send(
        f"🚀 <b>Scanner started</b>\n"
        f"Universe: {len(universe)} | Interval: {CANDLE_INTERVAL_MIN}m\n"
        f"Auto-trade: {'ON ✅' if AUTO_TRADE_ENABLED else 'OFF (dry-run) 🧪'}\n"
        f"RR: 1:{RISK_REWARD_RATIO} | Max risk/trade: ₹{MAX_RISK_PER_TRADE}\n"
        f"Filters: VWAP + EMA20 + Confirmation={REQUIRE_CONFIRMATION}"
    )

    all_signals_today: list[pd.DataFrame] = []
    next_scan_at = now_ist()

    while market_open_now():
        # Time-slice: run scan every 5 min (aligned to bar close)
        now = now_ist()
        if now >= next_scan_at:
            try:
                ranked = scan_once(dhan, universe)
                if not ranked.empty:
                    all_signals_today.append(ranked)
                    print(f"\n=== Signals @ {now.strftime('%H:%M')} ===")
                    print(ranked.head(TOP_N_RESULTS).to_string(index=False))
                    act_on_signals(dhan, ranked)
            except Exception as e:
                log.exception("scan_once error")
                tg_send(f"⚠️ Scan error: {e}")

            # schedule next scan on next 5-min bar close (+ tiny buffer)
            minute_slot = (now.minute // CANDLE_INTERVAL_MIN + 1) * CANDLE_INTERVAL_MIN
            next_scan_at = now.replace(second=5, microsecond=0) + timedelta(
                minutes=minute_slot - now.minute
            )

        # OCO monitoring on each iteration
        try:
            monitor_oco(dhan)
        except Exception as e:
            log.debug(f"monitor_oco error: {e}")

        time.sleep(POSITION_POLL_SEC)

    # ---- EOD summary ----
    if all_signals_today:
        combined = pd.concat(all_signals_today, ignore_index=True)
        fname = f"signals_{now_ist().strftime('%Y%m%d')}.csv"
        combined.to_csv(fname, index=False)
        log.info(f"Saved {len(combined)} signals -> {fname}")
        tg_send(f"📈 <b>EOD</b>: {len(combined)} signals | traded {len(_TRADED_TODAY)}")
    else:
        tg_send("ℹ️ EOD: no actionable signals today.")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log.exception("Fatal error")
        tg_send(f"🚨 <b>Fatal error</b>: {e}")
        raise
