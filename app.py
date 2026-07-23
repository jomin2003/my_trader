"""
FLASK WEB SERVICE for Render FREE TIER + external cron (FIXED)

Endpoints:
  GET  /                    - status page
  GET  /healthz             - keep-alive ping
  GET  /status              - JSON state dump
  POST /trigger/refresh     - refresh Dhan token
  POST /trigger/scan        - one scan+act pass (every 5 min in mkt hrs)
  POST /trigger/oco         - poll open positions
  POST /trigger/download    - append today's bars + refresh OB data
  POST /trigger/promote     - manual promote of latest sweep

Auth: /trigger/* endpoints require X-Cron-Secret header or ?secret= query param
matching CRON_SECRET env var.

Environment variables (Render dashboard):
  DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET, DHAN_ACCESS_TOKEN
  TG_BOT_TOKEN, TG_CHAT_ID
  CRON_SECRET, GITHUB_TOKEN, GITHUB_GIST_ID
"""
from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import threading
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
    "last_download":      None,
    "last_token_refresh": None,
    "scans_today":        0,
    "signals_today":      0,
    "orders_today":       0,
    "errors_last_10":     [],
}


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
    """Called once at process start. Never raises."""
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


def _run_module(script: str, args: list, timeout: int) -> int:
    """Run a python script as subprocess. Returns exit code."""
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
# ENDPOINTS
# =====================================================================
@app.route("/")
def index():
    return jsonify({
        "service":       "intraday_pattern_scanner",
        "status":        "running",
        "time_ist":      now_ist().isoformat(),
        "boot_time":     STATE["boot_time_ist"],
        "in_market":     MARKET_OPEN <= now_ist().time() <= MARKET_CLOSE,
        "state":         STATE,
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
    """Refresh Dhan token via TOTP."""
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
    """One scan+act pass. Cron every 5 min in market hours."""
    try:
        t = now_ist().time()
        if not (MARKET_OPEN <= t <= MARKET_CLOSE):
            return jsonify({"skipped": "outside market hours", "time_ist": t.isoformat()})

        import intraday_pattern_scanner_v2 as scn

        try:
            from live_config import apply_live_config
            apply_live_config(module_name="intraday_pattern_scanner_v2", tg_sender=tg_send)
        except Exception as e:
            log.debug(f"live_config apply skipped: {e}")

        dhan = get_dhan()
        universe = scn.load_intraday_universe()

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

        return jsonify({
            "ok": True,
            "signals": signals_count,
            "scans_today": STATE["scans_today"],
            "time_ist": now_ist().isoformat(),
        })
    except Exception as e:
        tb = traceback.format_exc()[-500:]
        _record_error(f"scan: {e}")
        tg_send(f"⚠️ Scan error: <code>{str(e)[:200]}</code>")
        return jsonify({"ok": False, "error": str(e), "traceback": tb}), 500


@app.route("/trigger/oco", methods=["POST", "GET"])
@require_cron_secret
def trigger_oco():
    """OCO cleanup only (lighter than full scan)."""
    try:
        t = now_ist().time()
        if not (MARKET_OPEN <= t <= MARKET_CLOSE):
            return jsonify({"skipped": "outside market hours"})

        import intraday_pattern_scanner_v2 as scn
        scn.monitor_oco(get_dhan())
        return jsonify({"ok": True, "time_ist": now_ist().isoformat()})
    except Exception as e:
        _record_error(f"oco: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/trigger/download", methods=["POST", "GET"])
@require_cron_secret
def trigger_download():
    """Append today's bars + refresh OB zones. Runs after market close."""
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

        overall_ok = (rc1 == 0 and rc2 == 0)
        result = {
            "ok": overall_ok,
            "download_rc": rc1,
            "ob_precompute_rc": rc2,
            "time_ist": now_ist().isoformat(),
        }

        if not overall_ok:
            tg_send(f"⚠️ Post-market data pipeline failed: dl_rc={rc1}, ob_rc={rc2}")

        return jsonify(result)
    except Exception as e:
        _record_error(f"download: {e}")
        tg_send(f"⚠️ Download endpoint crashed: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/trigger/promote", methods=["POST", "GET"])
@require_cron_secret
def trigger_promote():
    """Manually promote latest sweep to live config + back up to Gist."""
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
