"""
FLASK WEB SERVICE for Render FREE TIER + external cron (FIXED v2)

CHANGES vs v1:
  * /trigger/scan now runs in a BACKGROUND THREAD, returns 200 instantly
    (fixes cron-job.org 30-sec timeout on slow Render CPU)
  * /trigger/oco also backgrounded
  * Universe is CACHED (not re-downloaded every scan)
  * Overlap guard: won't start a new scan if one is already running

Endpoints:
  GET  /                    - status page
  GET  /healthz             - keep-alive ping
  GET  /status              - JSON state dump
  POST /trigger/refresh     - refresh Dhan token
  POST /trigger/scan        - fire-and-forget scan (returns instantly)
  POST /trigger/oco         - fire-and-forget OCO check
  POST /trigger/download    - append today's bars + refresh OB data
  POST /trigger/promote     - manual promote of latest sweep

Auth: /trigger/* require X-Cron-Secret header or ?secret= query param.
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

# Cache the universe so we don't re-download instrument master every scan
_UNIVERSE = None
_UNIVERSE_DATE = None
_SCAN_LOCK = threading.Lock()


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


def require_cron_secret(f):
    @wraps(f)
    def _wrap(*a, **kw):
        if not CRON_SECRET:
            return jsonify({"error": "CRON_SECRET not configured on server"}), 500
        supplied = request.headers.get("X-Cron-Secret") or request.args.get("secret")
        if supplied != CRON_SECRET:
            return jsonify({"error": "unauthorized"}), 401
        return f(*a, **kw)
    return _wrap


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
    """Cache the universe per day so we don't re-download every scan."""
    global _UNIVERSE, _UNIVERSE_DATE
    today = now_ist().date()
    if _UNIVERSE is None or _UNIVERSE_DATE != today:
        _UNIVERSE = scn.load_intraday_universe()
        _UNIVERSE_DATE = today
        log.info(f"Universe cached: {len(_UNIVERSE)} stocks for {today}")
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
            log.error(f"{script} rc={p.returncode}\nSTDERR:\n{p.stderr[-800:]}")
        return p.returncode
    except subprocess.TimeoutExpired:
        log.error(f"{script} TIMEOUT after {timeout}s")
        return -1
    except Exception as e:
        log.error(f"{script} crashed: {e}")
        return -2


# =====================================================================
# BACKGROUND SCAN WORKER
# =====================================================================
def _scan_worker():
    """Runs the full scan in a background thread. Updates STATE."""
    if not _SCAN_LOCK.acquire(blocking=False):
        log.info("Scan already running, skipping this trigger")
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
            log.debug(f"live_config apply skipped: {e}")

        dhan = get_dhan()
        universe = _get_universe(scn)

        ranked = scn.scan_once(dhan, universe)
        STATE["scans_today"] += 1
        STATE["last_scan"] = now_ist().isoformat()

        signals_count = len(ranked) if ranked is not None and not ranked.empty else 0
        STATE["signals_today"] += signals_count

        if ranked is not None and not ranked.empty and SCAN_START <= t <= NO_ENTRY_AFTER:
            scn.act_on_signals(dhan, ranked)
            orders = int((ranked["score"] >= scn.MIN_SCORE_TO_TRADE).sum())
            STATE["orders_today"] += orders

        try:
            scn.monitor_oco(dhan)
        except Exception as e:
            log.debug(f"OCO monitor issue: {e}")

        STATE["last_scan_result"] = f"ok, {signals_count} signals"
        log.info(f"Scan complete: {signals_count} signals")

    except Exception as e:
        tb = traceback.format_exc()[-500:]
        _record_error(f"scan: {e}")
        STATE["last_scan_result"] = f"error: {str(e)[:100]}"
        tg_send(f"⚠️ Scan error: <code>{str(e)[:200]}</code>")
    finally:
        STATE["scan_running"] = False
        _SCAN_LOCK.release()


def _oco_worker():
    """Background OCO check."""
    try:
        t = now_ist().time()
        if not (MARKET_OPEN <= t <= MARKET_CLOSE):
            return
        import intraday_pattern_scanner_v2 as scn
        scn.monitor_oco(get_dhan())
    except Exception as e:
        _record_error(f"oco: {e}")


# =====================================================================
# ENDPOINTS
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
    live_config_exists = (BASE_DIR / "live_config.json").exists()
    ob_data_exists     = (BASE_DIR / "ob_data.csv").exists()

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
        "live_config_active": live_config_exists,
        "ob_data_active":     ob_data_exists,
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
        return jsonify({
            "ok": True,
            "token_prefix": tok[:10] + "..." if tok else None,
            "refreshed_at": STATE["last_token_refresh"],
        })
    except Exception as e:
        tb = traceback.format_exc()[-500:]
        _record_error(f"refresh: {e}")
        tg_send(f"⚠️ Token refresh FAILED: {e}")
        return jsonify({"ok": False, "error": str(e), "traceback": tb}), 500


@app.route("/trigger/scan", methods=["POST", "GET"])
@require_cron_secret
def trigger_scan():
    """
    Fire-and-forget: launches scan in background thread, returns instantly.
    This prevents cron-job.org timeouts on slow Render CPU.
    """
    t = now_ist().time()
    if not (MARKET_OPEN <= t <= MARKET_CLOSE):
        return jsonify({"ok": True, "skipped": "outside market hours",
                        "time_ist": t.isoformat()})

    if STATE["scan_running"]:
        return jsonify({"ok": True, "note": "scan already running, skipped"})

    threading.Thread(target=_scan_worker, daemon=True).start()
    return jsonify({"ok": True, "note": "scan started in background",
                    "time_ist": now_ist().isoformat()})


@app.route("/trigger/oco", methods=["POST", "GET"])
@require_cron_secret
def trigger_oco():
    """Fire-and-forget OCO check."""
    t = now_ist().time()
    if not (MARKET_OPEN <= t <= MARKET_CLOSE):
        return jsonify({"ok": True, "skipped": "outside market hours"})
    threading.Thread(target=_oco_worker, daemon=True).start()
    return jsonify({"ok": True, "note": "oco check started"})


@app.route("/trigger/download", methods=["POST", "GET"])
@require_cron_secret
def trigger_download():
    """
    Download today's bars + refresh OB zones. Runs in background thread
    because it takes 2-3 min (would timeout the cron otherwise).
    """
    def _dl_worker():
        try:
            rc1 = _run_module("csv_downloader.py",
                              ["--mode", "dhan", "--preset", "nifty50",
                               "--out", str(DATA_DIR)],
                              timeout=600)
            rc2 = _run_module("precompute_order_blocks.py",
                              ["--csv-dir", str(DATA_DIR),
                               "--out", str(BASE_DIR / "ob_data.csv")],
                              timeout=300)
            STATE["last_download"] = now_ist().isoformat()
            if rc1 != 0 or rc2 != 0:
                tg_send(f"⚠️ Data pipeline: dl_rc={rc1}, ob_rc={rc2}")
            else:
                tg_send("✅ Post-market data + OB zones refreshed", silent=True)
        except Exception as e:
            _record_error(f"download: {e}")
            tg_send(f"⚠️ Download crashed: {e}")

    threading.Thread(target=_dl_worker, daemon=True).start()
    return jsonify({"ok": True, "note": "download started in background"})


@app.route("/trigger/promote", methods=["POST", "GET"])
@require_cron_secret
def trigger_promote():
    try:
        sweeps = sorted(SW_DIR.glob("sweep_*.csv"), key=lambda p: p.stat().st_mtime)
        if not sweeps:
            return jsonify({"ok": False, "error": "no sweep files"}), 404
        rc = _run_module("live_config.py",
                         ["promote", "--sweep", str(sweeps[-1]), "--force"],
                         timeout=60)
        if rc == 0:
            try:
                from gist_storage import backup_to_gist
                backup_to_gist(BASE_DIR, files=["live_config.json"])
            except Exception as e:
                log.warning(f"Gist backup failed: {e}")
        return jsonify({"ok": rc == 0, "promoted_from": sweeps[-1].name})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =====================================================================
# BOOT
# =====================================================================
boot_restore()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
