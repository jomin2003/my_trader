"""
=====================================================================
 DHAN INTRADAY SCANNER + AUTO-TRADER + TELEGRAM  (v2 + 3 STRATEGIES)
---------------------------------------------------------------------
 Runs 3 strategies together via multi_strategy_live.py:
   * OB SHORTS  (validated p<0.0001) - shooting star at bear OB zones
   * ORB        - opening range breakout, both directions
   * GAP-FILL   - fade 1-3% gaps back to prev close

 Each signal is tagged by strategy in Telegram alerts.

 Requires alongside this file:
   multi_strategy_live.py
   ob_data.csv       (from precompute_order_blocks.py)
   gap_data.csv      (from precompute_gapfill.py)

 AUTO_TRADE_ENABLED = False (paper trading). Keep it False for 2 weeks.
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

# ---- Auto-Trading ----
AUTO_TRADE_ENABLED    = False   # KEEP FALSE for paper trading
MIN_SCORE_TO_TRADE    = 6       # multi-strategy signals score 6-8
RISK_REWARD_RATIO     = 2.0     # blended default across 3 strategies
MAX_RISK_PER_TRADE    = 500     # ₹
MAX_CAPITAL_PER_TRADE = 25000   # ₹
MAX_OPEN_POSITIONS    = 5
USE_ATR_STOP          = True
ATR_MULTIPLIER        = 1.5
FALLBACK_SL_PERCENT   = 0.005
MIN_SL_PCT            = 0.003
MAX_SL_PCT            = 0.015
REQUIRE_CONFIRMATION  = False   # multi-strategy handles its own confirmation

# ---- Universe / data ----
USE_FNO_UNIVERSE_ONLY = True
MAX_STOCKS            = 180
CANDLE_INTERVAL_MIN   = 5
MIN_CANDLES_NEEDED    = 4        # multi-strategy needs few bars
TOP_N_RESULTS         = 20
MIN_TURNOVER_LAKHS    = 25

# ---- NIFTY trend gate ----
NIFTY_GATE_ENABLED    = True
NIFTY_STRICT          = False
_NIFTY_SEC_ID         = "13"
_NIFTY_EXCH_SEG       = "IDX_I"
_NIFTY_INSTR_TYPE     = "INDEX"

# ---- Rate limiting ----
REQUEST_SLEEP_SEC     = 0.22
BACKOFF_ON_ERROR_SEC  = 2.0

# ---- Market timings (IST) ----
IST             = ZoneInfo("Asia/Kolkata")
MARKET_OPEN     = dtime(9, 15)
SCAN_START      = dtime(9, 30)
NO_ENTRY_AFTER  = dtime(14, 30)
MARKET_CLOSE    = dtime(15, 20)

SCAN_EVERY_SEC        = CANDLE_INTERVAL_MIN * 60
POSITION_POLL_SEC     = 20

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
            "chat_id": TELEGRAM_CHAT_ID, "text": text,
            "parse_mode": "HTML", "disable_notification": silent,
        }, timeout=5)
    except Exception as e:
        log.debug(f"Telegram send failed: {e}")


# =====================================================================
# UNIVERSE
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
# HISTORICAL DATA
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
        "open":   data["open"], "high": data["high"],
        "low":    data["low"],  "close": data["close"],
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

    # Drop still-forming last bar
    df = df.iloc[:-1].reset_index(drop=True)
    return df if len(df) >= MIN_CANDLES_NEEDED - 1 else None


# =====================================================================
# INDICATORS (used by multi_strategy_live)
# =====================================================================
def wilder_atr(df: pd.DataFrame, period: int = 14) -> float | None:
    if len(df) < period + 1:
        return None
    h = df["high"].values.astype(float)
    l = df["low"].values.astype(float)
    c = df["close"].values.astype(float)
    prev_c = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum.reduce([h - l, np.abs(h - prev_c), np.abs(l - prev_c)])
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
# DEFAULT DETECTORS (fallback if multi_strategy_live not present)
# =====================================================================
def detect_patterns(df: pd.DataFrame) -> list[tuple[int, str, int]]:
    return []   # replaced at import time below


def score_signals(symbol, security_id, df, hits) -> list[dict]:
    return []   # replaced at import time below


# =====================================================================
# STRATEGY OVERRIDE — 3 STRATEGIES (module-level, runs at import)
# =====================================================================
# OB Shorts + ORB + Gap-Fill, each signal tagged by strategy.
# PAPER TRADING ONLY — AUTO_TRADE_ENABLED must stay False.
try:
    import multi_strategy_live
    detect_patterns = multi_strategy_live.detect_patterns
    score_signals   = multi_strategy_live.score_signals
    log.info("[SCANNER] Multi-strategy active: OB Shorts + ORB + Gap-Fill")
except ImportError as e:
    log.warning(f"[SCANNER] multi_strategy_live not found ({e}) — no signals will fire")
except Exception as e:
    log.error(f"[SCANNER] multi wire-in failed: {e}")


# =====================================================================
# TRADE MANAGEMENT
# =====================================================================
_OPEN_POSITIONS: dict[str, dict] = {}
_TRADED_TODAY:  set[str]        = set()


def compute_sl_target(entry: float, direction: int, atr_val: float | None):
    if USE_ATR_STOP and atr_val and atr_val > 0:
        sl_dist = ATR_MULTIPLIER * atr_val
    else:
        sl_dist = FALLBACK_SL_PERCENT * entry
    sl_dist = max(MIN_SL_PCT * entry, min(sl_dist, MAX_SL_PCT * entry))
    if direction > 0:
        sl = round(entry - sl_dist, 2); target = round(entry + RISK_REWARD_RATIO * sl_dist, 2)
    else:
        sl = round(entry + sl_dist, 2); target = round(entry - RISK_REWARD_RATIO * sl_dist, 2)
    return sl, target, sl_dist


def compute_quantity(entry: float, sl_dist: float) -> int:
    if sl_dist <= 0 or entry <= 0:
        return 0
    return max(0, min(int(MAX_RISK_PER_TRADE // sl_dist),
                      int(MAX_CAPITAL_PER_TRADE // entry)))


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
    strat    = sig.get("strategy", "?")

    if symbol in _OPEN_POSITIONS or symbol in _TRADED_TODAY:
        return
    if len(_OPEN_POSITIONS) >= MAX_OPEN_POSITIONS:
        log.info(f"Max positions reached -> skip {symbol}")
        return

    sl_px, tgt_px, sl_dist = compute_sl_target(entry_px, dirn, atr_val)
    qty = compute_quantity(entry_px, sl_dist)
    if qty <= 0:
        log.info(f"[{symbol}] qty=0 -> skip")
        return

    side      = "BUY"  if dirn > 0 else "SELL"
    exit_side = "SELL" if dirn > 0 else "BUY"

    tg_txt = (
        f"🎯 <b>ORDER ATTEMPT</b> [{strat}] {sig['pattern']}\n"
        f"<b>{symbol}</b> {side} ×{qty}\n"
        f"Entry~₹{entry_px}  SL ₹{sl_px}  TGT ₹{tgt_px} (1:{RISK_REWARD_RATIO})\n"
        f"Score {sig['score']} | Vol×{sig['vol_ratio']}"
    )

    if not AUTO_TRADE_ENABLED:
        log.info(f"[DRY-RUN][{strat}] {side} {qty} {symbol} @{entry_px} SL {sl_px} TGT {tgt_px}")
        tg_send("🧪 <b>DRY-RUN</b>\n" + tg_txt)
        _TRADED_TODAY.add(symbol)
        return

    order_kw = dict(
        security_id=sec_id,
        exchange_segment=getattr(dhan, "NSE", "NSE_EQ"),
        transaction_type=getattr(dhan, side, side),
        quantity=qty,
        product_type=getattr(dhan, "INTRA", "INTRADAY"),
    )
    try:
        entry_resp = dhan.place_order(**order_kw,
                                      order_type=getattr(dhan, "MARKET", "MARKET"), price=0)
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
    sl_id = None
    try:
        sl_resp = dhan.place_order(**exit_kw,
                                   order_type=getattr(dhan, "SLM", "STOP_LOSS_MARKET"),
                                   price=0, trigger_price=sl_px)
        sl_id = _order_id(sl_resp)
    except Exception as e:
        log.error(f"[{symbol}] SL fail: {e}")
        tg_send(f"⚠️ SL failed {symbol}: {e}")
    tgt_id = None
    try:
        tgt_resp = dhan.place_order(**exit_kw,
                                    order_type=getattr(dhan, "LIMIT", "LIMIT"), price=tgt_px)
        tgt_id = _order_id(tgt_resp)
    except Exception as e:
        log.error(f"[{symbol}] TGT fail: {e}")
        tg_send(f"⚠️ TGT failed {symbol}: {e}")

    _OPEN_POSITIONS[symbol] = {
        "entry_id": entry_id, "sl_id": sl_id, "tgt_id": tgt_id,
        "qty": qty, "entry": entry_px, "sl": sl_px, "target": tgt_px,
        "side": side, "opened_at": now_ist(), "strategy": strat,
    }
    _TRADED_TODAY.add(symbol)
    tg_send(
        f"✅ <b>ORDER PLACED</b> [{strat}] {sig['pattern']}\n"
        f"<b>{symbol}</b> {side} ×{qty}\n"
        f"Entry~₹{entry_px}  SL ₹{sl_px}  🎯 ₹{tgt_px}\n"
        f"IDs: {entry_id} / {sl_id} / {tgt_id}"
    )


def monitor_oco(dhan) -> None:
    if not _OPEN_POSITIONS:
        return
    done = []
    for sym, pos in list(_OPEN_POSITIONS.items()):
        sl_st  = _order_status(dhan, pos["sl_id"])  if pos["sl_id"]  else ""
        tgt_st = _order_status(dhan, pos["tgt_id"]) if pos["tgt_id"] else ""
        if any(k in sl_st for k in ("TRADED", "EXECUTED", "FILLED")):
            _cancel(dhan, pos["tgt_id"])
            tg_send(f"🛑 SL hit: <b>{sym}</b> @ ₹{pos['sl']} [{pos.get('strategy','?')}]")
            done.append(sym)
        elif any(k in tgt_st for k in ("TRADED", "EXECUTED", "FILLED")):
            _cancel(dhan, pos["sl_id"])
            tg_send(f"🎉 Target hit: <b>{sym}</b> @ ₹{pos['target']} [{pos.get('strategy','?')}]")
            done.append(sym)
        elif "REJECTED" in sl_st and "REJECTED" in tgt_st:
            tg_send(f"⚠️ Both exits rejected for <b>{sym}</b>, cleaning up")
            done.append(sym)
    for sym in done:
        _OPEN_POSITIONS.pop(sym, None)


# =====================================================================
# MAIN LOOP
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
                signals.extend(score_signals(row.symbol, row.security_id, df, hits))
        time.sleep(REQUEST_SLEEP_SEC)
        if (i + 1) % 25 == 0:
            log.info(f"  processed {i + 1}/{len(universe)}")

    if not signals:
        return pd.DataFrame()

    ranked = (pd.DataFrame(signals)
              .sort_values(["score", "vol_ratio"], ascending=[False, False])
              .drop_duplicates(subset=["symbol"], keep="first")
              .reset_index(drop=True))
    return ranked


def act_on_signals(dhan, ranked: pd.DataFrame) -> None:
    if ranked.empty:
        return
    ntrend = get_nifty_trend(dhan) if NIFTY_GATE_ENABLED else 0
    if NIFTY_GATE_ENABLED:
        log.info(f"NIFTY trend: {ntrend:+d}  (strict={NIFTY_STRICT})")
    for _, sig in ranked.head(TOP_N_RESULTS).iterrows():
        direction = 1 if sig["signal"] == "BUY" else -1
        if not passes_nifty_gate(direction, ntrend):
            log.debug(f"[{sig['symbol']}] rejected by NIFTY gate")
            continue
        strat = sig.get("strategy", "?")
        tg_send(
            f"📊 <b>{sig['symbol']}</b> {sig['signal']} [{strat}] {sig['pattern']}\n"
            f"₹{sig['price']} | Score {sig['score']} | Vol×{sig['vol_ratio']} | {sig['time']}",
            silent=True,
        )
        if sig["score"] >= MIN_SCORE_TO_TRADE and in_session_for_entries():
            place_bracket_orders(dhan, sig.to_dict())


def run() -> None:
    # Auto-apply live_config.json if present
    try:
        from live_config import apply_live_config
        applied = apply_live_config(module_name="intraday_pattern_scanner_v2", tg_sender=tg_send)
        if applied:
            log.info("Live config applied.")
    except ImportError:
        log.debug("live_config not found — using hardcoded defaults.")
    except Exception as e:
        log.warning(f"live_config apply failed ({e})")

    if DHAN_SDK_V2:
        ctx = DhanContext(CLIENT_ID, ACCESS_TOKEN)
        dhan = dhanhq(ctx)
    else:
        dhan = dhanhq(CLIENT_ID, ACCESS_TOKEN)   # type: ignore

    universe = load_intraday_universe()
    wait_until(SCAN_START)

    tg_send(
        f"🚀 <b>Scanner started</b>\n"
        f"Universe: {len(universe)} | Interval: {CANDLE_INTERVAL_MIN}m\n"
        f"Auto-trade: {'ON ✅' if AUTO_TRADE_ENABLED else 'OFF (dry-run) 🧪'}\n"
        f"Strategies: OB Shorts + ORB + Gap-Fill\n"
        f"RR: 1:{RISK_REWARD_RATIO} | Max risk/trade: ₹{MAX_RISK_PER_TRADE}"
    )

    all_signals_today: list[pd.DataFrame] = []
    next_scan_at = now_ist()

    while market_open_now():
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

            minute_slot = (now.minute // CANDLE_INTERVAL_MIN + 1) * CANDLE_INTERVAL_MIN
            next_scan_at = now.replace(second=5, microsecond=0) + timedelta(
                minutes=minute_slot - now.minute)

        try:
            monitor_oco(dhan)
        except Exception as e:
            log.debug(f"monitor_oco error: {e}")

        time.sleep(POSITION_POLL_SEC)

    # EOD summary
    if all_signals_today:
        combined = pd.concat(all_signals_today, ignore_index=True)
        fname = f"signals_{now_ist().strftime('%Y%m%d')}.csv"
        combined.to_csv(fname, index=False)
        log.info(f"Saved {len(combined)} signals -> {fname}")
        # Per-strategy breakdown
        if "strategy" in combined.columns:
            breakdown = combined["strategy"].value_counts().to_dict()
            bstr = " | ".join(f"{k}:{v}" for k, v in breakdown.items())
            tg_send(f"📈 <b>EOD</b>: {len(combined)} signals ({bstr}) | traded {len(_TRADED_TODAY)}")
        else:
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
