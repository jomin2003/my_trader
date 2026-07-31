"""
=====================================================================
DHAN INTRADAY SCANNER + AUTO-TRADER + TELEGRAM  (v6 — Kronos-adaptive exits)
=====================================================================
Runs 4 strategies via multi_strategy_live.py:
  - OB SHORTS      - shooting star at bear OB zones (validated)
  - ORB            - opening range breakout, both directions
  - GAP-FILL       - fade 1-3% gaps back to prev close
  - CANDLE-STRUCT  - candlestick reversals with STRUCTURE-BASED exits

TRADE-MANAGEMENT FIXES (v3):
  #1 EOD force-exit FLATTENS + records PnL.  #2 fills/trail/partial every poll.
  #3 partial resizes remaining legs.  #4 trail places new SL before cancel.
  #5 all state under _POS_LOCK.  #6 R pinned to init_sl_dist.
  #7 atr None-safe.  #8 partials recorded.  #9 MAX_OPEN_POSITIONS live.
  #10 paper PnL uses backtest cost model.  #11 MARKET_CLOSE 15:30.
  #12 consistent keys.  #13 paper fills simulated off bar high/low.

PROFIT-CAPTURE EXITS (v3):
  PEAK_EXIT / TIME_PROFIT / TIME_STALE for trades that drag.

HUMAN-LIKE DISCIPLINE (v4):
  BREAKEVEN stop, DAILY CIRCUIT breakers, LOSS COOLDOWN, UNREALISED P&L.

KRONOS AI GATE (v5):
  Reads a Gist forecast (published by a free GitHub Actions job) via
  kronos_gate.py. soft = score +/- boost; strict = veto disagreements.

KRONOS-ADAPTIVE EXITS (v6 — NEW):
  kronos_exits.adjust_exits() scales the SL distance by Kronos's predicted
  volatility and caps the target near Kronos's expected move — so exits are
  FORWARD-looking instead of lagging ATR. Safe no-op when Kronos has no view.

DAILY REPORT (Telegram /report, /pnl, EOD push) with full blotter.

AUTO_TRADE_ENABLED = False (paper trading). Keep False 2 weeks.
Requires alongside this file: multi_strategy_live.py, structure_levels.py,
ob_data.csv, gap_data.csv  (kronos_gate.py + kronos_exits.py optional)
"""
from __future__ import annotations

import io
import os
import time
import logging
import threading
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

try:
    from dhanhq import DhanContext, dhanhq
    DHAN_SDK_V2 = True
except ImportError:
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

AUTO_TRADE_ENABLED    = False   # KEEP FALSE for paper trading
MIN_SCORE_TO_TRADE    = 6
RISK_REWARD_RATIO     = 2.0     # used only when a signal has no struct exits
MAX_RISK_PER_TRADE    = 500
MAX_CAPITAL_PER_TRADE = 25000
MAX_OPEN_POSITIONS    = 5
USE_ATR_STOP          = True
ATR_MULTIPLIER        = 1.5
FALLBACK_SL_PERCENT   = 0.005
MIN_SL_PCT            = 0.003
MAX_SL_PCT            = 0.015
REQUIRE_CONFIRMATION  = False

USE_FNO_UNIVERSE_ONLY = True
MAX_STOCKS            = 180
CANDLE_INTERVAL_MIN   = 5
MIN_CANDLES_NEEDED    = 4
TOP_N_RESULTS         = 20
MIN_TURNOVER_LAKHS    = 25

NIFTY_GATE_ENABLED    = True
NIFTY_STRICT          = False
_NIFTY_SEC_ID         = "13"
_NIFTY_EXCH_SEG       = "IDX_I"
_NIFTY_INSTR_TYPE     = "INDEX"

REQUEST_SLEEP_SEC     = 0.22
BACKOFF_ON_ERROR_SEC  = 2.0

# ---- Cost model (MUST match backtest_harness for paper==backtest) ----
BROKERAGE_PER_TRADE   = 0        # Dhan zero brokerage on equity intraday
SLIPPAGE_BPS          = 3        # 0.03% each way
TAXES_BPS_ONEWAY      = 6        # STT+GST+SEBI+stamp ~ 0.06%

# Thread-safety
_CONFIG_LOCK = threading.Lock()   # protects config globals during monkey-patch
_POS_LOCK    = threading.Lock()   # protects positions + blotter (fix #5)

IST             = ZoneInfo("Asia/Kolkata")
MARKET_OPEN     = dtime(9, 15)
SCAN_START      = dtime(9, 30)
NO_ENTRY_AFTER  = dtime(14, 30)
MARKET_CLOSE    = dtime(15, 30)   # fix #11: aligned to app.py

POSITION_POLL_SEC     = 20
OCO_TIMEOUT_SEC       = 300       # only used for the "stuck" warning now
FORCE_EXIT_TIME       = dtime(15, 15)

# Trailing stop and partial profit-taking
TRAILING_STOP_ENABLED = True
TRAILING_ATR_MULT     = 1.0       # trail distance = this * ATR
TRAIL_ACTIVATE_R      = 1.0       # activate trail after 1R in profit
PARTIAL_EXIT_ENABLED  = True
PARTIAL_EXIT_R        = 1.0       # take partial at 1R
PARTIAL_EXIT_FRACTION = 0.5       # exit 50% of position

# ---- Breakeven stop (v4): never let a winner turn into a loser ----
BREAKEVEN_ENABLED     = True
BREAKEVEN_TRIGGER_R   = 0.7       # once +0.7R in profit...
BREAKEVEN_BUFFER_R    = 0.05      # ...move SL to entry + this * risk (covers costs)

# ---- Time-based / peak exits: capture profit on trades that drag ----
TIME_EXIT_ENABLED       = True
TIME_EXIT_MINUTES       = 45      # after this hold, exit if in profit
TIME_EXIT_MIN_PROFIT_R  = 0.3     # ...but only if >= this profit (in R)
TIME_EXIT_MAX_MINUTES   = 120     # after this, exit regardless (stale)
PEAK_GIVEBACK_ENABLED   = True
PEAK_GIVEBACK_ARM_R     = 0.8     # arm once trade reached this profit (R)
PEAK_GIVEBACK_FRACTION  = 0.35    # exit if it gives back this % of the peak

# ---- Daily circuit breakers (v4): walk away on a bad day ----
DAILY_MAX_LOSS          = 1500    # halt new entries if realized net <= -this (0=off)
DAILY_PROFIT_TARGET     = 0       # halt after banking this much (0=off, let winners run)
DAILY_MAX_TRADES        = 8       # max positions opened per day (0=off)
MAX_CONSECUTIVE_LOSSES  = 3       # halt after this many losing trades in a row (0=off)
LOSS_COOLDOWN_MINUTES   = 30      # after a loss in a symbol, block re-entry this long

INSTRUMENT_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("dhan_scanner")

# =====================================================================
# HELPERS
# =====================================================================
def now_ist() -> datetime:
    return datetime.now(IST)

def in_session_for_entries() -> bool:
    return SCAN_START <= now_ist().time() <= NO_ENTRY_AFTER

def market_open_now() -> bool:
    return MARKET_OPEN <= now_ist().time() <= MARKET_CLOSE

# =====================================================================
# NIFTY TREND GATE
# =====================================================================
def get_nifty_trend(dhan) -> int:
    today = now_ist().strftime("%Y-%m-%d")
    try:
        resp = dhan.intraday_minute_data(
            security_id=_NIFTY_SEC_ID, exchange_segment=_NIFTY_EXCH_SEG,
            instrument_type=_NIFTY_INSTR_TYPE, from_date=today, to_date=today,
            interval=CANDLE_INTERVAL_MIN)
    except Exception as e:
        log.debug(f"NIFTY fetch failed: {e}"); return 0
    data = resp.get("data", resp) if isinstance(resp, dict) else {}
    if not isinstance(data, dict) or not data.get("close"):
        return 0
    df = pd.DataFrame({"high": data["high"], "low": data["low"],
                       "close": data["close"],
                       "volume": data.get("volume", [0] * len(data["close"]))})
    if len(df) >= 2:
        df = df.iloc[:-1]          # drop forming bar
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
    if c > vwap and c > ema20:
        return +1
    if c < vwap and c < ema20:
        return -1
    return 0

def passes_nifty_gate(direction, ntrend):
    if not NIFTY_GATE_ENABLED:
        return True
    if NIFTY_STRICT:
        return (direction > 0 and ntrend == +1) or (direction < 0 and ntrend == -1)
    return (direction > 0 and ntrend >= 0) or (direction < 0 and ntrend <= 0)

# =====================================================================
# TELEGRAM
# =====================================================================
def tg_send(text, silent=False):
    if not TELEGRAM_ENABLED:
        return
    if TELEGRAM_BOT_TOKEN.startswith("YOUR_") or TELEGRAM_CHAT_ID.startswith("YOUR_"):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000],
                  "parse_mode": "HTML", "disable_notification": silent},
            timeout=5)
    except Exception as e:
        log.debug(f"Telegram send failed: {e}")

# =====================================================================
# UNIVERSE
# =====================================================================
def load_intraday_universe() -> pd.DataFrame:
    log.info("Downloading Dhan instrument master ...")
    resp = requests.get(INSTRUMENT_MASTER_URL, timeout=30); resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), low_memory=False)
    cols = {c.upper(): c for c in df.columns}; C = lambda n: cols[n.upper()]
    eq = df[(df[C("SEM_EXM_EXCH_ID")].astype(str).str.upper() == "NSE") &
            (df[C("SEM_SEGMENT")].astype(str).str.upper() == "E") &
            (df[C("SEM_INSTRUMENT_NAME")].astype(str).str.upper() == "EQUITY")].copy()
    if USE_FNO_UNIVERSE_ONLY:
        fno = df[(df[C("SEM_EXM_EXCH_ID")].astype(str).str.upper() == "NSE") &
                 (df[C("SEM_INSTRUMENT_NAME")].astype(str).str.upper().isin(["FUTSTK", "OPTSTK"]))]
        und = fno[C("SEM_TRADING_SYMBOL")].astype(str).str.split("-").str[0].str.upper().unique()
        eq = eq[eq[C("SEM_TRADING_SYMBOL")].astype(str).str.upper().isin(und)]
    eq = (eq.drop_duplicates(subset=[C("SEM_TRADING_SYMBOL")])
            .sort_values(by=C("SEM_TRADING_SYMBOL")).head(MAX_STOCKS).copy())
    out = pd.DataFrame({"security_id": eq[C("SEM_SMST_SECURITY_ID")].astype(str),
                        "symbol": eq[C("SEM_TRADING_SYMBOL")].astype(str)}).reset_index(drop=True)
    log.info(f"Final intraday universe: {len(out)} stocks")
    return out

# =====================================================================
# HISTORICAL DATA
# =====================================================================
def fetch_intraday(dhan, security_id):
    today = now_ist().strftime("%Y-%m-%d"); resp = None
    try:
        resp = dhan.intraday_minute_data(security_id=security_id, exchange_segment="NSE_EQ",
            instrument_type="EQUITY", from_date=today, to_date=today, interval=CANDLE_INTERVAL_MIN)
    except TypeError:
        try:
            resp = dhan.intraday_minute_data(security_id, "NSE_EQ", "EQUITY", today, today, CANDLE_INTERVAL_MIN)
        except Exception as e:
            log.debug(f"[{security_id}] fallback error: {e}"); time.sleep(BACKOFF_ON_ERROR_SEC); return None
    except Exception as e:
        log.debug(f"[{security_id}] fetch error: {e}"); time.sleep(BACKOFF_ON_ERROR_SEC); return None
    if not isinstance(resp, dict):
        return None
    data = resp.get("data") if resp.get("data") else resp
    if not isinstance(data, dict) or "open" not in data or not data["open"]:
        return None
    df = pd.DataFrame({"open": data["open"], "high": data["high"], "low": data["low"],
                       "close": data["close"], "volume": data.get("volume", [0] * len(data["open"]))})
    ts = data.get("timestamp") or data.get("start_Time") or data.get("startTime")
    if ts:
        try:
            df["ts"] = pd.to_datetime(ts, unit="s", utc=True).tz_convert(IST)
        except Exception:
            df["ts"] = pd.NaT
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    if len(df) < MIN_CANDLES_NEEDED:
        return None
    df = df.iloc[:-1].reset_index(drop=True)  # drop forming bar
    return df if len(df) >= MIN_CANDLES_NEEDED - 1 else None

def _latest_bar(dhan, security_id):
    """Return the most recent CLOSED 5-min bar (high/low/close) or None."""
    if not security_id:
        return None
    df = fetch_intraday(dhan, security_id)
    if df is None or df.empty:
        return None
    last = df.iloc[-1]
    return {"high": float(last["high"]), "low": float(last["low"]),
            "close": float(last["close"])}

# =====================================================================
# INDICATORS (used by multi_strategy_live + structure engine)
# =====================================================================
def wilder_atr(df, period=14):
    if len(df) < period + 1:
        return None
    h = df["high"].values.astype(float); l = df["low"].values.astype(float); c = df["close"].values.astype(float)
    prev = np.concatenate([[c[0]], c[:-1]]); tr = np.maximum.reduce([h - l, np.abs(h - prev), np.abs(l - prev)])
    atr = np.zeros_like(tr); atr[period - 1] = tr[:period].mean()
    for i in range(period, len(tr)):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period
    v = float(atr[-1]); return v if v > 0 else None

def rolling_vwap(df):
    if len(df) < 3:
        return None
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].replace(0, np.nan)
    if vol.sum() <= 0 or vol.isna().all():
        return None
    return float((tp * df["volume"]).sum() / max(df["volume"].sum(), 1))

def ema(series, span):
    if len(series) < span:
        return None
    return float(series.ewm(span=span, adjust=False).mean().iloc[-1])

def adx(df, period=14):
    "Compute ADX (Average Directional Index). Returns ADX value or None."
    if len(df) < period * 2:
        return None
    h = df["high"].values.astype(float); l = df["low"].values.astype(float); c = df["close"].values.astype(float)
    prev_h = np.concatenate([[h[0]], h[:-1]]); prev_l = np.concatenate([[l[0]], l[:-1]])
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
    plus_dm = np.where((h - prev_h) > (prev_l - l), np.maximum(h - prev_h, 0), 0.0)
    minus_dm = np.where((prev_l - l) > (h - prev_h), np.maximum(prev_l - l, 0), 0.0)
    atr_s = np.zeros_like(tr); atr_s[period - 1] = tr[:period].mean()
    pdm_s = np.zeros_like(tr); pdm_s[period - 1] = plus_dm[:period].mean()
    ndm_s = np.zeros_like(tr); ndm_s[period - 1] = minus_dm[:period].mean()
    for i in range(period, len(tr)):
        atr_s[i] = (atr_s[i - 1] * (period - 1) + tr[i]) / period
        pdm_s[i] = (pdm_s[i - 1] * (period - 1) + plus_dm[i]) / period
        ndm_s[i] = (ndm_s[i - 1] * (period - 1) + minus_dm[i]) / period
    with np.errstate(divide='ignore', invalid='ignore'):
        plus_di = 100.0 * pdm_s / np.where(atr_s > 0, atr_s, 1e-9)
        minus_di = 100.0 * ndm_s / np.where(atr_s > 0, atr_s, 1e-9)
        dx = 100.0 * np.abs(plus_di - minus_di) / np.where(plus_di + minus_di > 0, plus_di + minus_di, 1e-9)
    adx_arr = np.zeros_like(dx); start = 2 * period - 1
    if start >= len(dx):
        return None
    adx_arr[start] = dx[period:start + 1].mean()
    for i in range(start + 1, len(dx)):
        adx_arr[i] = (adx_arr[i - 1] * (period - 1) + dx[i]) / period
    return float(adx_arr[-1]) if adx_arr[-1] > 0 else None

def detect_patterns(df): return []       # replaced at import
def score_signals(symbol, security_id, df, hits): return []   # replaced at import

# =====================================================================
# STRATEGY OVERRIDE — 4 strategies via multi_strategy_live
# =====================================================================
import multi_strategy_live
detect_patterns = multi_strategy_live.detect_patterns
score_signals   = multi_strategy_live.score_signals
log.info("[SCANNER] Multi-strategy active: OB Shorts + ORB + Gap-Fill + Candle-Struct")

# ---- Kronos confirmation gate (reads forecast from Gist; no torch) ----
try:
    import kronos_gate
    _KRONOS_OK = True
    log.info("[SCANNER] Kronos gate active")
except Exception as _e:
    _KRONOS_OK = False
    log.info(f"[SCANNER] Kronos gate unavailable ({_e})")

# ---- Kronos-adaptive SL/target sizing (forward-looking exits) ----
try:
    import kronos_exits
    _KEXIT_OK = True
    log.info("[SCANNER] Kronos-adaptive exits active")
except Exception as _e:
    _KEXIT_OK = False
    log.info(f"[SCANNER] Kronos-adaptive exits unavailable ({_e})")

# ---- Per-strategy exit tuning (safe no-op fallback to globals) ----
try:
    import strategy_exits
    _SEXIT_OK = True
    log.info("[SCANNER] Per-strategy exits active")
except Exception as _e:
    _SEXIT_OK = False
    log.info(f"[SCANNER] Per-strategy exits unavailable ({_e})")

# =====================================================================
# STATE  (all mutated under _POS_LOCK — fix #5)
# =====================================================================
_OPEN_POSITIONS: dict[str, dict] = {}
_TRADED_TODAY:  set[str]         = set()
_COMPLETED_TRADES: list[dict]    = []   # finals + partials, with PnL
_SIGNALS_TODAY:  list[dict]      = []   # every suggestion the bot surfaced
_POSITIONS_OPENED = 0                    # count of positions opened today

# ---- human-discipline state (v4) ----
_TRADING_HALTED_TODAY = False            # circuit-breaker halt (auto)
_HALT_REASON          = ""
_CONSECUTIVE_LOSSES   = 0
_SYMBOL_COOLDOWN: dict[str, datetime] = {}   # symbol -> blocked-until time

# =====================================================================
# COST MODEL (fix #10)
# =====================================================================
def _apply_costs(entry_px, exit_px, qty, side):
    "Net PnL after slippage + taxes. Mirrors backtest_harness._apply_costs."
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
    return round(gross - taxes - BROKERAGE_PER_TRADE * 2, 2)

# =====================================================================
# BLOTTER
# =====================================================================
def record_suggestion(sig: dict):
    "Log every signal the bot surfaces (whether or not it becomes a trade)."
    with _POS_LOCK:
        _SIGNALS_TODAY.append({
            "symbol":   sig.get("symbol"),
            "strategy": sig.get("strategy", "?"),
            "signal":   sig.get("signal"),
            "pattern":  sig.get("pattern", ""),
            "price":    sig.get("price"),
            "score":    sig.get("score"),
            "vol_ratio": sig.get("vol_ratio"),
            "kronos":   sig.get("kronos", ""),
            "time":     sig.get("time"),
            "acted":    False,
        })

def _mark_suggestion_acted(symbol, strategy):
    for s in _SIGNALS_TODAY:
        if s["symbol"] == symbol and s["strategy"] == strategy and not s["acted"]:
            s["acted"] = True
            break

def _record_trade(sym, pos, outcome, exit_price, qty, pnl):
    "Append a completed (or partial) trade row to the blotter. Call under lock."
    direction = 1 if pos["side"] == "BUY" else -1
    init_sl_dist = max(pos.get("init_sl_dist", 0.0), 1e-9)
    r_multiple = round(((exit_price - pos["entry"]) * direction) / init_sl_dist, 2)
    _COMPLETED_TRADES.append({
        "symbol":   sym,
        "strategy": pos.get("strategy", "?"),
        "side":     pos["side"],
        "outcome":  outcome,   # SL/TARGET/PARTIAL/EOD_FORCED/TIME_PROFIT/TIME_STALE/PEAK_EXIT
        "entry":    round(pos["entry"], 2),
        "exit":     round(exit_price, 2),
        "qty":      int(qty),
        "pnl":      round(pnl, 2),
        "r":        r_multiple,
        "kexit":    pos.get("kexit", ""),
        "time":     now_ist().strftime("%H:%M:%S"),
    })

def _finalize(sym):
    "Remove a fully-closed position. Call under lock."
    _OPEN_POSITIONS.pop(sym, None)

def reset_day():
    "Clear the day's state. Called at the day boundary by app.py."
    global _POSITIONS_OPENED, _TRADING_HALTED_TODAY, _HALT_REASON, _CONSECUTIVE_LOSSES
    with _POS_LOCK:
        if _OPEN_POSITIONS:
            log.warning(f"reset_day: {len(_OPEN_POSITIONS)} position(s) still open, clearing")
            _OPEN_POSITIONS.clear()
        _SIGNALS_TODAY.clear()
        _COMPLETED_TRADES.clear()
        _TRADED_TODAY.clear()
        _SYMBOL_COOLDOWN.clear()
        _POSITIONS_OPENED = 0
        _TRADING_HALTED_TODAY = False
        _HALT_REASON = ""
        _CONSECUTIVE_LOSSES = 0
    log.info("Day state reset (signals, trades, positions, halts cleared)")

# =====================================================================
# CIRCUIT BREAKERS (v4) — walk away like a disciplined human
# =====================================================================
def _realized_net():
    with _POS_LOCK:
        return sum(t["pnl"] for t in _COMPLETED_TRADES)

def _evaluate_halt():
    "Set the day-halt flag if any circuit breaker trips. Alerts once."
    global _TRADING_HALTED_TODAY, _HALT_REASON
    if _TRADING_HALTED_TODAY:
        return True
    net = _realized_net()
    reason = None
    if DAILY_MAX_LOSS > 0 and net <= -abs(DAILY_MAX_LOSS):
        reason = f"daily max loss hit (net ₹{net:+,.0f})"
    elif DAILY_PROFIT_TARGET > 0 and net >= DAILY_PROFIT_TARGET:
        reason = f"daily profit target hit (net ₹{net:+,.0f})"
    elif MAX_CONSECUTIVE_LOSSES > 0 and _CONSECUTIVE_LOSSES >= MAX_CONSECUTIVE_LOSSES:
        reason = f"{_CONSECUTIVE_LOSSES} losing trades in a row"
    if reason:
        _TRADING_HALTED_TODAY = True
        _HALT_REASON = reason
        tg_send(f"🛑 <b>Auto-halt for the day</b>: {reason}.\n"
                f"No new entries. Open trades still managed. Use /resume to override.")
        log.warning(f"Auto-halt: {reason}")
    return _TRADING_HALTED_TODAY

def resume_trading():
    "Manual override (called by /resume) — clears the auto-halt for today."
    global _TRADING_HALTED_TODAY, _HALT_REASON, _CONSECUTIVE_LOSSES
    _TRADING_HALTED_TODAY = False
    _HALT_REASON = ""
    _CONSECUTIVE_LOSSES = 0
    log.info("Auto-halt cleared via resume")

def halt_status():
    "Return (halted, reason) for status/report."
    return _TRADING_HALTED_TODAY, _HALT_REASON

def _entry_blocked(symbol) -> str | None:
    """Return a reason string if we must NOT open a new trade, else None.
       Enforces halt, daily trade cap, position cap, dedupe, cooldown."""
    if _TRADING_HALTED_TODAY:
        return f"halted ({_HALT_REASON})"
    if DAILY_MAX_TRADES > 0 and _POSITIONS_OPENED >= DAILY_MAX_TRADES:
        return f"daily trade cap ({DAILY_MAX_TRADES}) reached"
    if len(_OPEN_POSITIONS) >= MAX_OPEN_POSITIONS:
        return f"max open positions ({MAX_OPEN_POSITIONS})"
    if symbol in _OPEN_POSITIONS:
        return "already in a position"
    cd = _SYMBOL_COOLDOWN.get(symbol)
    if cd and now_ist() < cd:
        return f"cooldown until {cd:%H:%M}"
    return None

# =====================================================================
# ORDER PLUMBING
# =====================================================================
def compute_sl_target(entry, direction, atr_val, strategy=None):
    # per-strategy multipliers; falls back to globals if unavailable/unknown
    if _SEXIT_OK:
        sl_mult, rr, _tm = strategy_exits.get_exit_params(
            strategy, ATR_MULTIPLIER, RISK_REWARD_RATIO, TRAILING_ATR_MULT)
    else:
        sl_mult, rr = ATR_MULTIPLIER, RISK_REWARD_RATIO
    if USE_ATR_STOP and atr_val and atr_val > 0:
        sl_dist = sl_mult * atr_val
    else:
        sl_dist = FALLBACK_SL_PERCENT * entry
    sl_dist = max(MIN_SL_PCT * entry, min(sl_dist, MAX_SL_PCT * entry))
    if direction > 0:
        sl = round(entry - sl_dist, 2); tgt = round(entry + rr * sl_dist, 2)
    else:
        sl = round(entry + sl_dist, 2); tgt = round(entry - rr * sl_dist, 2)
    return sl, tgt, sl_dist

def compute_quantity(entry, sl_dist):
    if sl_dist <= 0 or entry <= 0:
        return 0
    return max(0, min(int(MAX_RISK_PER_TRADE // sl_dist), int(MAX_CAPITAL_PER_TRADE // entry)))

def _order_id(resp):
    if not isinstance(resp, dict):
        return None
    data = resp.get("data") or {}
    return (data.get("orderId") or data.get("order_id") or
            resp.get("orderId") or resp.get("order_id"))

def _order_status(dhan, oid):
    try:
        r = dhan.get_order_by_id(oid)
        if isinstance(r, dict):
            d = r.get("data") or r
            return str(d.get("orderStatus") or d.get("status") or "").upper()
    except Exception as e:
        log.debug(f"status fail {oid}: {e}")
    return ""

def _cancel(dhan, oid):
    if not oid:
        return
    try:
        dhan.cancel_order(oid)
    except Exception as e:
        log.debug(f"cancel fail {oid}: {e}")

def _place(dhan, sec_id, side, qty, order_type, price=0, trigger_price=None):
    "Thin wrapper around dhan.place_order with graceful attr fallbacks."
    kwargs = dict(
        security_id=sec_id,
        exchange_segment=getattr(dhan, "NSE", "NSE_EQ"),
        transaction_type=getattr(dhan, side, side),
        quantity=qty,
        product_type=getattr(dhan, "INTRA", "INTRADAY"),
        order_type=getattr(dhan, order_type, order_type),
        price=price,
    )
    if trigger_price is not None:
        kwargs["trigger_price"] = trigger_price
    return dhan.place_order(**kwargs)

def _market_flatten(dhan, pos):
    "Send a market order to close whatever qty remains (live only)."
    exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
    try:
        _place(dhan, pos.get("security_id", ""), exit_side, pos["qty"], "MARKET", price=0)
    except Exception as e:
        log.warning(f"[{pos.get('symbol')}] market flatten failed: {e}")

def _move_stop(dhan, sym, pos, new_sl, label):
    """Move the stop favorably. Paper: just update. Live: place new SLM first,
       confirm an id, THEN cancel old (no naked-position window). Fix #4."""
    if not AUTO_TRADE_ENABLED:
        pos["sl"] = new_sl
        log.info(f"[{sym}] PAPER {label} SL -> ₹{new_sl}")
        return True
    exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
    try:
        r = _place(dhan, pos.get("security_id", ""), exit_side, pos["qty"],
                   "SLM", price=0, trigger_price=new_sl)
        nid = _order_id(r)
        if not nid:
            log.warning(f"[{sym}] {label}: new SL returned no id, keeping old SL")
            return False
        _cancel(dhan, pos.get("sl_id"))
        pos["sl_id"] = nid; pos["sl"] = new_sl
        log.info(f"[{sym}] LIVE {label} SL -> ₹{new_sl}")
        return True
    except Exception as e:
        log.warning(f"[{sym}] {label} SL update failed (old SL still active): {e}")
        return False

# =====================================================================
# ENTRY
# =====================================================================
def place_bracket_orders(dhan, sig):
    "Open a position (paper or live). Records the suggestion as 'acted'."
    global _POSITIONS_OPENED
    symbol  = sig["symbol"]; sec_id = sig["security_id"]; entry_px = float(sig["price"])
    dirn    = int(sig["direction"]); atr_val = sig.get("atr"); strat = sig.get("strategy", "?")

    # ---- all entry gating in one place, under lock (v4) ----
    with _POS_LOCK:
        block = _entry_blocked(symbol)
        if block:
            log.info(f"[{symbol}] entry blocked: {block}")
            return

    # ---- structural exits if the signal carries them, else ATR ----
    struct_sl  = sig.get("struct_sl")
    struct_tgt = sig.get("struct_target")
    if struct_sl and struct_tgt:
        sl = float(struct_sl); tgt = float(struct_tgt); sl_dist = abs(entry_px - sl)
    else:
        sl, tgt, sl_dist = compute_sl_target(entry_px, dirn, atr_val, strat)

    if sl_dist <= 0:
        log.info(f"[{symbol}] invalid SL distance — skip")
        return

    # ---- Kronos-adaptive exits (v6): scale SL by forecast vol, cap target near exp_ret ----
    kexit_note = ""
    if _KEXIT_OK:
        try:
            new_sl_dist, tgt, kexit_note = kronos_exits.adjust_exits(
                symbol, dirn, entry_px, sl_dist, tgt, rr=RISK_REWARD_RATIO)
            sl_dist = new_sl_dist
            sl = round(entry_px - dirn * sl_dist, 2)   # recompute SL price from adjusted distance
            log.info(f"[{symbol}] {kexit_note} -> SL {sl} TGT {tgt}")
        except Exception as e:
            log.debug(f"[{symbol}] kexit skipped: {e}")

    qty = compute_quantity(entry_px, sl_dist)
    if qty <= 0:
        log.info(f"[{symbol}] qty=0 (risk/capital caps) — skip")
        return

    side = "BUY" if dirn > 0 else "SELL"
    atr_store = float(atr_val) if (atr_val and atr_val > 0) else None

    pos = {
        "symbol": symbol, "security_id": sec_id, "side": side,
        "entry": entry_px, "sl": sl, "target": tgt, "qty": qty,
        "init_sl_dist": sl_dist, "atr": atr_store, "strategy": strat,
        "pattern": sig.get("pattern", ""), "kexit": kexit_note,
        "sl_id": None, "tgt_id": None,
        "opened_at": now_ist(), "partial_done": False, "be_done": False,
        "best_fav": 0.0, "best_price": entry_px, "last_price": entry_px,
        "realized": 0.0,   # booked partial PnL, for accurate trade-total streak
    }

    if not AUTO_TRADE_ENABLED:
        # ---- PAPER: register virtual position; fills simulated in monitor_oco ----
        with _POS_LOCK:
            block = _entry_blocked(symbol)   # re-check under lock (race-safe)
            if block:
                log.info(f"[{symbol}] entry blocked (recheck): {block}"); return
            _OPEN_POSITIONS[symbol] = pos
            _POSITIONS_OPENED += 1
            _mark_suggestion_acted(symbol, strat)
        tg_send(f"🧪 <b>PAPER ENTRY</b> {symbol} {side} [{strat}]\n"
                f"₹{entry_px} | SL ₹{sl} | TGT ₹{tgt} | Qty {qty}", silent=True)
        log.info(f"[{symbol}] PAPER {side} qty={qty} entry={entry_px} sl={sl} tgt={tgt}")
        return

    # ---- LIVE: market entry, then SL (SLM) + target (LIMIT) ----
    try:
        entry_resp = _place(dhan, sec_id, side, qty, "MARKET", price=0)
    except Exception as e:
        log.error(f"[{symbol}] entry order failed: {e}")
        tg_send(f"⚠️ Entry FAILED {symbol} {side}: {str(e)[:150]}")
        return
    entry_id = _order_id(entry_resp)
    if not entry_id:
        log.error(f"[{symbol}] entry order returned no id: {entry_resp}")
        tg_send(f"⚠️ Entry no-id {symbol} {side}")
        return

    exit_side = "SELL" if side == "BUY" else "BUY"
    sl_id = tgt_id = None
    try:
        sl_resp = _place(dhan, sec_id, exit_side, qty, "SLM", price=0, trigger_price=sl)
        sl_id = _order_id(sl_resp)
    except Exception as e:
        log.error(f"[{symbol}] SL order failed: {e}")
    try:
        tgt_resp = _place(dhan, sec_id, exit_side, qty, "LIMIT", price=tgt)
        tgt_id = _order_id(tgt_resp)
    except Exception as e:
        log.error(f"[{symbol}] target order failed: {e}")

    pos["sl_id"] = sl_id; pos["tgt_id"] = tgt_id
    with _POS_LOCK:
        _OPEN_POSITIONS[symbol] = pos
        _POSITIONS_OPENED += 1
        _mark_suggestion_acted(symbol, strat)

    tg_send(f"💰 <b>LIVE ENTRY</b> {symbol} {side} [{strat}]\n"
            f"₹{entry_px} | SL ₹{sl} | TGT ₹{tgt} | Qty {qty}")
    log.info(f"[{symbol}] LIVE {side} qty={qty} entry={entry_px} sl_id={sl_id} tgt_id={tgt_id}")

# =====================================================================
# POSITION MANAGEMENT
# =====================================================================
def _resize_exit_legs(dhan, pos, qty):
    "Re-place SL + target legs for the reduced qty (live only). Fix #3."
    exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
    sec_id = pos.get("security_id", "")
    try:
        r = _place(dhan, sec_id, exit_side, qty, "SLM", price=0, trigger_price=pos["sl"])
        nid = _order_id(r)
        if nid:
            _cancel(dhan, pos.get("sl_id")); pos["sl_id"] = nid
    except Exception as e:
        log.warning(f"[{pos['symbol']}] resize SL leg failed: {e}")
    try:
        r = _place(dhan, sec_id, exit_side, qty, "LIMIT", price=pos["target"])
        tid = _order_id(r)
        if tid:
            _cancel(dhan, pos.get("tgt_id")); pos["tgt_id"] = tid
    except Exception as e:
        log.warning(f"[{pos['symbol']}] resize target leg failed: {e}")

def _update_peak(pos, high, low):
    "Track best favorable excursion so we can lock the highest profit."
    direction = 1 if pos["side"] == "BUY" else -1
    extreme = high if direction > 0 else low
    fav = (extreme - pos["entry"]) * direction
    if fav > pos.get("best_fav", 0.0):
        pos["best_fav"] = fav
        pos["best_price"] = extreme

def _update_breakeven(dhan, sym, pos, price):
    "Once +BREAKEVEN_TRIGGER_R, move SL to entry (+buffer). Never give back a winner."
    if not BREAKEVEN_ENABLED or pos.get("be_done"):
        return
    entry = pos["entry"]
    init_sl_dist = pos.get("init_sl_dist", abs(entry - pos["sl"]))
    direction = 1 if pos["side"] == "BUY" else -1
    if (price - entry) * direction < BREAKEVEN_TRIGGER_R * init_sl_dist:
        return
    buffer = BREAKEVEN_BUFFER_R * init_sl_dist
    new_sl = round(entry + direction * buffer, 2)
    if direction > 0 and new_sl <= pos["sl"]:
        pos["be_done"] = True; return
    if direction < 0 and new_sl >= pos["sl"]:
        pos["be_done"] = True; return
    if _move_stop(dhan, sym, pos, new_sl, "breakeven"):
        pos["be_done"] = True

def _time_based_exit(pos, price, now):
    """Decide whether to close a dragging trade. Returns a reason or None.
       PEAK_EXIT   : reached decent profit then gave back too much (any time)
       TIME_PROFIT : held too long and currently in profit -> lock it
       TIME_STALE  : held way too long -> cut it regardless
    """
    if not TIME_EXIT_ENABLED:
        return None
    init_sl_dist = max(pos.get("init_sl_dist", abs(pos["entry"] - pos["sl"])), 1e-9)
    direction = 1 if pos["side"] == "BUY" else -1
    profit_r = ((price - pos["entry"]) * direction) / init_sl_dist

    # 1) Peak give-back is PROFIT protection -> independent of hold time
    if PEAK_GIVEBACK_ENABLED:
        best_r = pos.get("best_fav", 0.0) / init_sl_dist
        if best_r >= PEAK_GIVEBACK_ARM_R and profit_r <= best_r * (1 - PEAK_GIVEBACK_FRACTION):
            return "PEAK_EXIT"

    opened = pos.get("opened_at")
    if not opened:
        return None
    held_min = (now - opened).total_seconds() / 60.0

    # 2) Dragging but currently in profit -> take what's there
    if held_min >= TIME_EXIT_MINUTES and profit_r >= TIME_EXIT_MIN_PROFIT_R:
        return "TIME_PROFIT"

    # 3) Stale trade -> cut it regardless of P/L
    if held_min >= TIME_EXIT_MAX_MINUTES:
        return "TIME_STALE"

    return None

def _update_trailing_stop(dhan, sym, pos, current_price):
    "Move SL toward price once past TRAIL_ACTIVATE_R. Fixes #4, #6, #7."
    if not TRAILING_STOP_ENABLED:
        return
    entry = pos["entry"]
    init_sl_dist = pos.get("init_sl_dist", abs(entry - pos["sl"]))   # fix #6
    direction = 1 if pos["side"] == "BUY" else -1
    profit = (current_price - entry) * direction
    if profit < TRAIL_ACTIVATE_R * init_sl_dist:
        return
    _tm = (strategy_exits.trail_mult_for(pos.get("strategy"), TRAILING_ATR_MULT)
           if _SEXIT_OK else TRAILING_ATR_MULT)
    atr_trail = (pos.get("atr") or init_sl_dist) * _tm  # fix #7 (per-strategy)
    if direction > 0:
        new_sl = round(current_price - atr_trail, 2)
        if new_sl <= pos["sl"]:
            return
    else:
        new_sl = round(current_price + atr_trail, 2)
        if new_sl >= pos["sl"]:
            return
    _move_stop(dhan, sym, pos, new_sl, "trailing")

def _partial_exit(dhan, sym, pos, current_price):
    "Exit a fraction at PARTIAL_EXIT_R. Fixes #3, #6, #8."
    if not PARTIAL_EXIT_ENABLED or pos.get("partial_done"):
        return
    entry = pos["entry"]
    init_sl_dist = pos.get("init_sl_dist", abs(entry - pos["sl"]))   # fix #6
    direction = 1 if pos["side"] == "BUY" else -1
    profit = (current_price - entry) * direction
    if profit < PARTIAL_EXIT_R * init_sl_dist:
        return
    exit_qty = max(1, int(pos["qty"] * PARTIAL_EXIT_FRACTION))
    remaining = pos["qty"] - exit_qty
    if remaining <= 0:
        return

    if AUTO_TRADE_ENABLED:
        exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
        try:
            _place(dhan, pos.get("security_id", ""), exit_side, exit_qty, "MARKET", price=0)
        except Exception as e:
            log.warning(f"[{sym}] partial exit failed: {e}")
            return
        _resize_exit_legs(dhan, pos, remaining)   # fix #3

    pnl = _apply_costs(entry, current_price, exit_qty, pos["side"])   # fix #8, #10
    with _POS_LOCK:
        _record_trade(sym, pos, "PARTIAL", current_price, exit_qty, pnl)
        pos["qty"] = remaining
        pos["partial_done"] = True
        pos["realized"] = pos.get("realized", 0.0) + pnl
    tg_send(f"📊 Partial exit {exit_qty} of {sym} @ ~₹{round(current_price,2)} "
            f"[{pos.get('strategy','?')}] P&L: ₹{pnl:+,.2f} | {remaining} left",
            silent=True)
    log.info(f"[{sym}] partial {exit_qty} @ {current_price} pnl={pnl}; {remaining} remaining")

def _paper_fill(pos, hi, lo):
    "Pessimistic same-bar fill check (SL first), mirrors backtest _simulate_bar."
    side = pos["side"]; sl = pos["sl"]; tgt = pos["target"]
    if side == "BUY":
        if lo <= sl:  return "SL"
        if hi >= tgt: return "TARGET"
    else:
        if hi >= sl:  return "SL"
        if lo <= tgt: return "TARGET"
    return None

def _close_and_record(dhan, sym, pos, outcome, exit_price, cancel_other=None):
    "Cancel opposite live leg, record PnL, update streak/cooldown, halt-check, alert."
    global _CONSECUTIVE_LOSSES
    if AUTO_TRADE_ENABLED and cancel_other:
        _cancel(dhan, pos.get(cancel_other))
    leg_pnl = _apply_costs(pos["entry"], exit_price, pos["qty"], pos["side"])
    trade_total = round(pos.get("realized", 0.0) + leg_pnl, 2)   # include partials
    with _POS_LOCK:
        _record_trade(sym, pos, outcome, exit_price, pos["qty"], leg_pnl)
        _finalize(sym)
        # human discipline: streak + cooldown based on the WHOLE trade's P&L
        if trade_total > 0:
            _CONSECUTIVE_LOSSES = 0
        else:
            _CONSECUTIVE_LOSSES += 1
            _SYMBOL_COOLDOWN[sym] = now_ist() + timedelta(minutes=LOSS_COOLDOWN_MINUTES)
    emoji = {"TARGET": "🎉", "SL": "🛑", "EOD_FORCED": "⏰",
             "TIME_PROFIT": "⌛", "TIME_STALE": "💤", "PEAK_EXIT": "🔒"}.get(outcome, "✅")
    tg_send(f"{emoji} {outcome} {sym} @ ₹{round(exit_price,2)} "
            f"[{pos.get('strategy','?')}] P&L: ₹{leg_pnl:+,.2f} "
            f"(trade ₹{trade_total:+,.2f}, {pos['side']})")
    log.info(f"[{sym}] {outcome} @ {exit_price} leg={leg_pnl} trade_total={trade_total}")
    _evaluate_halt()   # may auto-stop the day (outside lock; sends its own alert)

def _close_market_both_legs(dhan, sym, pos, outcome, exit_price):
    "Cancel BOTH live legs, market-flatten, then record. Used by time/peak/EOD exits."
    if AUTO_TRADE_ENABLED:
        _cancel(dhan, pos.get("sl_id")); _cancel(dhan, pos.get("tgt_id"))
        _market_flatten(dhan, pos)
    _close_and_record(dhan, sym, pos, outcome, exit_price)

def monitor_oco(dhan):
    "Manage every open position each poll. Handles paper + live. Fixes #1,#2,#5."
    with _POS_LOCK:
        syms = list(_OPEN_POSITIONS.keys())
    if not syms:
        return

    now = now_ist()
    force_exit = now.time() >= FORCE_EXIT_TIME

    for sym in syms:
        with _POS_LOCK:
            pos = _OPEN_POSITIONS.get(sym)
        if not pos:
            continue

        bar = _latest_bar(dhan, pos.get("security_id"))
        ltp = bar["close"] if bar else None
        if ltp is not None:
            pos["last_price"] = ltp   # for unrealized P&L in /report

        # ---- EOD force-flatten (fix #1) ----
        if force_exit:
            exit_px = ltp if ltp is not None else pos["entry"]
            _close_market_both_legs(dhan, sym, pos, "EOD_FORCED", exit_px)
            continue

        if AUTO_TRADE_ENABLED:
            # ---- LIVE: check real order status every poll (fix #2) ----
            sl_st  = _order_status(dhan, pos["sl_id"])  if pos.get("sl_id")  else ""
            tgt_st = _order_status(dhan, pos["tgt_id"]) if pos.get("tgt_id") else ""
            filled = ("TRADED", "EXECUTED", "FILLED")
            if any(k in sl_st for k in filled):
                _close_and_record(dhan, sym, pos, "SL", pos["sl"], cancel_other="tgt_id")
                continue
            if any(k in tgt_st for k in filled):
                _close_and_record(dhan, sym, pos, "TARGET", pos["target"], cancel_other="sl_id")
                continue
            if "REJECTED" in sl_st and "REJECTED" in tgt_st:
                tg_send(f"⚠️ Both exits rejected {sym}, cleaning up")
                with _POS_LOCK:
                    _finalize(sym)
                continue
            opened = pos.get("opened_at")
            if opened and (now - opened).total_seconds() > OCO_TIMEOUT_SEC:
                log.debug(f"[{sym}] open > {OCO_TIMEOUT_SEC}s (sl={sl_st} tgt={tgt_st})")

            if bar is not None:
                _update_peak(pos, bar["high"], bar["low"])
                reason = _time_based_exit(pos, bar["close"], now)
                if reason:
                    _close_market_both_legs(dhan, sym, pos, reason, bar["close"])
                    continue
            if ltp is not None:
                _update_breakeven(dhan, sym, pos, ltp)
                _partial_exit(dhan, sym, pos, ltp)
                with _POS_LOCK:
                    pos = _OPEN_POSITIONS.get(sym)
                if pos:
                    _update_trailing_stop(dhan, sym, pos, ltp)
        else:
            # ---- PAPER: simulate fills off the latest bar (fix #13) ----
            if bar is None:
                continue
            hit = _paper_fill(pos, bar["high"], bar["low"])
            if hit == "SL":
                _close_and_record(dhan, sym, pos, "SL", pos["sl"]); continue
            if hit == "TARGET":
                _close_and_record(dhan, sym, pos, "TARGET", pos["target"]); continue

            _update_peak(pos, bar["high"], bar["low"])
            reason = _time_based_exit(pos, bar["close"], now)
            if reason:
                _close_and_record(dhan, sym, pos, reason, bar["close"]); continue

            _update_breakeven(dhan, sym, pos, bar["close"])
            _partial_exit(dhan, sym, pos, bar["close"])
            with _POS_LOCK:
                pos = _OPEN_POSITIONS.get(sym)
            if pos:
                _update_trailing_stop(dhan, sym, pos, bar["close"])

# =====================================================================
# DAILY REPORT
# =====================================================================
def _pnl_totals():
    with _POS_LOCK:
        trades = list(_COMPLETED_TRADES)
    finals = [t for t in trades if t["outcome"] != "PARTIAL"]
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    net    = sum(t["pnl"] for t in trades)
    wr     = (len([t for t in finals if t["pnl"] > 0]) / len(finals) * 100) if finals else 0.0
    return {"trades": trades, "finals": finals, "wins": wins, "losses": losses,
            "net": net, "win_rate": wr}

def _unrealized(pos):
    "Floating P&L on an open position using last seen price."
    lp = pos.get("last_price", pos["entry"])
    return _apply_costs(pos["entry"], lp, pos["qty"], pos["side"])

def build_daily_report() -> str:
    "Full day's-PnL report: suggestions + trades + realized + floating P&L."
    tot = _pnl_totals()
    with _POS_LOCK:
        n_sig    = len(_SIGNALS_TODAY)
        open_pos = list(_OPEN_POSITIONS.values())
        opened   = _POSITIONS_OPENED
        sugg     = list(_SIGNALS_TODAY)
    halted, reason = halt_status()

    mode = "LIVE 💰" if AUTO_TRADE_ENABLED else "PAPER 🧪"
    date = now_ist().strftime("%d %b %Y")
    L = [f"📊 <b>DAILY TRADING REPORT</b> — {date}",
         "━━━━━━━━━━━━━━━━━━━━━",
         f"Mode: {mode}",
         f"Signals suggested: {n_sig}",
         f"Positions opened: {opened}"]
    if halted:
        L.append(f"⛔ Auto-halted: {reason}")

    if tot["trades"]:
        L.append("\n<b>── Trades ──</b>")
        for i, t in enumerate(tot["trades"], 1):
            L.append(f"{i}. {t['symbol']} [{t['strategy']}] {t['side']} · {t['outcome']}\n"
                     f"   ₹{t['entry']} → ₹{t['exit']} | Qty {t['qty']} | "
                     f"₹{t['pnl']:+,.2f} ({t['r']:+.2f}R)")
    else:
        L.append("\n<i>No completed trades.</i>")

    float_total = 0.0
    if open_pos:
        L.append("\n<b>── Still Open (floating) ──</b>")
        for i, p in enumerate(open_pos, 1):
            upnl = _unrealized(p); float_total += upnl
            L.append(f"{i}. {p['symbol']} [{p['strategy']}] {p['side']} @ ₹{round(p['entry'],2)} "
                     f"→ ₹{round(p.get('last_price', p['entry']),2)} | Qty {p['qty']} | "
                     f"uP&L ₹{upnl:+,.2f}")

    not_acted = [s for s in sugg if not s["acted"]]
    if not_acted:
        L.append(f"\n<b>── Other Suggestions ({len(not_acted)}) ──</b>")
        for s in not_acted[:15]:
            kro = f" | {s['kronos']}" if s.get("kronos") else ""
            L.append(f"• {s['symbol']} [{s['strategy']}] {s['signal']} "
                     f"₹{s['price']} | score {s['score']} | {s['time']}{kro}")

    best = max((t['pnl'] for t in tot["trades"]), default=0.0)
    worst = min((t['pnl'] for t in tot["trades"]), default=0.0)
    L += ["\n━━━━━━━━━━━━━━━━━━━━━",
          f"Completed: {len(tot['finals'])} | W:{len(tot['wins'])} L:{len(tot['losses'])} "
          f"| WR {tot['win_rate']:.1f}%",
          f"<b>Realised P&L: ₹{tot['net']:+,.2f}</b>",
          f"Best: ₹{best:+,.2f} | Worst: ₹{worst:+,.2f}"]
    if open_pos:
        L.append(f"Floating P&L: ₹{float_total:+,.2f} ({len(open_pos)} open)")
        L.append(f"<b>Total (real+float): ₹{tot['net'] + float_total:+,.2f}</b>")
    return "\n".join(L)

def get_daily_pnl_summary() -> str:
    "Backward-compatible alias used by app.py."
    return build_daily_report()

def send_daily_report():
    "Push the day's report to Telegram (used by EOD cron)."
    tg_send(build_daily_report())

# =====================================================================
# MAIN LOOP
# =====================================================================
def wait_until(target_t):
    while True:
        now = now_ist()
        if now.time() >= target_t:
            return
        w = (datetime.combine(now.date(), target_t, tzinfo=IST) - now).total_seconds()
        log.info(f"Waiting {int(w)}s until {target_t}"); time.sleep(min(max(w, 1), 60))

def scan_once(dhan, universe):
    signals = []
    log.info(f"Scan @ {now_ist().strftime('%H:%M:%S')} on {len(universe)} stocks")
    for i, row in enumerate(universe.itertuples(index=False)):
        if not market_open_now():
            break
        df = fetch_intraday(dhan, row.security_id)
        if df is not None:
            hits = detect_patterns(df)
            if hits:
                signals.extend(score_signals(row.symbol, row.security_id, df, hits))
        time.sleep(REQUEST_SLEEP_SEC)
        if (i + 1) % 25 == 0:
            log.info(f"  processed {i + 1}/{len(universe)}")
    if not signals:
        return pd.DataFrame()
    return (pd.DataFrame(signals).sort_values(["score", "vol_ratio"], ascending=[False, False])
            .drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True))

def act_on_signals(dhan, ranked):
    if ranked.empty:
        return
    ntrend = get_nifty_trend(dhan) if NIFTY_GATE_ENABLED else 0
    if NIFTY_GATE_ENABLED:
        log.info(f"NIFTY trend: {ntrend:+d}")
    for _, sig in ranked.head(TOP_N_RESULTS).iterrows():
        direction = 1 if sig["signal"] == "BUY" else -1
        if not passes_nifty_gate(direction, ntrend):
            continue
        sigd = sig.to_dict()
        strat = sigd.get("strategy", "?")

        # ---- Kronos confirmation / score adjust (v5) ----
        if _KRONOS_OK:
            allow, adj, kreason = kronos_gate.kronos_check(sigd["symbol"], direction)
            sigd["kronos"] = kreason
            if not allow:                      # strict-mode veto
                log.info(f"[{sigd['symbol']}] Kronos veto: {kreason}")
                continue
            sigd["score"] = int(sigd.get("score", 0)) + adj   # soft-mode boost/penalty

        # record every surfaced suggestion (for the day's report)
        record_suggestion(sigd)
        tg_send(f"📊 {sigd['symbol']} {sigd['signal']} [{strat}] {sigd['pattern']}\n"
                f"₹{sigd['price']} | Score {sigd['score']} | Vol×{sigd.get('vol_ratio','?')} | "
                f"{sigd.get('kronos','')} | {sigd.get('time','')}", silent=True)

        # only trade if score qualifies, in-window, and entry not blocked (v4)
        if sigd["score"] >= MIN_SCORE_TO_TRADE and in_session_for_entries():
            with _POS_LOCK:
                block = _entry_blocked(sigd["symbol"])
            if block:
                log.info(f"[{sigd['symbol']}] not entering: {block}")
                continue
            place_bracket_orders(dhan, sigd)

def run():
    try:
        from live_config import apply_live_config
        if apply_live_config(module_name="intraday_pattern_scanner_v2", tg_sender=tg_send):
            log.info("Live config applied.")
    except ImportError:
        log.debug("live_config not found.")
    except Exception as e:
        log.warning(f"live_config apply failed ({e})")

if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log.exception("Fatal error"); tg_send(f"🚨 Fatal error: {e}"); raise

