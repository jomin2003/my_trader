"""
FLASK WEB SERVICE for Render FREE TIER + external cron + TELEGRAM COMMANDS

CHANGES vs previous:
  * NEW: /telegram/webhook endpoint — lets you type /status, /health, /help
    directly in Telegram and get a live reply.
  * Background-thread scans (no cron timeout)
  * Universe cached per day
  * Overlap guard on scans

Telegram commands (type in your bot chat):
  /status  -> full live status (scans, token, market state, last signal)
  /health  -> quick alive check
  /help    -> list commands

Endpoints:
  GET  /                    - status page
  GET  /healthz             - keep-alive ping
  GET  /status              - JSON state dump
  POST /telegram/webhook    - Telegram command handler  (NEW)
  POST /trigger/refresh     - refresh Dhan token
  POST /trigger/scan        - fire-and-forget scan
  POST /trigger/oco         - fire-and-forget OCO check
  POST /trigger/download    - append bars + refresh OB data
  POST /trigger/promote     - promote latest sweep
"""
from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, time as dtime
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, request

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN    = dtime(9, 15)
MARKET_CLOSE   = dtime(15, 30)
SCAN_START     = dtime(9, 30)
NO_ENTRY_AFTER = dtime(14, 30)

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID", "")
CRON_SECRET  = os.getenv("CRON_SECRET", "")

DATA_DIR = BASE_DIR / "data"
SW_DIR   = BASE_DIR / "sweep_out"
BT_DIR   = BASE_DIR / "backtest_out"
for d in (DATA_DIR, SW_DIR, BT_DIR):
    d.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("app")

app = Flask(__name__)

STATE = {
    "boot_time_ist":      datetime.now(IST).isoformat(),
    "last_scan":          None,
    "last_scan_result":   None,
    "last_download":      None,
    "last_token_refresh": None,
    "scans_today":        0,
    "signals_today":      0,
    "orders_today":       0,
    "scan_running":       False,
    "errors_last_10":     [],
}

_UNIVERSE = None
_UNIVERSE_DATE = None
_SCAN_LOCK = threading.Lock()
_TG_RATE: dict[str, list[float]] = {}  # chat_id -> list of timestamps
_TG_RATE_LIMIT = 10  # max requests per 60 seconds


def now_ist() -> datetime:
    return datetime.now(IST)


def _record_error(msg: str):
    STATE["errors_last_10"].append({"t": now_ist().isoformat(), "msg": msg[:400]})
    STATE["errors_last_10"] = STATE["errors_last_10"][-10:]


def tg_send(text: str, silent: bool = False):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": text[:4000],
                  "parse_mode": "HTML", "disable_notification": silent},
            timeout=5,
        )
    except Exception:
        pass


def tg_send_to(chat_id: str, text: str):
    """Send to a specific chat (used by webhook replies)."""
    if not TG_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML"},
            timeout=5,
        )
    except Exception:
        pass


def require_cron_secret(f):
    @wraps(f)
    def _wrap(*a, **kw):
        if not CRON_SECRET:
            return jsonify({"error": "CRON_SECRET not configured"}), 500
        supplied = request.headers.get("X-Cron-Secret") or request.args.get("secret")
        if supplied != CRON_SECRET:
            return jsonify({"error": "unauthorized"}), 401
        return f(*a, **kw)
    return _wrap


_TRADING_HALTED = False


def boot_restore():
    log.info("=" * 50)
    log.info(f"BOOT at {now_ist().isoformat()}")
    log.info("=" * 50)
    try:
        from gist_storage import restore_from_gist
        n = restore_from_gist(BASE_DIR)
        log.info(f"Restored {n} file(s) from Gist")
    except Exception as e:
        log.warning(f"Gist restore skipped: {e}")
    try:
        from dhan_token_manager import ensure_valid_token
        tok = ensure_valid_token(force=False)
        if tok:
            reset_dhan()
            STATE["last_token_refresh"] = now_ist().isoformat()
            log.info("Dhan token ready")
    except Exception as e:
        log.error(f"Token refresh on boot failed: {e}")
        _record_error(f"boot token: {e}")
    tg_send(f"🟢 App booted on Render at {now_ist():%H:%M IST}", silent=True)


_DHAN = None
_DHAN_LOCK = threading.Lock()


def get_dhan():
    global _DHAN
    with _DHAN_LOCK:
        if _DHAN is None:
            from dhanhq import DhanContext, dhanhq
            cid = os.getenv("DHAN_CLIENT_ID", "")
            tok = os.getenv("DHAN_ACCESS_TOKEN", "")
            _DHAN = dhanhq(DhanContext(cid, tok))
        return _DHAN


def reset_dhan():
    global _DHAN
    with _DHAN_LOCK:
        _DHAN = None


def _get_universe(scn):
    global _UNIVERSE, _UNIVERSE_DATE
    today = now_ist().date()
    if _UNIVERSE is None or _UNIVERSE_DATE != today:
        _UNIVERSE = scn.load_intraday_universe()
        _UNIVERSE_DATE = today
        log.info(f"Universe cached: {len(_UNIVERSE)} stocks for {today}")
        # Day-boundary: clear strategy caches and traded-today sets
        try:
            import multi_strategy_live
            multi_strategy_live.clear_cache()
            log.info("Day-boundary: strategy caches cleared")
        except Exception:
            pass
        scn._TRADED_TODAY.clear()
    return _UNIVERSE


def _run_module(script: str, args: list, timeout: int) -> int:
    log.info(f"Running: python {script} {' '.join(args)}")
    try:
        p = subprocess.run(
            [sys.executable, script, *args],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        if p.returncode != 0:
            log.error(f"{script} rc={p.returncode}\n{p.stderr[-800:]}")
        return p.returncode
    except subprocess.TimeoutExpired:
        log.error(f"{script} TIMEOUT after {timeout}s")
        return -1
    except Exception as e:
        log.error(f"{script} crashed: {e}")
        return -2


# =====================================================================
# BACKGROUND WORKERS
# =====================================================================
def _scan_worker():
    if not _SCAN_LOCK.acquire(blocking=False):
        log.info("Scan already running, skip")
        return
    try:
        STATE["scan_running"] = True
        t = now_ist().time()
        if not (MARKET_OPEN <= t <= MARKET_CLOSE):
            STATE["last_scan_result"] = "skipped (outside market hours)"
            return
        import intraday_pattern_scanner_v2 as scn
        try:
            from live_config import apply_live_config
            apply_live_config(module_name="intraday_pattern_scanner_v2", tg_sender=tg_send)
        except Exception as e:
            log.debug(f"live_config skipped: {e}")
        dhan = get_dhan()
        universe = _get_universe(scn)
        ranked = scn.scan_once(dhan, universe)
        STATE["scans_today"] += 1
        STATE["last_scan"] = now_ist().isoformat()
        signals_count = len(ranked) if ranked is not None and not ranked.empty else 0
        STATE["signals_today"] += signals_count
        if ranked is not None and not ranked.empty and SCAN_START <= t <= NO_ENTRY_AFTER:
            if not _TRADING_HALTED:
                scn.act_on_signals(dhan, ranked)
                STATE["orders_today"] += int((ranked["score"] >= scn.MIN_SCORE_TO_TRADE).sum())
            else:
                log.info("Trading halted — skipping order placement")
        try:
            scn.monitor_oco(dhan)
        except Exception as e:
            log.debug(f"OCO issue: {e}")
        STATE["last_scan_result"] = f"ok, {signals_count} signals"
        log.info(f"Scan complete: {signals_count} signals")
    except Exception as e:
        _record_error(f"scan: {e}")
        STATE["last_scan_result"] = f"error: {str(e)[:100]}"
        tg_send(f"⚠️ Scan error: <code>{str(e)[:200]}</code>")
    finally:
        STATE["scan_running"] = False
        _SCAN_LOCK.release()


def _oco_worker():
    try:
        t = now_ist().time()
        if not (MARKET_OPEN <= t <= MARKET_CLOSE):
            return
        import intraday_pattern_scanner_v2 as scn
        scn.monitor_oco(get_dhan())
    except Exception as e:
        _record_error(f"oco: {e}")


# =====================================================================
# TELEGRAM COMMAND HANDLER (NEW)
# =====================================================================
def _build_status_text() -> str:
    """Human-readable status for Telegram /status command."""
    t = now_ist()
    in_market = MARKET_OPEN <= t.time() <= MARKET_CLOSE
    in_entry  = SCAN_START <= t.time() <= NO_ENTRY_AFTER

    # Token
    token_line = "unknown"
    try:
        from dhan_token_manager import load_token
        tok = load_token()
        if tok:
            token_line = f"{tok.hours_left:.1f}h left"
    except Exception:
        pass

    live_cfg = "active" if (BASE_DIR / "live_config.json").exists() else "hardcoded defaults"
    ob_data  = "✅" if (BASE_DIR / "ob_data.csv").exists() else "❌ MISSING"

    boot = STATE.get("boot_time_ist", "?")[:19].replace("T", " ")
    last_scan = STATE.get("last_scan")
    last_scan = last_scan[11:16] if last_scan else "none yet"
    errors = STATE.get("errors_last_10", [])

    lines = [
        f"📊 <b>BOT STATUS</b> — {t:%H:%M:%S IST}",
        f"",
        f"🕐 Market open:    {'YES ✅' if in_market else 'NO (closed)'}",
        f"🎯 Entry window:   {'YES ✅' if in_entry else 'NO'}",
        f"🛑 Trading halted: {'YES ⛔' if _TRADING_HALTED else 'NO ✅'}",
        f"🔑 Dhan token:     {token_line}",
        f"⚙️  Live config:    {live_cfg}",
        f"📁 OB data:        {ob_data}",
        f"",
        f"🔄 Scans today:    {STATE.get('scans_today', 0)}",
        f"📶 Signals today:  {STATE.get('signals_today', 0)}",
        f"🎯 Orders today:   {STATE.get('orders_today', 0)}",
        f"⏱  Last scan:      {last_scan}",
        f"📋 Last result:    {STATE.get('last_scan_result', 'none')}",
        f"🏃 Scan running:   {'yes' if STATE.get('scan_running') else 'no'}",
        f"",
        f"🚀 Booted:         {boot}",
        f"⚠️  Recent errors:  {len(errors)}",
    ]
    if errors:
        last_err = errors[-1]
        lines.append(f"   Latest: {last_err.get('msg', '')[:120]}")
    return "\n".join(lines)


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """Handles /status, /health, /help, /stop, /resume typed in Telegram."""
    global _TRADING_HALTED
    try:
        update = request.get_json(force=True, silent=True) or {}
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        text = (msg.get("text") or "").strip().lower()

        # Only respond to the configured owner chat (security)
        if TG_CHAT_ID and chat_id != str(TG_CHAT_ID):
            return "ok", 200

        # Rate limiting
        now = time.time()
        history = _TG_RATE.get(chat_id, [])
        history = [t for t in history if now - t < 60]
        if len(history) >= _TG_RATE_LIMIT:
            return "ok", 200
        history.append(now)
        _TG_RATE[chat_id] = history

        if text.startswith("/status"):
            tg_send_to(chat_id, _build_status_text())
        elif text.startswith("/health"):
            tg_send_to(chat_id, "✅ Bot is alive and responding.")
        elif text.startswith("/stop"):
            _TRADING_HALTED = True
            tg_send_to(chat_id, "🛑 Trading HALTED. No new orders will be placed. Use /resume to restart.")
            log.warning("Trading halted via Telegram /stop command")
        elif text.startswith("/resume"):
            _TRADING_HALTED = False
            tg_send_to(chat_id, "▶️ Trading RESUMED. Orders will be placed again.")
            log.info("Trading resumed via Telegram /resume command")
        elif text.startswith("/help"):
            tg_send_to(chat_id,
                       "<b>Commands</b>\n"
                       "/status — full live status\n"
                       "/health — quick alive check\n"
                       "/stop — halt all new orders\n"
                       "/resume — resume trading\n"
                       "/help — this message")
        # ignore everything else silently
        return "ok", 200
    except Exception as e:
        log.debug(f"webhook err: {e}")
        return "ok", 200


# =====================================================================
# HTTP ENDPOINTS
# =====================================================================
@app.route("/")
def index():
    return jsonify({
        "service":   "intraday_pattern_scanner",
        "status":    "running",
        "time_ist":  now_ist().isoformat(),
        "in_market": MARKET_OPEN <= now_ist().time() <= MARKET_CLOSE,
        "state":     STATE,
    })


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/status")
def status():
    token_hours_left = None
    try:
        from dhan_token_manager import load_token
        tok = load_token()
        if tok:
            token_hours_left = round(tok.hours_left, 2)
    except Exception:
        pass
    return jsonify({
        "time_ist":           now_ist().isoformat(),
        "in_market_hours":    MARKET_OPEN <= now_ist().time() <= MARKET_CLOSE,
        "in_entry_window":    SCAN_START <= now_ist().time() <= NO_ENTRY_AFTER,
        "live_config_active": (BASE_DIR / "live_config.json").exists(),
        "ob_data_active":     (BASE_DIR / "ob_data.csv").exists(),
        "token_hours_left":   token_hours_left,
        "state":              STATE,
    })


@app.route("/trigger/refresh", methods=["POST", "GET"])
@require_cron_secret
def trigger_refresh():
    try:
        from dhan_token_manager import ensure_valid_token
        force = request.args.get("force") == "1"
        tok = ensure_valid_token(force=force)
        reset_dhan()
        STATE["last_token_refresh"] = now_ist().isoformat()
        return jsonify({"ok": True, "token_prefix": tok[:10] + "..." if tok else None})
    except Exception as e:
        _record_error(f"refresh: {e}")
        tg_send(f"⚠️ Token refresh FAILED: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/trigger/scan", methods=["POST", "GET"])
@require_cron_secret
def trigger_scan():
    t = now_ist().time()
    if not (MARKET_OPEN <= t <= MARKET_CLOSE):
        return jsonify({"ok": True, "skipped": "outside market hours"})
    if STATE["scan_running"]:
        return jsonify({"ok": True, "note": "scan already running"})
    threading.Thread(target=_scan_worker, daemon=True).start()
    return jsonify({"ok": True, "note": "scan started in background"})


@app.route("/trigger/oco", methods=["POST", "GET"])
@require_cron_secret
def trigger_oco():
    t = now_ist().time()
    if not (MARKET_OPEN <= t <= MARKET_CLOSE):
        return jsonify({"ok": True, "skipped": "outside market hours"})
    threading.Thread(target=_oco_worker, daemon=True).start()
    return jsonify({"ok": True, "note": "oco check started"})


@app.route("/trigger/download", methods=["POST", "GET"])
@require_cron_secret
def trigger_download():
    def _dl():
        try:
            rc1 = _run_module("csv_downloader.py",
                              ["--mode", "dhan", "--preset", "nifty50",
                               "--out", str(DATA_DIR)], timeout=600)
            rc2 = _run_module("precompute_order_blocks.py",
                              ["--csv-dir", str(DATA_DIR),
                               "--out", str(BASE_DIR / "ob_data.csv")], timeout=300)
            STATE["last_download"] = now_ist().isoformat()
            if rc1 != 0 or rc2 != 0:
                tg_send(f"⚠️ Data pipeline: dl_rc={rc1}, ob_rc={rc2}")
            else:
                tg_send("✅ Post-market data + OB zones refreshed", silent=True)
        except Exception as e:
            _record_error(f"download: {e}")
            tg_send(f"⚠️ Download crashed: {e}")
    threading.Thread(target=_dl, daemon=True).start()
    return jsonify({"ok": True, "note": "download started in background"})


@app.route("/trigger/promote", methods=["POST", "GET"])
@require_cron_secret
def trigger_promote():
    try:
        sweeps = sorted(SW_DIR.glob("sweep_*.csv"), key=lambda p: p.stat().st_mtime)
        if not sweeps:
            return jsonify({"ok": False, "error": "no sweep files"}), 404
        rc = _run_module("live_config.py",
                         ["promote", "--sweep", str(sweeps[-1]), "--force"], timeout=60)
        if rc == 0:
            try:
                from gist_storage import backup_to_gist
                backup_to_gist(BASE_DIR, files=["live_config.json"])
            except Exception as e:
                log.warning(f"Gist backup failed: {e}")
        return jsonify({"ok": rc == 0, "promoted_from": sweeps[-1].name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


boot_restore()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
