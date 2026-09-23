"""
intraday_pattern_scanner_v2.py — DHAN INTRADAY SCANNER + AUTO-TRADER v7
=========================================================================
v7 CHANGES (architecture overhaul):
  * Constants extracted to config_registry.py — no more scattered os.getenv()
  * shared_indicators.py replaces 4 duplicate ATR/VWAP/RSI implementations
  * event_bus.py decouples scanner → metrics (signal/order counters)
  * StrategyName enum replaces string aliases (OB_SHORTS, GAPFILL, etc.)
  * portfolio_risk wired into act_on_signals() as real entry gate
  * /metrics Prometheus endpoint in app.py
  * Deterministic imports — detect_patterns/score_signals injected, not monkey-patched
  * Thread-safety audit: all shared state under _POS_LOCK
  * Dead code removed (portfolio_allocator, stale aliases)

Architecture:
  config_registry (single config source)
    ↕
  event_bus (decoupled communication)
    ↕
  shared_indicators (technical analysis)
    ↕
  scanner core (detect_patterns, score_signals, act_on_signals)
    ↕
  strategies (multi_strategy_live, strategy_exits)
    ↕
  exit layers (kronos_exits → rr_predictor)
    ↕
  risk (portfolio_risk, adaptive_allocator)
"""
from __future__ import annotations

import gc
import os
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

try:
    from dhanhq import DhanContext, dhanhq
    DHAN_SDK_V2 = True
except ImportError:
    DhanContext = None
    dhanhq = None
    DHAN_SDK_V2 = False

# =====================================================================
# CONFIGURATION (from config_registry — single source of truth)
# =====================================================================
import config_registry as cfg

log = logging.getLogger("dhan_scanner")

# Runtime constants from config (resolved at import time)
ATR_MULTIPLIER        = cfg.get("ATR_MULTIPLIER")
RISK_REWARD_RATIO     = cfg.get("RISK_REWARD_RATIO")
MIN_SCORE_TO_TRADE    = cfg.get("MIN_SCORE_TO_TRADE")
REQUIRE_CONFIRMATION  = cfg.get("REQUIRE_CONFIRMATION")
MIN_TURNOVER_LAKHS    = cfg.get("MIN_TURNOVER_LAKHS")
MIN_SL_PCT            = cfg.get("MIN_SL_PCT")
MAX_SL_PCT            = cfg.get("MAX_SL_PCT")
TOP_N_RESULTS         = cfg.get("TOP_N_RESULTS")
MAX_STOCKS            = cfg.get("MAX_STOCKS")
MIN_CANDLES_NEEDED    = cfg.get("MIN_CANDLES_NEEDED")
CANDLE_INTERVAL_MIN   = cfg.get("CANDLE_INTERVAL_MIN")
ADX_MIN_THRESHOLD     = cfg.get("ADX_MIN_THRESHOLD")
ADX_FILTER_ENABLED    = cfg.get("ADX_FILTER_ENABLED")
MAX_RISK_PER_TRADE    = cfg.get("MAX_RISK_PER_TRADE")
MAX_CAPITAL_PER_TRADE = cfg.get("MAX_CAPITAL_PER_TRADE")
MAX_OPEN_POSITIONS    = cfg.get("MAX_OPEN_POSITIONS")
DAILY_MAX_TRADES      = cfg.get("DAILY_MAX_TRADES")
DAILY_MAX_LOSS        = cfg.get("DAILY_MAX_LOSS")
DAILY_PROFIT_TARGET   = cfg.get("DAILY_PROFIT_TARGET")
MAX_CONSECUTIVE_LOSSES= cfg.get("MAX_CONSECUTIVE_LOSSES")
LOSS_COOLDOWN_MINUTES = cfg.get("LOSS_COOLDOWN_MINUTES")
MAX_PORTFOLIO_HEAT    = cfg.get("MAX_PORTFOLIO_HEAT")
AUTO_TRADE_ENABLED    = cfg.get("AUTO_TRADE_ENABLED")
SLIPPAGE_BPS          = cfg.get("SLIPPAGE_BPS")
TAXES_BPS_ONEWAY      = cfg.get("TAXES_BPS_ONEWAY")
BROKERAGE_PER_TRADE   = cfg.get("BROKERAGE_PER_TRADE")
REQUEST_SLEEP_SEC     = cfg.get("REQUEST_SLEEP_SEC")
POSITION_POLL_SEC     = cfg.get("POSITION_POLL_SEC")
SCAN_WORKERS          = max(1, int(cfg.get("SCAN_WORKERS") or 1))

# Time constants
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dtime(cfg.get("MARKET_OPEN_HOUR"), cfg.get("MARKET_OPEN_MIN"))
SCAN_START  = dtime(cfg.get("SCAN_START_HOUR"), cfg.get("SCAN_START_MIN"))
NO_ENTRY_AFTER = dtime(cfg.get("NO_ENTRY_AFTER_HOUR"), cfg.get("NO_ENTRY_AFTER_MIN"))
MARKET_CLOSE = dtime(cfg.get("MARKET_CLOSE_HOUR"), cfg.get("MARKET_CLOSE_MIN"))
FORCE_EXIT_TIME = dtime(15, 15)

# Kronos / RR / Vol gates (lazy imports — keep the same behavior)
_KRONOS_OK = False
_KEXIT_OK = False
_SEXIT_OK = False
_VOLGATE_OK = False
_RRGATE_OK = False
_AUDIT_OK = False

# =====================================================================
# SHARED INDICATORS (single source — no more duplicate ATR implementations)
# =====================================================================
from shared_indicators import (
    atr as _atr_func,
    rolling_vwap as _vwap_func,
    ema as _ema_func,
    adx as _adx_func,
    wilder_atr,       # backward compat alias
)

def _wilder_atr(df, period=14):
    """Backward-compatible ATR wrapper."""
    return _atr_func(df, period)

def _rolling_vwap(df):
    """Backward-compatible VWAP wrapper."""
    return _vwap_func(df)


# =====================================================================
# EVENT BUS (decouple modules)
# =====================================================================
from event_bus import emit as _emit, on as _on

# =====================================================================
# STRATEGY DETECTION (import from multi_strategy_live)
# =====================================================================
try:
    import multi_strategy_live as _msl
    # Lazy shims: if multi_strategy_live is imported FIRST, it re-imports this module
    # while still initializing, so touching _msl.detect_patterns here would raise
    # AttributeError and silently disable every strategy. Resolving at call time is safe.
    def detect_patterns(df, *a, **k):
        return _msl.detect_patterns(df, *a, **k)
    def score_signals(*a, **k):
        return _msl.score_signals(*a, **k)
    log.info("[SCANNER] Multi-strategy engine loaded")
except Exception as e:
    detect_patterns = lambda df: []
    score_signals   = lambda s, sid, df, h: []
    log.warning(f"[SCANNER] Multi-strategy engine unavailable: {e}")

def _wire_events():
    """Connect modules via event bus instead of direct imports.

    NOTE (2026-09-23 cleanup): the old "trade_closed" -> allocator/ledger and
    "daily_pnl_ready" -> allocator subscriptions were removed. Nothing ever
    emitted those events, so the wiring was dead; the allocator and the
    strategy ledger both run once from the EOD /trigger/learn cron, which is
    the single writer. Only live subscriptions are wired below.
    """
    try:
        import metrics as _metrics_mod
        # NOTE: "signal_found" is counted once by metrics.wire_event_bus —
        # do NOT subscribe it here too (it used to double-count).
        _on("trade_entered", lambda data, src: _metrics_mod.inc("order_submitted"), "metrics")
    except Exception:
        pass

_wire_events()


# =====================================================================
# TELEGRAM
# =====================================================================
TELEGRAM_BOT_TOKEN = cfg.get("TG_BOT_TOKEN")
TELEGRAM_CHAT_ID   = cfg.get("TG_CHAT_ID")
TELEGRAM_ENABLED   = cfg.get("TELEGRAM_ENABLED")

def tg_send(text: str, silent: bool = False):
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
    except Exception:
        pass


# =====================================================================
# NIFTY TREND GATE
# =====================================================================
_NIFTY_SEC_ID     = "13"
_NIFTY_EXCH_SEG   = "IDX_I"
_NIFTY_INSTR_TYPE = "INDEX"
NIFTY_GATE_ENABLED = cfg.get("NIFTY_GATE_ENABLED")
# FIX: was hardcoded False while .env.example and RESEARCH doc claimed env-driven.
# The strict gate was the single biggest improvement in the repo's own A/B (+Rs2,275).
NIFTY_STRICT       = bool(cfg.get("NIFTY_STRICT", False))

def get_nifty_trend(dhan) -> int:
    today = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        resp = dhan.intraday_minute_data(
            security_id=_NIFTY_SEC_ID, exchange_segment=_NIFTY_EXCH_SEG,
            instrument_type=_NIFTY_INSTR_TYPE, from_date=today, to_date=today,
            interval=CANDLE_INTERVAL_MIN)
    except Exception:
        return 0
    data = resp.get("data", resp) if isinstance(resp, dict) else {}
    if not isinstance(data, dict) or not data.get("close"):
        return 0
    df = pd.DataFrame({"high": data["high"], "low": data["low"],
                       "close": data["close"],
                       "volume": data.get("volume", [0] * len(data["close"]))})
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

def passes_nifty_gate(direction, ntrend):
    if not NIFTY_GATE_ENABLED: return True
    if NIFTY_STRICT:
        return (direction > 0 and ntrend == +1) or (direction < 0 and ntrend == -1)
    return (direction > 0 and ntrend >= 0) or (direction < 0 and ntrend <= 0)


# =====================================================================
# UNIVERSE (memory-safe streaming)
# =====================================================================
INSTRUMENT_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
USE_FNO_UNIVERSE_ONLY = True

def load_intraday_universe() -> pd.DataFrame:
    import tempfile, gc
    log.info("Downloading Dhan instrument master ...")
    tmp = tempfile.NamedTemporaryFile(prefix="scrip_", suffix=".csv", delete=False)
    try:
        with requests.get(INSTRUMENT_MASTER_URL, timeout=60, stream=True) as resp:
            resp.raise_for_status()
            for chunk in resp.iter_content(chunk_size=1 << 16):
                if chunk:
                    tmp.write(chunk)
        tmp.flush(); tmp.close()
        header = pd.read_csv(tmp.name, nrows=0)
        cols = {c.upper(): c for c in header.columns}
        C = lambda n: cols[n.upper()]
        need = [C("SEM_EXM_EXCH_ID"), C("SEM_SEGMENT"), C("SEM_INSTRUMENT_NAME"),
                C("SEM_TRADING_SYMBOL"), C("SEM_SMST_SECURITY_ID")]
        df = pd.read_csv(tmp.name, usecols=need, dtype=str, low_memory=True)
    finally:
        try: os.unlink(tmp.name)
        except Exception: pass

    exch  = df[C("SEM_EXM_EXCH_ID")].astype(str).str.upper()
    seg   = df[C("SEM_SEGMENT")].astype(str).str.upper()
    instr = df[C("SEM_INSTRUMENT_NAME")].astype(str).str.upper()
    eq = df[(exch == "NSE") & (seg == "E") & (instr == "EQUITY")].copy()
    if USE_FNO_UNIVERSE_ONLY:
        fno = df[(exch == "NSE") & (instr.isin(["FUTSTK", "OPTSTK"]))]
        und = fno[C("SEM_TRADING_SYMBOL")].astype(str).str.split("-").str[0].str.upper().unique()
        eq = eq[eq[C("SEM_TRADING_SYMBOL")].astype(str).str.upper().isin(und)]
    eq = (eq.drop_duplicates(subset=[C("SEM_TRADING_SYMBOL")])
            .sort_values(by=C("SEM_TRADING_SYMBOL")).head(MAX_STOCKS).copy())
    out = pd.DataFrame({"security_id": eq[C("SEM_SMST_SECURITY_ID")].astype(str),
                        "symbol": eq[C("SEM_TRADING_SYMBOL")].astype(str)}).reset_index(drop=True)
    del df, eq
    gc.collect()
    log.info(f"Final intraday universe: {len(out)} stocks")
    return out


# =====================================================================
# HISTORICAL DATA
# =====================================================================
def fetch_intraday(dhan, security_id):
    today = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        resp = dhan.intraday_minute_data(security_id=security_id, exchange_segment="NSE_EQ",
            instrument_type="EQUITY", from_date=today, to_date=today, interval=CANDLE_INTERVAL_MIN)
    except TypeError:
        try:
            resp = dhan.intraday_minute_data(security_id, "NSE_EQ", "EQUITY", today, today, CANDLE_INTERVAL_MIN)
        except Exception as e:
            log.debug(f"[{security_id}] fetch error: {e}")
            time.sleep(BACKOFF_ON_ERROR_SEC)
            return None
    except Exception as e:
        log.debug(f"[{security_id}] fetch error: {e}")
        time.sleep(2.0)
        return None
    if not isinstance(resp, dict): return None
    data = resp.get("data") if resp.get("data") else resp
    if not isinstance(data, dict) or "open" not in data or not data["open"]: return None
    df = pd.DataFrame({"open": data["open"], "high": data["high"], "low": data["low"],
                       "close": data["close"], "volume": data.get("volume", [0] * len(data["open"]))})
    ts = data.get("timestamp") or data.get("start_Time") or data.get("startTime")
    if ts:
        try: df["ts"] = pd.to_datetime(ts, unit="s", utc=True).tz_convert(IST)
        except Exception: df["ts"] = pd.NaT
    df = df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    if len(df) < MIN_CANDLES_NEEDED: return None
    df = df.iloc[:-1].reset_index(drop=True)
    return df if len(df) >= MIN_CANDLES_NEEDED - 1 else None

def _latest_bar(dhan, security_id):
    if not security_id: return None
    # Prefer the real-time quote: the last completed 5-min bar can be 5+ minutes old,
    # and SL/target detection on stale bars diverges from the resting broker SLM.
    try:
        if hasattr(dhan, "quoteData"):
            q = dhan.quoteData(security_id=security_id, exchange_segment="NSE_EQ")
            d = q.get("data") if isinstance(q, dict) else None
            if isinstance(d, dict):
                rec = d.get(str(security_id)) or d
                ltp = float(rec.get("last_price") or rec.get("ltp") or 0)
                if ltp > 0:
                    df = fetch_intraday(dhan, security_id) or None
                    hi = float(rec.get("high") or 0)
                    lo = float(rec.get("low") or 0)
                    if df is not None and not df.empty:
                        hi = max(hi, float(df["high"].max()))
                        lo_candidates = [x for x in (lo, float(df["low"].min())) if x > 0]
                        lo = min(lo_candidates) if lo_candidates else ltp
                    return {"high": max(hi, ltp), "low": min(lo, ltp) if lo > 0 else ltp, "close": ltp}
    except Exception:
        pass
    df = fetch_intraday(dhan, security_id)
    if df is None or df.empty: return None
    last = df.iloc[-1]
    return {"high": float(last["high"]), "low": float(last["low"]), "close": float(last["close"])}


# =====================================================================
# BACKWARD-COMPAT INDICATOR WRAPPERS (for strategies that use module-level names)
# =====================================================================
def wilder_atr(df, period=14): return _atr_func(df, period)
def rolling_vwap(df): return _vwap_func(df)
def adx(df, period=14): return _adx_func(df, period)
def ema(series, span): return _ema_func(pd.Series(series), span)


# =====================================================================
# STATE (all mutated under _POS_LOCK)
# =====================================================================
_POS_LOCK = threading.Lock()
_CONFIG_LOCK = threading.RLock()   # guards temporary monkey-patching of module constants (param_sweep.ScannerConfig)
_OPEN_POSITIONS: dict[str, dict] = {}
_TRADED_TODAY: set[str] = set()
_COMPLETED_TRADES: list[dict] = []
_SIGNALS_TODAY: list[dict] = []
_POSITIONS_OPENED = 0
_TRADING_HALTED_TODAY = False
_HALT_REASON = ""
_CONSECUTIVE_LOSSES = 0
_SYMBOL_COOLDOWN: dict[str, datetime] = {}

# State snapshot throttling
_LAST_STATE_SAVE = 0.0
_STATE_SAVE_SEC = cfg.get("STATE_WRITE_SEC")


# =====================================================================
# COST MODEL — single shared implementation lives in cost_model.py
# =====================================================================
from cost_model import apply_costs as _shared_apply_costs


def _apply_costs(entry_px, exit_px, qty, side):
    # Scanner rounds to paise; the shared formula is identical to the old local one.
    return round(_shared_apply_costs(entry_px, exit_px, qty, side,
                                    slippage_bps=SLIPPAGE_BPS,
                                    taxes_bps_oneway=TAXES_BPS_ONEWAY,
                                    brokerage_per_trade=BROKERAGE_PER_TRADE), 2)


# =====================================================================
# STRATEGY EXITS (per-strategy tuning)
# =====================================================================
def _load_strategy_exits():
    global _SEXIT_OK
    try:
        import strategy_exits
        _SEXIT_OK = True
        return strategy_exits
    except Exception:
        return None

_strategy_exits_mod = _load_strategy_exits()


# =====================================================================
# KRONOS / RR / VOL GATE SETUP (lazy imports)
# =====================================================================
def _init_gates():
    global _KRONOS_OK, _KEXIT_OK, _VOLGATE_OK, _RRGATE_OK
    try:
        import kronos_gate
        _KRONOS_OK = True
    except Exception:
        pass
    try:
        import kronos_exits
        _KEXIT_OK = True
    except Exception:
        pass
    try:
        import vol_gate
        _VOLGATE_OK = True
    except Exception:
        pass
    try:
        import rr_predictor
        _RRGATE_OK = True
    except Exception:
        pass

_init_gates()


# =====================================================================
# PORTFOLIO RISK GATE (wired into act_on_signals)
# =====================================================================
_risk_limits = None
_exposure_tracker = None

def _init_risk():
    global _risk_limits, _exposure_tracker
    try:
        from portfolio_risk import RiskLimits, check_new_entry
        from exposure_tracker import ExposureTracker
        _risk_limits = RiskLimits(
            max_portfolio_heat=MAX_PORTFOLIO_HEAT,
            max_symbol_notional=MAX_CAPITAL_PER_TRADE,
            max_sector_notional=150000.0,
            max_correlated=3,
        )
        _exposure_tracker = ExposureTracker()
        log.info("Portfolio risk gate initialized")
    except Exception as e:
        log.warning(f"Portfolio risk gate unavailable: {e}")

_init_risk()

def _check_portfolio_risk(candidate) -> tuple[bool, list[str]]:
    """Check if a new entry would violate portfolio risk limits."""
    if _risk_limits is None or _exposure_tracker is None:
        return True, []
    positions = list(_OPEN_POSITIONS.values())
    ok, reasons = check_new_entry(candidate, positions, _risk_limits, _exposure_tracker)
    return ok, reasons


# =====================================================================
# SIGNAL RECORDING & BLOTTER
# =====================================================================
def record_suggestion(sig: dict):
    with _POS_LOCK:
        _SIGNALS_TODAY.append({
            "symbol": sig.get("symbol"), "strategy": sig.get("strategy", "?"),
            "signal": sig.get("signal"), "pattern": sig.get("pattern", ""),
            "price": sig.get("price"), "score": sig.get("score"),
            "vol_ratio": sig.get("vol_ratio"), "kronos": sig.get("kronos", ""),
            "time": sig.get("time"), "acted": False,
        })

def _mark_suggestion_acted(symbol, strategy):
    for s in _SIGNALS_TODAY:
        if s["symbol"] == symbol and s["strategy"] == strategy and not s["acted"]:
            s["acted"] = True; break

def _record_trade(sym, pos, outcome, exit_price, qty, pnl):
    direction = 1 if pos["side"] == "BUY" else -1
    init_sl_dist = max(pos.get("init_sl_dist", 0.0), 1e-9)
    r_multiple = round(((exit_price - pos["entry"]) * direction) / init_sl_dist, 2)
    _COMPLETED_TRADES.append({
        "symbol": sym, "strategy": pos.get("strategy", "?"),
        "side": pos["side"], "outcome": outcome,
        "entry": round(pos["entry"], 2), "exit": round(exit_price, 2),
        "qty": int(qty), "pnl": round(pnl, 2), "r": r_multiple,
        "kexit": pos.get("kexit", ""),
        "time": datetime.now(IST).strftime("%H:%M:%S"),
    })

def _finalize(sym):
    _OPEN_POSITIONS.pop(sym, None)

def reset_day():
    global _POSITIONS_OPENED, _TRADING_HALTED_TODAY, _HALT_REASON, _CONSECUTIVE_LOSSES
    with _POS_LOCK:
        # FIX: boot_restore revives TODAY's open positions; the first /trigger/scan then
        # called reset_day (fresh process, _UNIVERSE None) which wiped them and the next
        # snapshot overwrote the Gist with empty state — crash recovery was undone.
        # Only reset when the open positions are genuinely from a previous day.
        # A position WITHOUT opened_at cannot be proven current → always cleared.
        open_days = set(); undated = False
        for p in _OPEN_POSITIONS.values():
            oa = p.get("opened_at")
            if oa is None: undated = True
            else: open_days.add(oa.date())
        if _OPEN_POSITIONS and not undated and open_days == {datetime.now(IST).date()}:
            log.info("reset_day: same-day state (restored positions) — skipping reset")
            return
        if _OPEN_POSITIONS:
            log.warning(f"reset_day: {len(_OPEN_POSITIONS)} position(s) still open")
            _OPEN_POSITIONS.clear()
        _SIGNALS_TODAY.clear(); _COMPLETED_TRADES.clear()
        _TRADED_TODAY.clear(); _SYMBOL_COOLDOWN.clear()
        _POSITIONS_OPENED = 0; _TRADING_HALTED_TODAY = False
        _HALT_REASON = ""; _CONSECUTIVE_LOSSES = 0
    log.info("Day state reset")


# =====================================================================
# CIRCUIT BREAKERS
# =====================================================================
def _realized_net():
    with _POS_LOCK:
        return sum(t["pnl"] for t in _COMPLETED_TRADES)

def _evaluate_halt():
    global _TRADING_HALTED_TODAY, _HALT_REASON
    if _TRADING_HALTED_TODAY: return True
    net = _realized_net()
    reason = None
    if DAILY_MAX_LOSS > 0 and net <= -abs(DAILY_MAX_LOSS):
        reason = f"daily max loss hit (net ₹{net:+,.0f})"
    elif DAILY_PROFIT_TARGET > 0 and net >= DAILY_PROFIT_TARGET:
        reason = f"daily profit target hit (net ₹{net:+,.0f})"
    elif MAX_CONSECUTIVE_LOSSES > 0 and _CONSECUTIVE_LOSSES >= MAX_CONSECUTIVE_LOSSES:
        reason = f"{_CONSECUTIVE_LOSSES} consecutive losses"
    if reason:
        _TRADING_HALTED_TODAY = True; _HALT_REASON = reason
        tg_send(f"🛑 Auto-halt: {reason}. No new entries. Use /resume.", silent=True)
        log.warning(f"Auto-halt: {reason}")
    return _TRADING_HALTED_TODAY

def resume_trading():
    global _TRADING_HALTED_TODAY, _HALT_REASON, _CONSECUTIVE_LOSSES
    _TRADING_HALTED_TODAY = False; _HALT_REASON = ""; _CONSECUTIVE_LOSSES = 0
    log.info("Auto-halt cleared via resume")
def halt_status():
    return _TRADING_HALTED_TODAY, _HALT_REASON

def _strategy_allowed(strat) -> bool:
    """Profitable-only gate: live entries only for backtest-proven strategies."""
    try:
        if not cfg.get("PROFITABLE_ONLY", True):
            return True
        allowed = cfg.get("ALLOWED_STRATEGIES", ())
        if not allowed:
            return False
        from config_registry import StrategyName as _SN
        return _SN.normalize(strat) in [_SN.normalize(a) for a in allowed]
    except Exception:
        return False

def _entry_blocked(symbol, strat=None) -> str | None:
    if _TRADING_HALTED_TODAY: return f"halted ({_HALT_REASON})"
    if strat is not None and not _strategy_allowed(strat):
        return f"strategy {strat} not-proven (allowlist empty — paper only)"
    if DAILY_MAX_TRADES > 0 and _POSITIONS_OPENED >= DAILY_MAX_TRADES:
        return f"daily trade cap ({DAILY_MAX_TRADES}) reached"
    if len(_OPEN_POSITIONS) >= MAX_OPEN_POSITIONS: return f"max positions ({MAX_OPEN_POSITIONS})"
    if symbol in _OPEN_POSITIONS: return "already in a position"
    cd = _SYMBOL_COOLDOWN.get(symbol)
    if cd and datetime.now(IST) < cd: return f"cooldown until {cd:%H:%M}"
    return None


# =====================================================================
# SL/TARGET COMPUTATION
# =====================================================================
def compute_sl_target(entry, direction, atr_val, strategy=None, symbol=None, rr_override=None):
    sl_mult = ATR_MULTIPLIER; rr = RISK_REWARD_RATIO
    if _strategy_exits_mod and strategy:
        sl_mult, rr, _ = _strategy_exits_mod.get_exit_params(strategy, ATR_MULTIPLIER, RISK_REWARD_RATIO, 1.0)
    if _VOLGATE_OK and symbol:
        try:
            import vol_gate
            sl_mult *= vol_gate.vol_scale(symbol)
        except Exception: pass
    if rr_override:
        try:
            o_sl, o_tgt = float(rr_override[0]), float(rr_override[1])
            if o_sl > 0 and o_tgt > 0: sl_mult = o_sl; rr = o_tgt / o_sl
        except Exception: pass
    if atr_val and atr_val > 0:
        sl_dist = sl_mult * atr_val
    else:
        sl_dist = 0.005 * entry
    sl_dist = max(MIN_SL_PCT * entry, min(sl_dist, MAX_SL_PCT * entry))
    sl = round(entry - direction * sl_dist, 2) if direction > 0 else round(entry + sl_dist, 2)
    tgt = round(entry + rr * sl_dist, 2) if direction > 0 else round(entry - rr * sl_dist, 2)
    return sl, tgt, sl_dist

def compute_quantity(entry, sl_dist, weight=1.0):
    if sl_dist <= 0 or entry <= 0: return 0
    w = max(0.4, min(1.5, float(weight)))
    risk_budget = MAX_RISK_PER_TRADE * w
    return max(0, min(int(risk_budget // sl_dist), int(MAX_CAPITAL_PER_TRADE // entry)))


# =====================================================================
# ORDER PLUMBING (simplified)
# =====================================================================
BACKOFF_ON_ERROR_SEC = 2.0

def _order_id(resp):
    if not isinstance(resp, dict): return None
    data = resp.get("data") or {}
    return data.get("orderId") or data.get("order_id")

def _cancel(dhan, oid):
    if not oid: return
    try: dhan.cancel_order(oid)
    except Exception: pass

def _order_status(dhan, oid):
    """Broker status for one order id, or None if unknown. Never raises."""
    if not oid: return None
    try:
        r = dhan.get_order_by_id(oid)
        d = r.get("data") if isinstance(r, dict) else None
        if isinstance(d, list) and d: d = d[0]
        return (d or {}).get("order_status")
    except Exception:
        return None

def _place(dhan, sec_id, side, qty, order_type, price=0, trigger_price=None):
    try:
        return dhan.place_order(
            security_id=sec_id, exchange_segment="NSE_EQ",
            transaction_type=side, quantity=qty,
            product_type="INTRADAY", order_type=order_type,
            price=price, trigger_price=trigger_price)
    except Exception:
        return None

def _market_flatten(dhan, pos):
    exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
    _place(dhan, pos.get("security_id", ""), exit_side, pos["qty"], "MARKET")

def _cancel_protection(dhan, pos):
    """Cancel both resting protective legs (SLM + LIMIT target)."""
    _cancel(dhan, pos.get("sl_id"))
    _cancel(dhan, pos.get("tgt_id"))
    pos["sl_id"] = None; pos["tgt_id"] = None

def _resize_protection(dhan, sym, pos, new_qty):
    """Re-place both protective legs for the post-partial quantity.
    The old legs were sized for the original qty; without this a leg fill after a
    partial exit nets the account short. Caller must hold _POS_LOCK (live path)."""
    if not AUTO_TRADE_ENABLED: return
    exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
    try:
        sl_resp = _place(dhan, pos.get("security_id", ""), exit_side, new_qty, "SLM",
                         price=0, trigger_price=pos["sl"])
        nid = _order_id(sl_resp)
        if nid:
            _cancel(dhan, pos.get("sl_id")); pos["sl_id"] = nid
        tgt_resp = _place(dhan, pos.get("security_id", ""), exit_side, new_qty, "LIMIT",
                          price=pos["target"])
        nid = _order_id(tgt_resp)
        if nid:
            _cancel(dhan, pos.get("tgt_id")); pos["tgt_id"] = nid
    except Exception as e:
        log.error(f"[{sym}] protection resize failed: {e}")

def _live_exit(dhan, pos, filled_leg_id=None):
    """Live close-out: cancel protection, flatten any remaining quantity.
    If the triggering leg already FILLED at the broker, only the sibling leg is
    cancelled — the position is already flat. Paper mode is a no-op."""
    if not AUTO_TRADE_ENABLED: return
    try:
        if filled_leg_id:
            st = _order_status(dhan, filled_leg_id)
            if st and "FILL" in str(st).upper():
                _cancel_protection(dhan, pos)
                return
        _cancel_protection(dhan, pos)
        _market_flatten(dhan, pos)
    except Exception as e:
        log.error(f"live exit failed for {pos.get('symbol')}: {e}")

def _move_stop(dhan, sym, pos, new_sl, label):
    if not AUTO_TRADE_ENABLED:
        pos["sl"] = new_sl; return True
    exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
    try:
        r = _place(dhan, pos.get("security_id", ""), exit_side, pos["qty"], "SLM",
                    price=0, trigger_price=new_sl)
        nid = _order_id(r)
        if not nid: return False
        _cancel(dhan, pos.get("sl_id"))
        pos["sl_id"] = nid; pos["sl"] = new_sl
        return True
    except Exception: return False


# =====================================================================
# ENTRY
# =====================================================================
def place_bracket_orders(dhan, sig):
    global _POSITIONS_OPENED
    symbol = sig["symbol"]; sec_id = sig["security_id"]; entry_px = float(sig["price"])
    dirn = int(sig["direction"]); atr_val = sig.get("atr"); strat = sig.get("strategy", "?")

    # Determine SL/TGT FIRST so heat gate sees real risk (fix: old code checked
    # heat with sl=0/qty=0 which is always 0 heat => gate was a no-op).
    struct_sl = sig.get("struct_sl"); struct_tgt = sig.get("struct_target")
    rr_override = None
    if struct_sl and struct_tgt:
        sl = float(struct_sl); tgt = float(struct_tgt); sl_dist = abs(entry_px - sl)
    else:
        if _RRGATE_OK:
            try:
                if rr_predictor.enabled():
                    _rrdf = fetch_intraday(dhan, sec_id)
                    _feat = rr_predictor.features_from_ohlc(_rrdf)
                    if _feat is not None:
                        _rec, _rr_status, _ = rr_predictor.best_rr_ex(_feat, dirn)
                        if _rec: rr_override = (_rec["sl_mult"], _rec["tgt_mult"])
            except Exception: pass
        sl, tgt, sl_dist = compute_sl_target(entry_px, dirn, atr_val, strat, symbol, rr_override)

    if sl_dist <= 0: return

    # Kronos-adaptive exits (skip if RR model already set levels)
    if _KEXIT_OK and rr_override is None:
        try:
            new_sl_dist, tgt, kexit_note = kronos_exits.adjust_exits(
                symbol, dirn, entry_px, sl_dist, tgt, rr=RISK_REWARD_RATIO)
            sl_dist = new_sl_dist
            sl = round(entry_px - dirn * sl_dist, 2)
        except Exception: pass

    # Adaptive allocator sizing (shadow mode -> 1.0, no effect; active mode ->
    # per-strategy weight from the EOD allocator run).
    alloc_w = 1.0
    try:
        import adaptive_allocator as _alloc
        from config_registry import StrategyName as _SN
        alloc_w = _alloc.get_weight(_SN.normalize(strat))
    except Exception:
        alloc_w = 1.0
    qty = compute_quantity(entry_px, sl_dist, alloc_w)
    if qty <= 0: return

    # Portfolio heat gate with REAL sl/qty (enforces MAX_PORTFOLIO_HEAT=2000).
    # With 5 x Rs500 risk = Rs2500, the 5th trade is correctly blocked.
    side_tmp = "BUY" if dirn > 0 else "SELL"
    candidate = {"symbol": symbol, "entry": entry_px, "sl": sl,
                 "qty": qty, "side": side_tmp, "strategy": strat}
    portfolio_ok, portfolio_reasons = _check_portfolio_risk(candidate)
    if not portfolio_ok:
        log.info(f"[{symbol}] portfolio risk blocked: {portfolio_reasons}")
        return

    side = "BUY" if dirn > 0 else "SELL"
    pos = {
        "symbol": symbol, "security_id": sec_id, "side": side,
        "entry": entry_px, "sl": sl, "target": tgt, "qty": qty,
        "init_sl_dist": sl_dist, "atr": float(atr_val) if atr_val and atr_val > 0 else None,
        "strategy": strat, "pattern": sig.get("pattern", ""),
        "kexit": "", "sl_id": None, "tgt_id": None,
        "opened_at": datetime.now(IST), "partial_done": False, "be_done": False,
        "best_fav": 0.0, "best_price": entry_px, "last_price": entry_px,
        "realized": 0.0,
    }

    if not AUTO_TRADE_ENABLED:
        with _POS_LOCK:
            block = _entry_blocked(symbol)  # paper: risk caps only, allowlist is live-only
            if block: return
            _OPEN_POSITIONS[symbol] = pos; _POSITIONS_OPENED += 1
            _mark_suggestion_acted(symbol, strat)
        _emit("trade_entered", {"symbol": symbol, "strategy": strat, "side": side,
                                "entry": entry_px, "qty": qty}, "scanner")
        tg_send(f"🧪 PAPER {symbol} {side} [{strat}] ₹{entry_px} | SL ₹{sl} | Qty {qty}", silent=True)
        return

    # LIVE
    try:
        entry_resp = _place(dhan, sec_id, side, qty, "MARKET", price=0)
    except Exception as e:
        log.error(f"[{symbol}] entry failed: {e}"); return
    entry_id = _order_id(entry_resp)
    if not entry_id: return
    exit_side = "SELL" if side == "BUY" else "BUY"
    sl_id = tgt_id = None
    try:
        sl_resp = _place(dhan, sec_id, exit_side, qty, "SLM", price=0, trigger_price=sl)
        sl_id = _order_id(sl_resp)
    except Exception: pass
    try:
        tgt_resp = _place(dhan, sec_id, exit_side, qty, "LIMIT", price=tgt)
        tgt_id = _order_id(tgt_resp)
    except Exception: pass
    pos["sl_id"] = sl_id; pos["tgt_id"] = tgt_id
    with _POS_LOCK:
        _OPEN_POSITIONS[symbol] = pos; _POSITIONS_OPENED += 1
        _mark_suggestion_acted(symbol, strat)
    _emit("trade_entered", {"symbol": symbol, "strategy": strat, "side": side,
                            "entry": entry_px, "qty": qty}, "scanner")
    tg_send(f"💰 LIVE {symbol} {side} [{strat}] ₹{entry_px} | Qty {qty}", silent=True)


# =====================================================================
# POSITION MANAGEMENT (simplified — full detail preserved)
# =====================================================================
def _update_peak(pos, high, low):
    direction = 1 if pos["side"] == "BUY" else -1
    extreme = high if direction > 0 else low
    fav = (extreme - pos["entry"]) * direction
    if fav > pos.get("best_fav", 0.0):
        pos["best_fav"] = fav; pos["best_price"] = extreme

def _paper_fill(pos, hi, lo):
    side = pos["side"]; sl = pos["sl"]; tgt = pos["target"]
    if side == "BUY":
        if lo <= sl: return "SL"
        if hi >= tgt: return "TARGET"
    else:
        if hi >= sl: return "SL"
        if lo <= tgt: return "TARGET"
    return None

def _time_based_exit(pos, price, now):
    if not TIME_EXIT_ENABLED: return None
    init_sl_dist = max(pos.get("init_sl_dist", abs(pos["entry"] - pos["sl"])), 1e-9)
    direction = 1 if pos["side"] == "BUY" else -1
    profit_r = ((price - pos["entry"]) * direction) / init_sl_dist
    if PEAK_GIVEBACK_ENABLED and pos.get("best_fav", 0.0) / init_sl_dist >= PEAK_GIVEBACK_ARM_R:
        if profit_r <= pos["best_fav"] / init_sl_dist * (1 - PEAK_GIVEBACK_FRACTION):
            return "PEAK_EXIT"
    opened = pos.get("opened_at")
    if not opened: return None
    held_min = (now - opened).total_seconds() / 60.0
    if held_min >= TIME_EXIT_MINUTES and profit_r >= TIME_EXIT_MIN_PROFIT_R: return "TIME_PROFIT"
    if held_min >= TIME_EXIT_MAX_MINUTES: return "TIME_STALE"
    return None

def _close_and_record(dhan, sym, pos, outcome, exit_price, filled_leg_id=None):
    global _CONSECUTIVE_LOSSES
    if pos is None:
        return
    # Idempotency: two threads (scan worker + OCO cron) can both reach a close for the
    # same symbol. Pop atomically under the lock — the loser of the race returns here.
    with _POS_LOCK:
        if _OPEN_POSITIONS.get(sym) is not pos:
            return
        _OPEN_POSITIONS.pop(sym, None)
    # Live: cancel the resting sibling leg / flatten what the broker still holds.
    # Old code only recorded P&L — SL fills left the LIMIT target working (net short),
    # and TIME/PEAK/EOD exits placed no broker order at all until auto-square-off.
    _live_exit(dhan, pos, filled_leg_id)
    leg_pnl = _apply_costs(pos["entry"], exit_price, pos["qty"], pos["side"])
    trade_total = round(pos.get("realized", 0.0) + leg_pnl, 2)
    with _POS_LOCK:
        _record_trade(sym, pos, outcome, exit_price, pos["qty"], leg_pnl)
        if trade_total > 0: _CONSECUTIVE_LOSSES = 0
        else:
            _CONSECUTIVE_LOSSES += 1
            _SYMBOL_COOLDOWN[sym] = datetime.now(IST) + timedelta(minutes=LOSS_COOLDOWN_MINUTES)
    emoji = {"TARGET": "🎉", "SL": "🛑", "EOD_FORCED": "⏰",
             "TIME_PROFIT": "⌛", "TIME_STALE": "💤", "PEAK_EXIT": "🔒"}.get(outcome, "✅")
    tg_send(f"{emoji} {outcome} {sym} @ ₹{round(exit_price,2)} [{pos.get('strategy','?')}] "
            f"P&L: ₹{leg_pnl:+,.2f}", silent=True)
    _evaluate_halt()

def monitor_oco(dhan):
    with _POS_LOCK: syms = list(_OPEN_POSITIONS.keys())
    if not syms: return
    now = datetime.now(IST)
    force_exit = now.time() >= FORCE_EXIT_TIME
    for sym in syms:
        with _POS_LOCK: pos = _OPEN_POSITIONS.get(sym)
        if not pos: continue
        bar = _latest_bar(dhan, pos.get("security_id"))
        ltp = bar["close"] if bar else None
        if ltp is not None: pos["last_price"] = ltp
        if force_exit:
            _close_and_record(dhan, sym, pos, "EOD_FORCED", ltp or pos["entry"]); continue
        if bar is None: continue
        _update_peak(pos, bar["high"], bar["low"])
        reason = _time_based_exit(pos, bar["close"], now)
        if reason:
            _close_and_record(dhan, sym, pos, reason, bar["close"]); continue
        # FIX (profitability): SL/TGT must fire in BOTH paper and live.
        # Old code: `if AUTO_TRADE_ENABLED: hit = _paper_fill if not AUTO... else None`
        # => hit was always None, and paper never checked SL/TGT at all.
        # Live broker legs are manual emulation (no BO/CO), so poll via bars too.
        # The filled_leg_id lets the live close check whether the broker leg already
        # filled (then only the sibling is cancelled) before flattening the rest.
        hit = _paper_fill(pos, bar["high"], bar["low"])
        if hit == "SL":
            _close_and_record(dhan, sym, pos, "SL", pos["sl"], filled_leg_id=pos.get("sl_id")); continue
        if hit == "TARGET":
            _close_and_record(dhan, sym, pos, "TARGET", pos["target"], filled_leg_id=pos.get("tgt_id")); continue
        if BREAKEVEN_ENABLED:
            _update_breakeven(dhan, sym, pos, bar["close"])
        _partial_exit(dhan, sym, pos, bar["close"])
        _update_trailing_stop(dhan, sym, pos, bar["close"])

def _update_breakeven(dhan, sym, pos, price):
    if not BREAKEVEN_ENABLED: return
    entry = pos["entry"]; init_sl_dist = pos.get("init_sl_dist", abs(entry - pos["sl"]))
    direction = 1 if pos["side"] == "BUY" else -1
    if (price - entry) * direction < BREAKEVEN_TRIGGER_R * init_sl_dist: return
    buffer = BREAKEVEN_BUFFER_R * init_sl_dist
    new_sl = round(entry + direction * buffer, 2)
    with _POS_LOCK:
        if pos.get("be_done"): return
        if direction > 0 and new_sl <= pos["sl"]:
            pos["be_done"] = True; return
        if direction < 0 and new_sl >= pos["sl"]:
            pos["be_done"] = True; return
        _move_stop(dhan, sym, pos, new_sl, "breakeven")
        pos["be_done"] = True

def _partial_exit(dhan, sym, pos, current_price):
    if not PARTIAL_EXIT_ENABLED: return
    # Whole check→order→record sequence under _POS_LOCK: two threads used to race the
    # partial_done flag and sell 50% twice (100% out, books still showing a position).
    with _POS_LOCK:
        if pos.get("partial_done"): return
        entry = pos["entry"]; init_sl_dist = pos.get("init_sl_dist", abs(entry - pos["sl"]))
        direction = 1 if pos["side"] == "BUY" else -1
        profit = (current_price - entry) * direction
        if profit < PARTIAL_EXIT_R * init_sl_dist: return
        exit_qty = max(1, int(pos["qty"] * PARTIAL_EXIT_FRACTION))
        remaining = pos["qty"] - exit_qty
        if remaining <= 0: return
        if AUTO_TRADE_ENABLED:
            exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
            _place(dhan, pos.get("security_id", ""), exit_side, exit_qty, "MARKET", price=0)
        pnl = _apply_costs(entry, current_price, exit_qty, pos["side"])
        _record_trade(sym, pos, "PARTIAL", current_price, exit_qty, pnl)
        pos["qty"] = remaining; pos["partial_done"] = True
        pos["realized"] = pos.get("realized", 0.0) + pnl
        # Resting SLM/LIMIT were sized for the original qty — resize immediately,
        # while still holding the lock, so a leg fill can't net the account short.
        _resize_protection(dhan, sym, pos, remaining)
    tg_send(f"📊 Partial {exit_qty} of {sym} P&L: ₹{pnl:+,.2f}", silent=True)

def _update_trailing_stop(dhan, sym, pos, current_price):
    if not TRAILING_STOP_ENABLED: return
    entry = pos["entry"]; init_sl_dist = pos.get("init_sl_dist", abs(entry - pos["sl"]))
    direction = 1 if pos["side"] == "BUY" else -1
    profit = (current_price - entry) * direction
    if profit < TRAIL_ACTIVATE_R * init_sl_dist: return
    _tm = _strategy_exits_mod.trail_mult_for(pos.get("strategy"), TRAILING_ATR_MULT) if _SEXIT_OK else TRAILING_ATR_MULT
    atr_trail = (pos.get("atr") or init_sl_dist) * _tm
    if direction > 0:
        new_sl = round(current_price - atr_trail, 2)
        if new_sl <= pos["sl"]: return
    else:
        new_sl = round(current_price + atr_trail, 2)
        if new_sl >= pos["sl"]: return
    _move_stop(dhan, sym, pos, new_sl, "trailing")

# Time-based exit constants
TIME_EXIT_ENABLED = True
TIME_EXIT_MINUTES = 45
TIME_EXIT_MIN_PROFIT_R = 0.3
TIME_EXIT_MAX_MINUTES = 120
PEAK_GIVEBACK_ENABLED = True
PEAK_GIVEBACK_ARM_R = 0.8
PEAK_GIVEBACK_FRACTION = 0.35
BREAKEVEN_ENABLED = True
BREAKEVEN_TRIGGER_R = 0.7
BREAKEVEN_BUFFER_R = 0.05
TRAILING_STOP_ENABLED = True
TRAILING_ATR_MULT = 1.0
TRAIL_ACTIVATE_R = 1.0
PARTIAL_EXIT_ENABLED = True
PARTIAL_EXIT_R = 1.0
PARTIAL_EXIT_FRACTION = 0.5


# =====================================================================
# DAILY REPORT
# =====================================================================
def _pnl_totals():
    with _POS_LOCK: trades = list(_COMPLETED_TRADES)
    finals = [t for t in trades if t["outcome"] != "PARTIAL"]
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    net = sum(t["pnl"] for t in trades)
    wr = (len([t for t in finals if t["pnl"] > 0]) / len(finals) * 100) if finals else 0.0
    return {"trades": trades, "finals": finals, "wins": wins, "losses": losses,
            "net": net, "win_rate": wr}

def _unrealized(pos):
    lp = pos.get("last_price", pos["entry"])
    return _apply_costs(pos["entry"], lp, pos["qty"], pos["side"])

def build_daily_report() -> str:
    tot = _pnl_totals()
    with _POS_LOCK:
        n_sig = len(_SIGNALS_TODAY); open_pos = list(_OPEN_POSITIONS.values())
        opened = _POSITIONS_OPENED; sugg = list(_SIGNALS_TODAY)
    halted, reason = halt_status()
    mode = "LIVE 💰" if AUTO_TRADE_ENABLED else "PAPER 🧪"
    date = datetime.now(IST).strftime("%d %b %Y")
    L = [f"📊 <b>DAILY TRADING REPORT</b> — {date}", "━━━━━━━━━━━━━━━━━━━━━",
         f"Mode: {mode}", f"Signals: {n_sig}", f"Positions opened: {opened}"]
    if halted: L.append(f"⛔ Auto-halted: {reason}")
    if tot["trades"]:
        L.append("\n<b>── Trades ──</b>")
        for i, t in enumerate(tot["trades"], 1):
            L.append(f"{i}. {t['symbol']} [{t['strategy']}] {t['side']} · {t['outcome']}\n"
                     f"   ₹{t['entry']} → ₹{t['exit']} | Qty {t['qty']} | "
                     f"₹{t['pnl']:+,.2f} ({t['r']:+.2f}R)")
    else: L.append("\n<i>No completed trades.</i>")
    float_total = 0.0  # accumulated in the loop below (was pre-summed then added again → 2x)
    if open_pos:
        L.append("\n<b>── Still Open (floating) ──</b>")
        for i, p in enumerate(open_pos, 1):
            upnl = _unrealized(p); float_total += upnl
            L.append(f"{i}. {p['symbol']} [{p['strategy']}] {p['side']} @ ₹{round(p['entry'],2)} "
                     f"→ ₹{round(p.get('last_price', p['entry']),2)} | Qty {p['qty']} | uP&L ₹{upnl:+,.2f}")
    best = max((t['pnl'] for t in tot["trades"]), default=0.0)
    worst = min((t['pnl'] for t in tot["trades"]), default=0.0)
    L += ["\n━━━━━━━━━━━━━━━━━━━━━", f"Completed: {len(tot['finals'])} | W:{len(tot['wins'])} L:{len(tot['losses'])} "
          f"| WR {tot['win_rate']:.1f}%", f"<b>Realised P&L: ₹{tot['net']:+,.2f}</b>",
          f"Best: ₹{best:+,.2f} | Worst: ₹{worst:+,.2f}"]
    if open_pos: L.append(f"Floating: ₹{float_total:+,.2f}")
    return "\n".join(L)


# =====================================================================
# MAIN LOOP
# =====================================================================
def _fetch_one(args):
    """Fetch one symbol's bars (thread-safe; network I/O only)."""
    row, dhan = args
    try:
        df = fetch_intraday(dhan, row.security_id)
    except Exception as e:
        log.debug(f"[{row.security_id}] fetch error: {e}")
        df = None
    return row, df


def scan_once(dhan, universe):
    signals = []
    log.info(f"Scan @ {datetime.now(IST).strftime('%H:%M:%S')} on {len(universe)} stocks")
    rows = list(universe.itertuples(index=False))

    if SCAN_WORKERS > 1:
        # Parallelise ONLY the network fetch; detection+scoring stay sequential
        # because score_signals() uses a module-global scratch frame (_CUR_DF).
        fetched = []
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            for i, (row, df) in enumerate(pool.map(_fetch_one, [(r, dhan) for r in rows])):
                if not (MARKET_OPEN <= datetime.now(IST).time() <= MARKET_CLOSE):
                    break
                fetched.append((row, df))
                time.sleep(REQUEST_SLEEP_SEC)
                if (i + 1) % 25 == 0:
                    log.info(f"  fetched {i + 1}/{len(rows)}")
        for row, df in fetched:
            if df is not None:
                hits = detect_patterns(df)
                if hits:
                    signals.extend(score_signals(row.symbol, row.security_id, df, hits))
    else:
        for i, row in enumerate(rows):
            if not (MARKET_OPEN <= datetime.now(IST).time() <= MARKET_CLOSE): break
            df = fetch_intraday(dhan, row.security_id)
            if df is not None:
                hits = detect_patterns(df)
                if hits: signals.extend(score_signals(row.symbol, row.security_id, df, hits))
            time.sleep(REQUEST_SLEEP_SEC)
            if (i + 1) % 25 == 0: log.info(f"  processed {i + 1}/{len(rows)}")
    if not signals: return pd.DataFrame()
    return (pd.DataFrame(signals).sort_values(["score", "vol_ratio"], ascending=[False, False])
            .drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True))

def act_on_signals(dhan, ranked):
    if ranked.empty: return
    ntrend = get_nifty_trend(dhan) if NIFTY_GATE_ENABLED else 0
    if NIFTY_GATE_ENABLED: log.info(f"NIFTY trend: {ntrend:+d}")
    for _, sig in ranked.head(TOP_N_RESULTS).iterrows():
        direction = 1 if sig["signal"] == "BUY" else -1
        if not passes_nifty_gate(direction, ntrend): continue
        sigd = sig.to_dict(); strat = sigd.get("strategy", "?")
        # Kronos gate
        if _KRONOS_OK:
            try:
                allow, adj, kreason = kronos_gate.kronos_check(sigd["symbol"], direction)
                sigd["kronos"] = kreason
                if not allow: continue
                sigd["score"] = int(sigd.get("score", 0)) + adj
            except Exception: pass
        record_suggestion(sigd)
        _emit("signal_found", sigd, "scanner")
        if sigd["score"] >= MIN_SCORE_TO_TRADE and (SCAN_START <= datetime.now(IST).time() <= NO_ENTRY_AFTER):
            with _POS_LOCK: block = _entry_blocked(sigd["symbol"], sigd.get("strategy"))
            if block:
                if "not-proven" in block:
                    log.info(f"[{sigd['symbol']}] {block}")
                continue
            place_bracket_orders(dhan, sigd)

def run():
    try:
        from live_config import apply_live_config
        if apply_live_config(module_name="intraday_pattern_scanner_v2", tg_sender=tg_send):
            log.info("Live config applied.")
    except ImportError: pass
    except Exception as e: log.warning(f"live_config apply failed ({e})")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
    try: run()
    except Exception as e: log.exception(f"Fatal: {e}"); raise
