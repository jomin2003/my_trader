"""
=====================================================================
 FLASK WEB SERVICE for Render FREE TIER + external cron
---------------------------------------------------------------------
 Architecture:
   * Flask app exposes HTTP endpoints (Render free tier requires this)
   * External cron (cron-job.org) hits endpoints at market-timed intervals
   * /healthz pinged every 5 min => Render never spins down
   * Config persistence via GitHub Gist (ephemeral disk-safe)
   * Dhan token refresh on every cold start (no disk cache needed)

 Endpoints:
   GET  /                    - status page (human-readable)
   GET  /healthz             - keep-alive ping (returns 200 fast)
   POST /trigger/refresh     - refresh Dhan token
   POST /trigger/scan        - one scan+act pass (called every 5 min in mkt hrs)
   POST /trigger/oco         - poll open positions for OCO cleanup
   POST /trigger/download    - append today's bars (called after mkt close)
   POST /trigger/weekly      - full re-optimization (called Sunday)
   POST /trigger/promote     - promote latest sweep to live_config

 Auth: all /trigger/* endpoints require header X-Cron-Secret matching
       CRON_SECRET env var (protects against random internet hits).

 Environment variables (Render dashboard):
   DHAN_CLIENT_ID, DHAN_PIN, DHAN_TOTP_SECRET, DHAN_ACCESS_TOKEN
   TG_BOT_TOKEN, TG_CHAT_ID
   CRON_SECRET             (any random string; also set on cron-job.org)
   GITHUB_TOKEN            (personal access token with gist scope)
   GITHUB_GIST_ID          (id of your private gist; created once manually)
=====================================================================
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

# Ensure local pipeline modules are importable
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

# ---------------------------------------------------------------------
IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN  = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
SCAN_START   = dtime(9, 30)
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

# ---------------------------------------------------------------------
# In-memory state (resets on cold start; that's OK)
# ---------------------------------------------------------------------
STATE = {
    "boot_time_ist":     datetime.now(IST).isoformat(),
    "last_scan":         None,
    "last_download":     None,
    "last_token_refresh": None,
    "scans_today":       0,
    "signals_today":     0,
    "orders_today":      0,
    "errors_last_10":    [],
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


# ---------------------------------------------------------------------
# AUTH DECORATOR
# ---------------------------------------------------------------------
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


# ---------------------------------------------------------------------
# BOOT: restore config from Gist + refresh token
# ---------------------------------------------------------------------
def boot_restore():
    """Called once at process start. Never raises."""
    log.info("=" * 50)
    log.info(f"BOOT at {now_ist().isoformat()}")
    log.info("=" * 50)

    # 1. Restore live_config.json from GitHub Gist
    try:
        from gist_storage import restore_from_gist
        n = restore_from_gist(BASE_DIR)
        log.info(f"Restored {n} file(s) from Gist")
    except Exception as e:
        log.warning(f"Gist restore skipped: {e}")

    # 2. Refresh Dhan token
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


# ---------------------------------------------------------------------
# DHAN CLIENT (lazy singleton)
# ---------------------------------------------------------------------
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
    """Call after token refresh so client uses fresh token."""
    global _DHAN
    with _DHAN_LOCK:
        _DHAN = None


# ---------------------------------------------------------------------
# ENDPOINTS
# ---------------------------------------------------------------------
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
    """Ultra-fast keep-alive endpoint. External cron hits this every 5 min."""
    return "ok", 200


@app.route("/status")
def status():
    live_config_exists = (BASE_DIR / "live_config.json").exists()
    return jsonify({
        "time_ist":           now_ist().isoformat(),
        "in_market_hours":    MARKET_OPEN <= now_ist().time() <= MARKET_CLOSE,
        "in_entry_window":    SCAN_START <= now_ist().time() <= NO_ENTRY_AFTER,
        "live_config_active": live_config_exists,
        "state":              STATE,
    })


@app.route("/trigger/refresh", methods=["POST", "GET"])
@require_cron_secret
def trigger_refresh():
    try:
        from dhan_token_manager import ensure_valid_token
        tok = ensure_valid_token(force=request.args.get("force") == "1")
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
    """One scan+act pass. External cron hits this every 5 min during market hours."""
    try:
        t = now_ist().time()
        if not (MARKET_OPEN <= t <= MARKET_CLOSE):
            return jsonify({"skipped": "outside market hours"})

        import intraday_pattern_scanner_v2 as scn
        # Apply live config if present
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
            # rough order count: highest-score signals that would trade
            orders = int((ranked["score"] >= scn.MIN_SCORE_TO_TRADE).sum())
            STATE["orders_today"] += orders

        # Always poll OCO regardless
        try:
            scn.monitor_oco(dhan)
        except Exception as e:
            log.debug(f"OCO monitor issue: {e}")

        return jsonify({"ok": True, "signals": signals_count,
                        "scans_today": STATE["scans_today"]})
    except Exception as e:
        tb = traceback.format_exc()[-500:]
        _record_error(f"scan: {e}")
        tg_send(f"⚠️ Scan error: <code>{tb}</code>")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/trigger/oco", methods=["POST", "GET"])
@require_cron_secret
def trigger_oco():
    """OCO cleanup only (lighter than full scan)."""
    try:
        import intraday_pattern_scanner_v2 as scn
        scn.monitor_oco(get_dhan())
        return jsonify({"ok": True})
    except Exception as e:
        _record_error(f"oco: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/trigger/download", methods=["POST", "GET"])
@require_cron_secret
def trigger_download():
    """Append today's bars via Dhan. Runs after market close."""
    try:
        rc = _run_module("csv_downloader.py",
                         ["--mode", "dhan", "--preset", "nifty50",
                          "--out", str(DATA_DIR)],
                         timeout=600)
        STATE["last_download"] = now_ist().isoformat()
        # Push updated CSVs to Gist? Skipped - too much data. Rebuild on cold start.
        return jsonify({"ok": rc == 0, "rc": rc})
    except Exception as e:
        _record_error(f"download: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/trigger/weekly", methods=["POST", "GET"])
@require_cron_secret
def trigger_weekly():
    """
    Sunday weekly optimization.
    WARNING: On free tier (0.1 CPU, 512 MB) this may take 30+ min and can OOM.
    Consider running weekly opt on GitHub Actions instead — see README.
    """
    def _bg():
        try:
            tg_send("🔧 Weekly optimization started (Render free tier — slow)")
            for cmd in [
                ("csv_downloader.py",  ["--mode", "yfinance", "--preset", "nifty50",
                                        "--days", "59", "--out", str(DATA_DIR)], 1800),
                ("param_sweep.py",     ["--mode", "csv", "--csv-dir", str(DATA_DIR),
                                        "--nifty-csv", str(DATA_DIR / "NIFTY.csv"),
                                        "--preset", "quick",   # quick to fit RAM/CPU
                                        "--walk-forward", "0.7",
                                        "--out", str(SW_DIR)], 5400),
                ("backtest_harness.py",["--mode", "csv", "--csv-dir", str(DATA_DIR),
                                        "--nifty-csv", str(DATA_DIR / "NIFTY.csv"),
                                        "--out", str(BT_DIR)], 1800),
            ]:
                rc = _run_module(cmd[0], cmd[1], timeout=cmd[2])
                if rc != 0:
                    tg_send(f"⚠️ Weekly step failed: {cmd[0]} rc={rc}")
                    return
            # Auto-promote (assumes sweep did walk-forward validation)
            sweeps = sorted(SW_DIR.glob("sweep_*.csv"), key=lambda p: p.stat().st_mtime)
            if sweeps:
                _run_module("live_config.py", ["promote", "--sweep", str(sweeps[-1]),
                                                "--force"], timeout=60)
                # Push live_config to Gist for persistence
                try:
                    from gist_storage import backup_to_gist
                    backup_to_gist(BASE_DIR, files=["live_config.json"])
                    tg_send("✅ Weekly opt done. live_config promoted + backed up to Gist.")
                except Exception as e:
                    tg_send(f"✅ Weekly opt done but Gist backup failed: {e}")
        except Exception as e:
            tg_send(f"🚨 Weekly opt crashed: {e}")

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({"ok": True, "note": "started in background"})


@app.route("/trigger/promote", methods=["POST", "GET"])
@require_cron_secret
def trigger_promote():
    """Manually promote the latest sweep result to live config."""
    try:
        sweeps = sorted(SW_DIR.glob("sweep_*.csv"), key=lambda p: p.stat().st_mtime)
        if not sweeps:
            return jsonify({"ok": False, "error": "no sweep files"}), 404
        rc = _run_module("live_config.py",
                         ["promote", "--sweep", str(sweeps[-1]), "--force"],
                         timeout=60)
        if rc == 0:
            from gist_storage import backup_to_gist
            backup_to_gist(BASE_DIR, files=["live_config.json"])
        return jsonify({"ok": rc == 0})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------------------------------------------------
def _run_module(script: str, args: list[str], timeout: int) -> int:
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
        log.error(f"{script} TIMEOUT")
        return -1


# ---------------------------------------------------------------------
# BOOT
# ---------------------------------------------------------------------
boot_restore()


if __name__ == "__main__":
    # Local dev only. On Render, gunicorn is used (see Procfile).
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
