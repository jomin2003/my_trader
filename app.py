"""
FLASK WEB SERVICE for Render FREE TIER + external cron + TELEGRAM COMMANDS
=========================================================================
CHANGES vs previous:
  - NEW: crash/cold-start-safe STATE PERSISTENCE via state_store.py.
         boot_restore() restores today's open positions / halts / kill
         switch from the Gist; _scan_worker + _oco_worker snapshot state
         (throttled) so a Render cold start can't silently wipe safety.
  - /trigger/learn endpoint (EOD cron ~15:45 IST) -> ledger + allocator.
  - /report + /pnl Telegram commands -> live day's-PnL report.
  - /trigger/report endpoint (cron 15:40 IST) -> pushes the day's report.
  - /status shows Kronos summary + allocator line + last learn.
  - /resume clears the scanner's AUTO-halt (circuit breaker).
  - Day-boundary calls scn.reset_day().

Telegram commands:
  /status  -> live status (scans, token, market, open pos, halt, Kronos, allocator)
  /report  -> today's trades + suggestions + realised + floating P&L
  /pnl     -> alias of /report
  /health  -> quick alive check
  /stop    -> halt order placement (kill switch)
  /resume  -> re-enable order placement (also clears auto-halt)
  /help    -> list commands

Endpoints:
  GET  /                    - status page
  GET  /healthz             - keep-alive ping
  GET  /status              - JSON state dump
  POST /telegram/webhook    - Telegram command handler
  POST /trigger/refresh     - refresh Dhan token
  POST /trigger/scan        - fire-and-forget scan
  POST /trigger/oco         - fire-and-forget OCO check
  POST /trigger/download    - append bars + refresh OB data
  POST /trigger/promote     - promote latest sweep
  POST /trigger/report      - push today's P&L report to Telegram
  POST /trigger/learn       - append trades to ledger + run allocator
"""
from __future__ import annotations

import gc
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
    "last_report":        None,
    "last_learn":         None,
    "scans_today":        0,
    "signals_today":      0,
    "orders_today":       0,
    "scan_running":       False,
    "trading_halted":     False,   # manual /stop kill switch
    "errors_last_10":     [],
}

_UNIVERSE = None
_UNIVERSE_DATE = None
_SCAN_LOCK = threading.Lock()
_TG_RATE: dict[str, list[float]] = {}   # chat_id -> list of timestamps
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
            timeout=5)
    except Exception:
        pass

def tg_send_to(chat_id: str, text: str):
    "Send to a specific chat (used by webhook replies)."
    if not TG_BOT_TOKEN:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML"},
            timeout=5)
    except Exception:
        pass

def _tg_rate_ok(chat_id: str) -> bool:
    now = time.time()
    hits = [t for t in _TG_RATE.get(chat_id, []) if now - t < 60]
    hits.append(now)
    _TG_RATE[chat_id] = hits
    return len(hits) <= _TG_RATE_LIMIT

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

def boot_restore():
    log.info("=" * 50)
    log.info(f"BOOT at {now_ist().isoformat()}")
    log.info("=" * 50)
    # [PHASE1] config report — sanitized, secret-safe, log-only
    try:
        import config_report
        log.info("\n" + config_report.format_report(base_dir=BASE_DIR))
    except Exception as _e:
        log.warning(f"config report skipped: {_e}")
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
    # ---- crash/cold-start recovery: restore today's live state ----
    try:
        import intraday_pattern_scanner_v2 as scn
        import state_store
        info = state_store.restore_state(scn, STATE)
        if info.get("restored"):
            log.info(f"State restored: {info}")
            tg_send(f"♻️ State restored: {info['positions']} open position(s), "
                    f"halted={info.get('halted')}", silent=True)
        else:
            log.info(f"State restore skipped ({info.get('reason')})")
    except Exception as e:
        log.warning(f"state restore skipped: {e}")
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

def _snapshot_state(scn):
    "Throttled state snapshot to Gist (safe no-op if state_store unavailable)."
    try:
        import state_store
        state_store.save_state(scn, STATE)
    except Exception:
        pass

def _get_universe(scn):
    global _UNIVERSE, _UNIVERSE_DATE
    today = now_ist().date()
    if _UNIVERSE is None or _UNIVERSE_DATE != today:
        _UNIVERSE = scn.load_intraday_universe()
        _UNIVERSE_DATE = today
        log.info(f"Universe cached: {len(_UNIVERSE)} stocks for {today}")
        try:
            import multi_strategy_live
            multi_strategy_live.clear_cache()
            log.info("Day-boundary: strategy caches cleared")
        except Exception:
            pass
        try:
            scn.reset_day()   # clears signals, trades, positions, halts, cooldowns
        except Exception as e:
            log.warning(f"reset_day failed: {e}")
        # refresh Kronos forecast at the day boundary too
        try:
            import kronos_gate
            kronos_gate.reload_forecast()
        except Exception:
            pass
    return _UNIVERSE

def _run_module(script: str, args: list, timeout: int) -> int:
    log.info(f"Running: python {script} {' '.join(args)}")
    try:
        p = subprocess.run(
            [sys.executable, script, *args],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "PYTHONUNBUFFERED": "1"})
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
            if not STATE["trading_halted"]:
                scn.act_on_signals(dhan, ranked)
                STATE["orders_today"] += int((ranked["score"] >= scn.MIN_SCORE_TO_TRADE).sum())
            else:
                log.info("Trading halted (manual) — skipping order placement")
        try:
            scn.monitor_oco(dhan)
        except Exception as e:
            log.debug(f"OCO issue: {e}")
        STATE["last_scan_result"] = f"ok, {signals_count} signals"
        log.info(f"Scan complete: {signals_count} signals")
        _snapshot_state(scn)   # persist state after each scan (throttled)
    except Exception as e:
        _record_error(f"scan: {e}")
        STATE["last_scan_result"] = f"error: {str(e)[:100]}"
        tg_send(f"⚠️ Scan error: {str(e)[:200]}")
    finally:
        STATE["scan_running"] = False
        _SCAN_LOCK.release()
        gc.collect()   # release per-scan pandas/numpy arenas on the 512 MB tier

def _oco_worker():
    try:
        t = now_ist().time()
        if not (MARKET_OPEN <= t <= MARKET_CLOSE):
            return
        import intraday_pattern_scanner_v2 as scn
        scn.monitor_oco(get_dhan())
        _snapshot_state(scn)   # persist state after OCO management (throttled)
    except Exception as e:
        _record_error(f"oco: {e}")

def _report_worker():
    try:
        import intraday_pattern_scanner_v2 as scn
        report = scn.build_daily_report()
        tg_send(report)
        STATE["last_report"] = now_ist().isoformat()
        log.info("Daily report sent to Telegram")
    except Exception as e:
        _record_error(f"report: {e}")
        tg_send(f"⚠️ Report failed: {str(e)[:200]}")

def _learn_worker():
    "EOD: append today's trades to the Gist ledger + run the allocator."
    try:
        import intraday_pattern_scanner_v2 as scn
        import strategy_ledger
        import adaptive_allocator
        try:
            with scn._POS_LOCK:
                trades = list(scn._COMPLETED_TRADES)
        except Exception:
            trades = list(getattr(scn, "_COMPLETED_TRADES", []))
        ntrend = 0
        try:
            ntrend = scn.get_nifty_trend(get_dhan())
        except Exception:
            pass
        strategy_ledger.append_today(trades, nifty_trend=ntrend, base_dir=str(BASE_DIR))
        adaptive_allocator.run(base_dir=str(BASE_DIR))
        tg_send(adaptive_allocator.summary_text(base_dir=str(BASE_DIR)))
        STATE["last_learn"] = now_ist().isoformat()
        log.info(f"Learn step done: {len(trades)} trades ledgered")
    except Exception as e:
        _record_error(f"learn: {e}")
        tg_send(f"⚠️ Learn step failed: {str(e)[:200]}")

# =====================================================================
# TELEGRAM COMMAND HANDLER
# =====================================================================
def _build_status_text() -> str:
    "Human-readable status for Telegram /status command."
    t = now_ist()
    in_market = MARKET_OPEN <= t.time() <= MARKET_CLOSE
    in_entry  = SCAN_START <= t.time() <= NO_ENTRY_AFTER
    token_hours_left = None
    try:
        from dhan_token_manager import load_token
        tok = load_token()
        if tok:
            token_hours_left = round(tok.hours_left, 2)
    except Exception:
        pass
    n_open = 0
    auto_halt = False; halt_reason = ""
    try:
        import intraday_pattern_scanner_v2 as scn
        n_open = len(scn._OPEN_POSITIONS)
        auto_halt, halt_reason = scn.halt_status()
    except Exception:
        pass
    lines = [
        f"🤖 <b>BOT STATUS</b> — {t:%d %b %H:%M IST}",
        f"Market open: {'✅' if in_market else '❌'}",
        f"Entry window: {'✅' if in_entry else '❌'}",
        f"Manual halt: {'🛑 YES' if STATE['trading_halted'] else 'no'}",
        f"Auto halt: {'⛔ ' + halt_reason if auto_halt else 'no'}",
        f"Scan running: {'yes' if STATE['scan_running'] else 'no'}",
        f"Scans today: {STATE['scans_today']}",
        f"Signals today: {STATE['signals_today']}",
        f"Open positions: {n_open}",
        f"Token hours left: {token_hours_left}",
        f"Last scan: {STATE['last_scan']}",
        f"Last report: {STATE['last_report']}",
        f"Last learn: {STATE['last_learn']}",
    ]
    try:
        import kronos_gate
        lines.append(kronos_gate.kronos_summary())
    except Exception:
        pass
    try:
        import adaptive_allocator
        lines.append(adaptive_allocator.summary_text(str(BASE_DIR)).split("\n")[0])
    except Exception:
        pass
    if STATE["errors_last_10"]:
        lines.append(f"Recent errors: {len(STATE['errors_last_10'])}")
    return "\n".join(lines)

@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    "Handles /status, /report, /pnl, /health, /help, /stop, /resume."
    try:
        update = request.get_json(force=True, silent=True) or {}
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id", ""))
        text = (msg.get("text") or "").strip().lower()
        if not chat_id or not text:
            return jsonify({"ok": True})
        if TG_CHAT_ID and chat_id != str(TG_CHAT_ID):
            return jsonify({"ok": True})
        if not _tg_rate_ok(chat_id):
            return jsonify({"ok": True})

        cmd = text.split()[0].lstrip("/").split("@")[0]

        if cmd == "health":
            tg_send_to(chat_id, "✅ alive")
        elif cmd == "status":
            tg_send_to(chat_id, _build_status_text())
        elif cmd in ("report", "pnl"):
            try:
                import intraday_pattern_scanner_v2 as scn
                tg_send_to(chat_id, scn.build_daily_report())
            except Exception as e:
                tg_send_to(chat_id, f"⚠️ Report error: {str(e)[:200]}")
        elif cmd == "stop":
            STATE["trading_halted"] = True
            # persist the kill switch immediately so a cold start keeps it
            try:
                import intraday_pattern_scanner_v2 as scn
                import state_store
                state_store.save_state(scn, STATE, force=True)
            except Exception:
                pass
            tg_send_to(chat_id, "🛑 Trading HALTED. Scans continue; no new orders.")
        elif cmd == "resume":
            STATE["trading_halted"] = False
            try:
                import intraday_pattern_scanner_v2 as scn
                scn.resume_trading()   # also clears the circuit-breaker auto-halt
                import state_store
                state_store.save_state(scn, STATE, force=True)
            except Exception:
                pass
            tg_send_to(chat_id, "▶️ Trading RESUMED (manual + auto halt cleared).")
        elif cmd == "mlstatus":
            import model_diagnostics as _md; tg_send_to(chat_id, _md.mlstatus_text())
        elif cmd == "models":
            import model_diagnostics as _md; tg_send_to(chat_id, _md.models_text())
        elif cmd == "config":
            import model_diagnostics as _md; tg_send_to(chat_id, _md.config_text(str(BASE_DIR)))
        elif cmd == "decision":
            import model_diagnostics as _md
            _arg = text.split(maxsplit=1)
            _sym = _arg[1].strip().upper() if len(_arg) > 1 else ""
            tg_send_to(chat_id, _md.decision_text(_sym) if _sym else "Usage: /decision SYMBOL")
        elif cmd == "help":
            tg_send_to(chat_id,
                "Commands:\n"
                "/status — live status + halt + Kronos + allocator\n"
                "/report — today's P&L + trades + suggestions\n"
                "/mlstatus , /models, /config , /decision SYMBOL\n"
                "/pnl — same as /report\n"
                "/stop — halt order placement\n"
                "/resume — re-enable orders (clears auto-halt)\n"
                "/health — quick ping")
        else:
            tg_send_to(chat_id, "Unknown command. Try /help")
        return jsonify({"ok": True})
    except Exception as e:
        _record_error(f"webhook: {e}")
        return jsonify({"ok": False, "error": str(e)}), 200

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
    n_open = 0
    auto_halt = False; halt_reason = ""
    try:
        import intraday_pattern_scanner_v2 as scn
        n_open = len(scn._OPEN_POSITIONS)
        auto_halt, halt_reason = scn.halt_status()
    except Exception:
        pass
    kronos_info = None
    try:
        import kronos_gate
        kronos_info = kronos_gate.kronos_summary()
    except Exception:
        pass
    alloc_mode = None
    try:
        import adaptive_allocator
        alloc_mode = adaptive_allocator.ALLOC_MODE
    except Exception:
        pass
    return jsonify({
        "time_ist":           now_ist().isoformat(),
        "in_market_hours":    MARKET_OPEN <= now_ist().time() <= MARKET_CLOSE,
        "in_entry_window":    SCAN_START <= now_ist().time() <= NO_ENTRY_AFTER,
        "live_config_active": (BASE_DIR / "live_config.json").exists(),
        "ob_data_active":     (BASE_DIR / "ob_data.csv").exists(),
        "token_hours_left":   token_hours_left,
        "open_positions":     n_open,
        "manual_halt":        STATE["trading_halted"],
        "auto_halt":          auto_halt,
        "auto_halt_reason":   halt_reason,
        "kronos":             kronos_info,
        "allocator_mode":     alloc_mode,
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

@app.route("/trigger/report", methods=["POST", "GET"])
@require_cron_secret
def trigger_report():
    "Push today's P&L report to Telegram (cron at ~15:40 IST)."
    threading.Thread(target=_report_worker, daemon=True).start()
    return jsonify({"ok": True, "note": "report started in background"})

@app.route("/trigger/learn", methods=["POST", "GET"])
@require_cron_secret
def trigger_learn():
    "EOD (cron ~15:45 IST): ledger today's trades + run adaptive allocator."
    threading.Thread(target=_learn_worker, daemon=True).start()
    return jsonify({"ok": True, "note": "learn started in background"})

@app.route("/trigger/download", methods=["POST", "GET"])
@require_cron_secret
def trigger_download():
    def _dl():
        try:
            rc1 = _run_module("csv_downloader.py", [
                "--mode", "dhan", "--preset", "nifty50",
                "--out", str(DATA_DIR)], timeout=600)
            rc2 = _run_module("precompute_order_blocks.py", [
                "--csv-dir", str(DATA_DIR),
                "--out", str(BASE_DIR / "ob_data.csv")], timeout=300)
            STATE["last_download"] = now_ist().isoformat()
            if rc1 != 0 or rc2 != 0:
                tg_send(f"⚠️ Data pipeline: dl_rc={rc1}, ob_rc={rc2}")
            else:
                tg_send("✅ Post-market data + OB zones refreshed", silent=True)
        except Exception as e:
            _record_error(f"download: {e}")
            tg_send(f"⚠️ Download crashed: {e}")
        finally:
            gc.collect()
    threading.Thread(target=_dl, daemon=True).start()
    return jsonify({"ok": True, "note": "download started in background"})

@app.route("/trigger/promote", methods=["POST", "GET"])
@require_cron_secret
def trigger_promote():
    try:
        sweeps = sorted(SW_DIR.glob("sweep_*.csv"), key=lambda p: p.stat().st_mtime)
        if not sweeps:
            return jsonify({"ok": False, "error": "no sweep files"}), 404
        rc = _run_module("live_config.py", [
            "promote", "--sweep", str(sweeps[-1]), "--force"], timeout=60)
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

