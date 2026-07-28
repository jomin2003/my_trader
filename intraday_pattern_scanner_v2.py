"""
=====================================================================
 DHAN INTRADAY SCANNER + AUTO-TRADER + TELEGRAM  (v2 + 4 STRATEGIES)
---------------------------------------------------------------------
 Runs 4 strategies via multi_strategy_live.py:
   * OB SHORTS      - shooting star at bear OB zones (validated)
   * ORB            - opening range breakout, both directions
   * GAP-FILL       - fade 1-3% gaps back to prev close
   * CANDLE-STRUCT  - candlestick reversals with STRUCTURE-BASED exits
                      (targets placed just inside real S/R walls)

 Signals tagged by strategy. Candle-Struct signals carry struct_sl /
 struct_target — place_bracket_orders uses those structural exits
 instead of fixed ATR (fixes "target almost hits then reverses").

 Requires alongside this file:
   multi_strategy_live.py, structure_levels.py
   ob_data.csv, gap_data.csv

 AUTO_TRADE_ENABLED = False (paper trading). Keep False 2 weeks.
=====================================================================
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

# Thread-safety: lock protects config globals during monkey-patch from live_config/param_sweep
_CONFIG_LOCK = threading.Lock()

IST             = ZoneInfo("Asia/Kolkata")
MARKET_OPEN     = dtime(9, 15)
SCAN_START      = dtime(9, 30)
NO_ENTRY_AFTER  = dtime(14, 30)
MARKET_CLOSE    = dtime(15, 20)

POSITION_POLL_SEC     = 20
OCO_TIMEOUT_SEC       = 300
FORCE_EXIT_TIME       = dtime(15, 15)

# Trailing stop and partial profit-taking
TRAILING_STOP_ENABLED = True
TRAILING_ATR_MULT     = 1.0       # trail distance = this * ATR
TRAIL_ACTIVATE_R      = 1.0       # activate trail after 1R in profit
PARTIAL_EXIT_ENABLED  = True
PARTIAL_EXIT_R        = 1.0       # take partial at 1R
PARTIAL_EXIT_FRACTION = 0.5       # exit 50% of position

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
    if not isinstance(data, dict) or not data.get("close"): return 0
    df = pd.DataFrame({"high":data["high"],"low":data["low"],"close":data["close"],
                       "volume":data.get("volume",[0]*len(data["close"]))})
    if len(df) >= 2: df = df.iloc[:-1]
    if len(df) < 3: return 0
    tp = (df["high"]+df["low"]+df["close"])/3.0
    if df["volume"].sum() <= 0:
        vwap = float(tp.expanding().mean().iloc[-1])
    else:
        vwap = float((tp*df["volume"]).cumsum().iloc[-1]/max(df["volume"].cumsum().iloc[-1],1))
    ema20 = float(df["close"].ewm(span=20,adjust=False).mean().iloc[-1])
    c = float(df["close"].iloc[-1])
    if c > vwap and c > ema20: return +1
    if c < vwap and c < ema20: return -1
    return 0

def passes_nifty_gate(direction, ntrend):
    if not NIFTY_GATE_ENABLED: return True
    if NIFTY_STRICT:
        return (direction>0 and ntrend==+1) or (direction<0 and ntrend==-1)
    return (direction>0 and ntrend>=0) or (direction<0 and ntrend<=0)


# =====================================================================
# TELEGRAM
# =====================================================================
def tg_send(text, silent=False):
    if not TELEGRAM_ENABLED: return
    if TELEGRAM_BOT_TOKEN.startswith("YOUR_") or TELEGRAM_CHAT_ID.startswith("YOUR_"): return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id":TELEGRAM_CHAT_ID,"text":text,"parse_mode":"HTML",
                  "disable_notification":silent}, timeout=5)
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
    eq = df[(df[C("SEM_EXM_EXCH_ID")].astype(str).str.upper()=="NSE") &
            (df[C("SEM_SEGMENT")].astype(str).str.upper()=="E") &
            (df[C("SEM_INSTRUMENT_NAME")].astype(str).str.upper()=="EQUITY")].copy()
    if USE_FNO_UNIVERSE_ONLY:
        fno = df[(df[C("SEM_EXM_EXCH_ID")].astype(str).str.upper()=="NSE") &
                 (df[C("SEM_INSTRUMENT_NAME")].astype(str).str.upper().isin(["FUTSTK","OPTSTK"]))]
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
            resp = dhan.intraday_minute_data(security_id,"NSE_EQ","EQUITY",today,today,CANDLE_INTERVAL_MIN)
        except Exception as e:
            log.debug(f"[{security_id}] fallback error: {e}"); time.sleep(BACKOFF_ON_ERROR_SEC); return None
    except Exception as e:
        log.debug(f"[{security_id}] fetch error: {e}"); time.sleep(BACKOFF_ON_ERROR_SEC); return None
    if not isinstance(resp, dict): return None
    data = resp.get("data") if resp.get("data") else resp
    if not isinstance(data, dict) or "open" not in data or not data["open"]: return None
    df = pd.DataFrame({"open":data["open"],"high":data["high"],"low":data["low"],
                       "close":data["close"],"volume":data.get("volume",[0]*len(data["open"]))})
    ts = data.get("timestamp") or data.get("start_Time") or data.get("startTime")
    if ts:
        try: df["ts"] = pd.to_datetime(ts, unit="s", utc=True).tz_convert(IST)
        except Exception: df["ts"] = pd.NaT
    df = df.dropna(subset=["open","high","low","close"]).reset_index(drop=True)
    if len(df) < MIN_CANDLES_NEEDED: return None
    df = df.iloc[:-1].reset_index(drop=True)  # drop forming bar
    return df if len(df) >= MIN_CANDLES_NEEDED-1 else None


# =====================================================================
# INDICATORS (used by multi_strategy_live + structure engine)
# =====================================================================
def wilder_atr(df, period=14):
    if len(df) < period+1: return None
    h=df["high"].values.astype(float);l=df["low"].values.astype(float);c=df["close"].values.astype(float)
    prev=np.concatenate([[c[0]],c[:-1]]);tr=np.maximum.reduce([h-l,np.abs(h-prev),np.abs(l-prev)])
    atr=np.zeros_like(tr);atr[period-1]=tr[:period].mean()
    for i in range(period,len(tr)):atr[i]=(atr[i-1]*(period-1)+tr[i])/period
    v=float(atr[-1]);return v if v>0 else None

def rolling_vwap(df):
    if len(df)<3: return None
    tp=(df["high"]+df["low"]+df["close"])/3.0
    vol=df["volume"].replace(0,np.nan)
    if vol.sum()<=0 or vol.isna().all(): return None
    return float((tp*df["volume"]).sum()/max(df["volume"].sum(),1))

def ema(series, span):
    if len(series)<span: return None
    return float(series.ewm(span=span,adjust=False).mean().iloc[-1])


def adx(df, period=14):
    """Compute ADX (Average Directional Index). Returns ADX value or None."""
    if len(df) < period * 2:
        return None
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    prev_h = np.concatenate([[h[0]], h[:-1]])
    prev_l = np.concatenate([[l[0]], l[:-1]])
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
    plus_dm = np.where((h - prev_h) > (prev_l - l), np.maximum(h - prev_h, 0), 0.0)
    minus_dm = np.where((prev_l - l) > (h - prev_h), np.maximum(prev_l - l, 0), 0.0)
    # Wilder smoothing
    atr_s = np.zeros_like(tr); atr_s[period-1] = tr[:period].mean()
    pdm_s = np.zeros_like(tr); pdm_s[period-1] = plus_dm[:period].mean()
    ndm_s = np.zeros_like(tr); ndm_s[period-1] = minus_dm[:period].mean()
    for i in range(period, len(tr)):
        atr_s[i] = (atr_s[i-1] * (period - 1) + tr[i]) / period
        pdm_s[i] = (pdm_s[i-1] * (period - 1) + plus_dm[i]) / period
        ndm_s[i] = (ndm_s[i-1] * (period - 1) + minus_dm[i]) / period
    with np.errstate(divide='ignore', invalid='ignore'):
        plus_di = 100.0 * pdm_s / np.where(atr_s > 0, atr_s, 1e-9)
        minus_di = 100.0 * ndm_s / np.where(atr_s > 0, atr_s, 1e-9)
        dx = 100.0 * np.abs(plus_di - minus_di) / np.where(plus_di + minus_di > 0, plus_di + minus_di, 1e-9)
    adx_arr = np.zeros_like(dx)
    start = 2 * period - 1
    if start >= len(dx):
        return None
    adx_arr[start] = dx[period:start+1].mean()
    for i in range(start + 1, len(dx)):
        adx_arr[i] = (adx_arr[i-1] * (period - 1) + dx[i]) / period
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


# =====================================================================
# TRADE MANAGEMENT
# =====================================================================
_OPEN_POSITIONS: dict[str, dict] = {}
_TRADED_TODAY:  set[str]        = set()
_COMPLETED_TRADES: list[dict]   = []


def get_daily_pnl_summary() -> str:
    if not _COMPLETED_TRADES:
        return "No completed trades today."
    total_pnl = sum(t["pnl"] for t in _COMPLETED_TRADES)
    wins = [t for t in _COMPLETED_TRADES if t["pnl"] > 0]
    losses = [t for t in _COMPLETED_TRADES if t["pnl"] <= 0]
    win_rate = len(wins) / len(_COMPLETED_TRADES) * 100 if _COMPLETED_TRADES else 0
    lines = [
        f"📊 <b>DAILY P&L SUMMARY</b>",
        f"Trades: {len(_COMPLETED_TRADES)} | W:{len(wins)} L:{len(losses)}",
        f"Win rate: {win_rate:.0f}%",
        f"Net P&L: ₹{total_pnl:+,.2f}",
    ]
    if wins:
        lines.append(f"Best win: ₹{max(t['pnl'] for t in wins):+,.2f}")
    if losses:
        lines.append(f"Worst loss: ₹{min(t['pnl'] for t in losses):+,.2f}")
    for t in _COMPLETED_TRADES:
        lines.append(f"  {t['symbol']} [{t['strategy']}] {t['outcome']}: ₹{t['pnl']:+,.2f}")
    return "\n".join(lines)


def compute_sl_target(entry, direction, atr_val):
    if USE_ATR_STOP and atr_val and atr_val > 0:
        sl_dist = ATR_MULTIPLIER * atr_val
    else:
        sl_dist = FALLBACK_SL_PERCENT * entry
    sl_dist = max(MIN_SL_PCT*entry, min(sl_dist, MAX_SL_PCT*entry))
    if direction > 0:
        sl=round(entry-sl_dist,2); tgt=round(entry+RISK_REWARD_RATIO*sl_dist,2)
    else:
        sl=round(entry+sl_dist,2); tgt=round(entry-RISK_REWARD_RATIO*sl_dist,2)
    return sl, tgt, sl_dist


def compute_quantity(entry, sl_dist):
    if sl_dist <= 0 or entry <= 0: return 0
    return max(0, min(int(MAX_RISK_PER_TRADE//sl_dist), int(MAX_CAPITAL_PER_TRADE//entry)))


def _order_id(resp):
    if not isinstance(resp, dict): return None
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
    if not oid: return
    try: dhan.cancel_order(oid)
    except Exception as e: log.debug(f"cancel fail {oid}: {e}")


def place_bracket_orders(dhan, sig):
    symbol=sig["symbol"]; sec_id=sig["security_id"]; entry_px=sig["price"]
    dirn=sig["direction"]; atr_val=sig.get("atr"); strat=sig.get("strategy","?")

    if symbol in _OPEN_POSITIONS or symbol in _TRADED_TODAY: return
    if len(_OPEN_POSITIONS) >= MAX_OPEN_POSITIONS:
        log.info(f"Max positions reached -> skip {symbol}"); return

    # ---- STRUCTURAL EXITS: if the signal carries struct_sl/target, use them ----
    if sig.get("struct_sl") is not None and sig.get("struct_target") is not None:
        sl_px  = float(sig["struct_sl"])
        tgt_px = float(sig["struct_target"])
        sl_dist = abs(entry_px - sl_px)
        exit_src = "structure"
    else:
        sl_px, tgt_px, sl_dist = compute_sl_target(entry_px, dirn, atr_val)
        exit_src = "atr"

    qty = compute_quantity(entry_px, sl_dist)
    if qty <= 0:
        log.info(f"[{symbol}] qty=0 -> skip"); return

    side="BUY" if dirn>0 else "SELL"; exit_side="SELL" if dirn>0 else "BUY"

    tg_txt = (f"🎯 <b>ORDER ATTEMPT</b> [{strat}] {sig['pattern']}\n"
              f"<b>{symbol}</b> {side} ×{qty}\n"
              f"Entry~₹{entry_px}  SL ₹{sl_px}  TGT ₹{tgt_px} (exits:{exit_src})\n"
              f"Score {sig['score']} | Vol×{sig['vol_ratio']}")

    if not AUTO_TRADE_ENABLED:
        log.info(f"[DRY-RUN][{strat}] {side} {qty} {symbol} @{entry_px} SL {sl_px} TGT {tgt_px} ({exit_src})")
        tg_send("🧪 <b>DRY-RUN</b>\n" + tg_txt)
        _TRADED_TODAY.add(symbol); return

    order_kw = dict(security_id=sec_id, exchange_segment=getattr(dhan,"NSE","NSE_EQ"),
        transaction_type=getattr(dhan,side,side), quantity=qty,
        product_type=getattr(dhan,"INTRA","INTRADAY"))
    try:
        entry_resp = dhan.place_order(**order_kw, order_type=getattr(dhan,"MARKET","MARKET"), price=0)
    except Exception as e:
        log.error(f"[{symbol}] entry error: {e}"); tg_send(f"❌ Entry error {symbol}: {e}"); return
    status = str(entry_resp.get("status","")).lower() if isinstance(entry_resp,dict) else ""
    if not any(k in status for k in ("success","pending","traded")):
        log.error(f"[{symbol}] entry rejected: {entry_resp}")
        tg_send(f"❌ Entry rejected {symbol}\n<code>{entry_resp}</code>"); return
    entry_id = _order_id(entry_resp)

    exit_kw = dict(security_id=sec_id, exchange_segment=getattr(dhan,"NSE","NSE_EQ"),
        transaction_type=getattr(dhan,exit_side,exit_side), quantity=qty,
        product_type=getattr(dhan,"INTRA","INTRADAY"))
    sl_id=None
    try:
        sl_resp=dhan.place_order(**exit_kw,order_type=getattr(dhan,"SLM","STOP_LOSS_MARKET"),
            price=0,trigger_price=sl_px); sl_id=_order_id(sl_resp)
    except Exception as e:
        log.error(f"[{symbol}] SL fail: {e}"); tg_send(f"⚠️ SL failed {symbol}: {e}")
    tgt_id=None
    try:
        tgt_resp=dhan.place_order(**exit_kw,order_type=getattr(dhan,"LIMIT","LIMIT"),price=tgt_px)
        tgt_id=_order_id(tgt_resp)
    except Exception as e:
        log.error(f"[{symbol}] TGT fail: {e}"); tg_send(f"⚠️ TGT failed {symbol}: {e}")

    _OPEN_POSITIONS[symbol]={"entry_id":entry_id,"sl_id":sl_id,"tgt_id":tgt_id,"qty":qty,
        "entry":entry_px,"sl":sl_px,"target":tgt_px,"side":side,"opened_at":now_ist(),
        "strategy":strat,"security_id":sec_id,"atr":atr_val,"partial_done":False}
    _TRADED_TODAY.add(symbol)
    tg_send(f"✅ <b>ORDER PLACED</b> [{strat}] {sig['pattern']}\n"
            f"<b>{symbol}</b> {side} ×{qty}\nEntry~₹{entry_px}  SL ₹{sl_px}  🎯 ₹{tgt_px}\n"
            f"IDs: {entry_id} / {sl_id} / {tgt_id}")


def _update_trailing_stop(dhan, sym, pos, current_price):
    """Move SL toward price when position moves in our favor past TRAIL_ACTIVATE_R."""
    if not TRAILING_STOP_ENABLED:
        return
    entry = pos["entry"]; sl_dist = abs(entry - pos["sl"])
    direction = 1 if pos["side"] == "BUY" else -1
    profit = (current_price - entry) * direction
    if profit < TRAIL_ACTIVATE_R * sl_dist:
        return
    atr_trail = pos.get("atr", sl_dist) * TRAILING_ATR_MULT
    if direction > 0:
        new_sl = round(current_price - atr_trail, 2)
        if new_sl <= pos["sl"]:
            return
    else:
        new_sl = round(current_price + atr_trail, 2)
        if new_sl >= pos["sl"]:
            return
    # Cancel old SL and place new one
    _cancel(dhan, pos["sl_id"])
    exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
    try:
        sl_resp = dhan.place_order(
            security_id=pos.get("security_id", ""),
            exchange_segment=getattr(dhan, "NSE", "NSE_EQ"),
            transaction_type=getattr(dhan, exit_side, exit_side),
            quantity=pos["qty"],
            product_type=getattr(dhan, "INTRA", "INTRADAY"),
            order_type=getattr(dhan, "SLM", "STOP_LOSS_MARKET"),
            price=0, trigger_price=new_sl)
        pos["sl_id"] = _order_id(sl_resp)
        pos["sl"] = new_sl
        log.info(f"[{sym}] Trailing SL moved to ₹{new_sl}")
    except Exception as e:
        log.warning(f"[{sym}] Trailing SL update failed: {e}")


def _partial_exit(dhan, sym, pos, current_price):
    """Exit a fraction of position when profit reaches PARTIAL_EXIT_R."""
    if not PARTIAL_EXIT_ENABLED or pos.get("partial_done"):
        return
    entry = pos["entry"]; sl_dist = abs(entry - pos["sl"])
    direction = 1 if pos["side"] == "BUY" else -1
    profit = (current_price - entry) * direction
    if profit < PARTIAL_EXIT_R * sl_dist:
        return
    exit_qty = max(1, int(pos["qty"] * PARTIAL_EXIT_FRACTION))
    remaining = pos["qty"] - exit_qty
    if remaining <= 0:
        return
    exit_side = "SELL" if pos["side"] == "BUY" else "BUY"
    try:
        dhan.place_order(
            security_id=pos.get("security_id", ""),
            exchange_segment=getattr(dhan, "NSE", "NSE_EQ"),
            transaction_type=getattr(dhan, exit_side, exit_side),
            quantity=exit_qty,
            product_type=getattr(dhan, "INTRA", "INTRADAY"),
            order_type=getattr(dhan, "MARKET", "MARKET"),
            price=0)
        pos["qty"] = remaining
        pos["partial_done"] = True
        tg_send(f"📊 Partial exit {exit_qty} of <b>{sym}</b> @ ~₹{current_price} [{pos.get('strategy','?')}]")
        log.info(f"[{sym}] Partial exit {exit_qty} units, {remaining} remaining")
    except Exception as e:
        log.warning(f"[{sym}] Partial exit failed: {e}")


def monitor_oco(dhan):
    if not _OPEN_POSITIONS: return
    done=[]
    now = now_ist()
    force_exit = now.time() >= FORCE_EXIT_TIME
    for sym,pos in list(_OPEN_POSITIONS.items()):
        # Force-cancel all exits near market close
        if force_exit:
            _cancel(dhan, pos["sl_id"]); _cancel(dhan, pos["tgt_id"])
            tg_send(f"⏰ Force-exit near close: <b>{sym}</b> [{pos.get('strategy','?')}]")
            done.append(sym); continue
        # Timeout: if position open too long, warn
        opened = pos.get("opened_at")
        if opened and (now - opened).total_seconds() > OCO_TIMEOUT_SEC:
            log.warning(f"[{sym}] OCO open >{OCO_TIMEOUT_SEC}s — checking status")
        sl_st=_order_status(dhan,pos["sl_id"]) if pos["sl_id"] else ""
        tgt_st=_order_status(dhan,pos["tgt_id"]) if pos["tgt_id"] else ""
        if any(k in sl_st for k in ("TRADED","EXECUTED","FILLED")):
            _cancel(dhan,pos["tgt_id"])
            pnl = (pos["sl"] - pos["entry"]) * pos["qty"] * (1 if pos["side"]=="BUY" else -1)
            _COMPLETED_TRADES.append({"symbol":sym,"strategy":pos.get("strategy","?"),
                "outcome":"SL","pnl":round(pnl,2),"entry":pos["entry"],"exit":pos["sl"]})
            tg_send(f"🛑 SL hit: <b>{sym}</b> @ ₹{pos['sl']} [{pos.get('strategy','?')}] P&L: ₹{pnl:+,.2f}")
            done.append(sym)
        elif any(k in tgt_st for k in ("TRADED","EXECUTED","FILLED")):
            _cancel(dhan,pos["sl_id"])
            pnl = (pos["target"] - pos["entry"]) * pos["qty"] * (1 if pos["side"]=="BUY" else -1)
            _COMPLETED_TRADES.append({"symbol":sym,"strategy":pos.get("strategy","?"),
                "outcome":"TARGET","pnl":round(pnl,2),"entry":pos["entry"],"exit":pos["target"]})
            tg_send(f"🎉 Target hit: <b>{sym}</b> @ ₹{pos['target']} [{pos.get('strategy','?')}] P&L: ₹{pnl:+,.2f}")
            done.append(sym)
        elif "REJECTED" in sl_st and "REJECTED" in tgt_st:
            tg_send(f"⚠️ Both exits rejected {sym}, cleaning up"); done.append(sym)
        else:
            # Trailing stop + partial profit on open positions
            try:
                today = now.strftime("%Y-%m-%d")
                sec_id = pos.get("security_id", "")
                if sec_id:
                    resp = dhan.intraday_minute_data(
                        security_id=sec_id, exchange_segment="NSE_EQ",
                        instrument_type="EQUITY", from_date=today,
                        to_date=today, interval=CANDLE_INTERVAL_MIN)
                    data = resp.get("data", resp) if isinstance(resp, dict) else {}
                    if isinstance(data, dict) and data.get("close"):
                        ltp = float(data["close"][-1])
                        if AUTO_TRADE_ENABLED:
                            _partial_exit(dhan, sym, pos, ltp)
                            _update_trailing_stop(dhan, sym, pos, ltp)
            except Exception as e:
                log.debug(f"[{sym}] trail/partial check: {e}")
    for sym in done: _OPEN_POSITIONS.pop(sym, None)


# =====================================================================
# MAIN LOOP
# =====================================================================
def wait_until(target_t):
    while True:
        now=now_ist()
        if now.time()>=target_t: return
        w=(datetime.combine(now.date(),target_t,tzinfo=IST)-now).total_seconds()
        log.info(f"Waiting {int(w)}s until {target_t}"); time.sleep(min(max(w,1),60))


def scan_once(dhan, universe):
    signals=[]
    log.info(f"Scan @ {now_ist().strftime('%H:%M:%S')} on {len(universe)} stocks")
    for i,row in enumerate(universe.itertuples(index=False)):
        if not market_open_now(): break
        df=fetch_intraday(dhan,row.security_id)
        if df is not None:
            hits=detect_patterns(df)
            if hits: signals.extend(score_signals(row.symbol,row.security_id,df,hits))
        time.sleep(REQUEST_SLEEP_SEC)
        if (i+1)%25==0: log.info(f"  processed {i+1}/{len(universe)}")
    if not signals: return pd.DataFrame()
    return (pd.DataFrame(signals).sort_values(["score","vol_ratio"],ascending=[False,False])
            .drop_duplicates(subset=["symbol"],keep="first").reset_index(drop=True))


def act_on_signals(dhan, ranked):
    if ranked.empty: return
    ntrend=get_nifty_trend(dhan) if NIFTY_GATE_ENABLED else 0
    if NIFTY_GATE_ENABLED: log.info(f"NIFTY trend: {ntrend:+d}")
    for _,sig in ranked.head(TOP_N_RESULTS).iterrows():
        direction=1 if sig["signal"]=="BUY" else -1
        if not passes_nifty_gate(direction,ntrend): continue
        strat=sig.get("strategy","?")
        tg_send(f"📊 <b>{sig['symbol']}</b> {sig['signal']} [{strat}] {sig['pattern']}\n"
                f"₹{sig['price']} | Score {sig['score']} | Vol×{sig['vol_ratio']} | {sig['time']}", silent=True)
        if sig["score"]>=MIN_SCORE_TO_TRADE and in_session_for_entries():
            place_bracket_orders(dhan, sig.to_dict())


def run():
    try:
        from live_config import apply_live_config
        if apply_live_config(module_name="intraday_pattern_scanner_v2", tg_sender=tg_send):
            log.info("Live config applied.")
    except ImportError:
        log.debug("live_config not found.")
    except Exception as e:
        log.warning(f"live_config apply failed ({e})")

    if DHAN_SDK_V2:
        dhan = dhanhq(DhanContext(CLIENT_ID, ACCESS_TOKEN))
    else:
        dhan = dhanhq(CLIENT_ID, ACCESS_TOKEN)   # type: ignore

    universe = load_intraday_universe()
    wait_until(SCAN_START)
    tg_send(f"🚀 <b>Scanner started</b>\nUniverse: {len(universe)} | {CANDLE_INTERVAL_MIN}m\n"
            f"Auto-trade: {'ON ✅' if AUTO_TRADE_ENABLED else 'OFF (dry-run) 🧪'}\n"
            f"Strategies: OB Shorts + ORB + Gap-Fill + Candle-Struct")

    all_signals=[]; next_scan=now_ist()
    while market_open_now():
        now=now_ist()
        if now>=next_scan:
            try:
                ranked=scan_once(dhan,universe)
                if not ranked.empty:
                    all_signals.append(ranked)
                    log.info(f"\n=== Signals @ {now.strftime('%H:%M')} ===")
                    log.info(ranked.head(TOP_N_RESULTS).to_string(index=False))
                    act_on_signals(dhan,ranked)
            except Exception as e:
                log.exception("scan error"); tg_send(f"⚠️ Scan error: {e}")
            slot=(now.minute//CANDLE_INTERVAL_MIN+1)*CANDLE_INTERVAL_MIN
            next_scan=now.replace(second=5,microsecond=0)+timedelta(minutes=slot-now.minute)
        try: monitor_oco(dhan)
        except Exception as e: log.debug(f"oco error: {e}")
        time.sleep(POSITION_POLL_SEC)

    if all_signals:
        combined=pd.concat(all_signals,ignore_index=True)
        fname=f"signals_{now_ist().strftime('%Y%m%d')}.csv"
        combined.to_csv(fname,index=False)
        if "strategy" in combined.columns:
            b=" | ".join(f"{k}:{v}" for k,v in combined["strategy"].value_counts().items())
            tg_send(f"📈 <b>EOD</b>: {len(combined)} signals ({b}) | traded {len(_TRADED_TODAY)}")
        else:
            tg_send(f"📈 <b>EOD</b>: {len(combined)} signals | traded {len(_TRADED_TODAY)}")
    else:
        tg_send("ℹ️ EOD: no actionable signals today.")
    # Daily P&L summary
    tg_send(get_daily_pnl_summary())
    _COMPLETED_TRADES.clear()


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        log.exception("Fatal error"); tg_send(f"🚨 <b>Fatal error</b>: {e}"); raise
