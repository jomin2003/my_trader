"""
=====================================================================
 24/7 ORCHESTRATOR for the intraday_pattern_scanner_v2 pipeline
 (with Dhan token auto-refresh)
---------------------------------------------------------------------
 This is the main process on Render. It runs FOREVER (Background
 Worker) and cron-schedules all pipeline modules at the right times.

 Schedule (all times IST):
   * At boot        -> ensure_valid_token()  (auto-refresh if stale)
   * Daily  08:30   -> refresh Dhan token BEFORE market open
   * Mon-Fri 09:14  -> launch live scanner subprocess
   * Mon-Fri 15:25  -> force-kill lingering scanner
   * Mon-Fri 15:35  -> download today's bars (dhan mode)
   * Sun    19:00   -> full weekly re-optimization pipeline
   * Every 15 min   -> health check
   * Daily  18:00   -> Telegram summary

 Safety:
   * Every job wrapped in try/except -> Telegram alerts
   * Scanner subprocess is watchdog-restarted if it dies during hours
   * Scanner force-killed at 15:25 IST
   * Global exception handler + SIGTERM handling
   * State survives restarts via ./state.json

 Environment variables required (set on Render dashboard):
   DHAN_CLIENT_ID           your Dhan client id
   DHAN_PIN                 6-digit Dhan PIN
   DHAN_TOTP_SECRET         base32 secret from Dhan's TOTP QR
   DHAN_ACCESS_TOKEN        (optional bootstrap; will auto-refresh)
   TG_BOT_TOKEN             Telegram bot token
   TG_CHAT_ID               Telegram chat id
   DATA_DIR                 (default: ./data)
   REPO_DIR                 (default: /opt/render/project/src on Render)
=====================================================================
"""
from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

# =====================================================================
# CONFIG
# =====================================================================
IST = ZoneInfo("Asia/Kolkata")

REPO_DIR = Path(os.getenv("REPO_DIR", str(Path(__file__).parent))).resolve()
DATA_DIR = Path(os.getenv("DATA_DIR", str(REPO_DIR / "data"))).resolve()
BT_DIR   = REPO_DIR / "backtest_out"
SW_DIR   = REPO_DIR / "sweep_out"
MC_DIR   = REPO_DIR / "mc_out"
LOG_DIR  = REPO_DIR / "logs"
STATE_FP = REPO_DIR / "state.json"

for d in (DATA_DIR, BT_DIR, SW_DIR, MC_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID   = os.getenv("TG_CHAT_ID", "")

# Scheduler behavior
MARKET_OPEN  = dtime(9, 15)
SCAN_LAUNCH  = dtime(9, 14)
SCAN_KILL_AT = dtime(15, 25)
SCAN_RESTART_COOLDOWN_SEC = 60

# Token refresh
TOKEN_REFRESH_TIME = dtime(8, 30)   # BEFORE market open

# Monte Carlo pass criterion for automatic live_config promotion
MC_P_VALUE_THRESHOLD = 0.05

# =====================================================================
# LOGGING
# =====================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "scheduler.log", mode="a"),
    ],
)
log = logging.getLogger("orchestrator")


def now_ist() -> datetime:
    return datetime.now(IST)


# =====================================================================
# TELEGRAM
# =====================================================================
def tg_send(text: str, silent: bool = False) -> None:
    """Fire-and-forget Telegram send. Never blocks or raises."""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TG_CHAT_ID,
                "text": text[:4000],
                "parse_mode": "HTML",
                "disable_notification": silent,
            },
            timeout=6,
        )
    except Exception as e:
        log.debug(f"tg_send failed: {e}")


# =====================================================================
# STATE
# =====================================================================
def load_state() -> dict:
    if not STATE_FP.exists():
        return {}
    try:
        return json.loads(STATE_FP.read_text())
    except Exception:
        return {}


def save_state(st: dict) -> None:
    try:
        STATE_FP.write_text(json.dumps(st, indent=2, default=str))
    except Exception as e:
        log.warning(f"save_state failed: {e}")


def mark_job(name: str, status: str, extra: dict | None = None) -> None:
    st = load_state()
    st.setdefault("jobs", {})
    st["jobs"][name] = {
        "last_run":  now_ist().isoformat(),
        "status":    status,
        "extra":     extra or {},
    }
    save_state(st)


# =====================================================================
# COMMAND EXECUTION
# =====================================================================
def run_cmd(cmd: list[str], job: str, timeout_sec: int = 3600) -> tuple[int, str, str]:
    log.info(f"[{job}] running: {' '.join(cmd)}")
    try:
        p = subprocess.run(
            cmd, cwd=str(REPO_DIR),
            capture_output=True, text=True, timeout=timeout_sec,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        out_tail = (p.stdout or "")[-1500:]
        err_tail = (p.stderr or "")[-1500:]
        if p.returncode != 0:
            log.error(f"[{job}] rc={p.returncode}\nSTDOUT-TAIL:\n{out_tail}\nSTDERR-TAIL:\n{err_tail}")
        else:
            log.info(f"[{job}] OK (rc=0)")
        return p.returncode, out_tail, err_tail
    except subprocess.TimeoutExpired:
        log.error(f"[{job}] TIMEOUT after {timeout_sec}s")
        return -1, "", f"TIMEOUT after {timeout_sec}s"
    except Exception as e:
        log.exception(f"[{job}] crashed")
        return -2, "", str(e)


# =====================================================================
# DHAN TOKEN AUTO-REFRESH  (calls dhan_token_manager.py in-process)
# =====================================================================
def _refresh_dhan_token(force: bool = False) -> bool:
    """
    Call the token manager in-process so os.environ gets updated for
    ALL subsequent subprocesses (they inherit the parent env).
    Returns True on success, False on failure.
    """
    try:
        # Import lazily so a missing pyotp doesn't kill the scheduler on boot
        from dhan_token_manager import ensure_valid_token, load_token
        token = ensure_valid_token(force=force)
        st = load_token()
        if st:
            log.info(f"Dhan token valid, {st.hours_left:.1f}h left "
                     f"(expires {st.expires_at_ist})")
        return bool(token)
    except SystemExit as e:
        log.error(f"Token refresh config error: {e}")
        tg_send(f"🚨 <b>Dhan token refresh CONFIG error</b>\n{e}\n"
                f"Set DHAN_CLIENT_ID / DHAN_PIN / DHAN_TOTP_SECRET on Render.")
        return False
    except Exception as e:
        log.exception("Token refresh failed")
        tg_send(f"🚨 <b>Dhan token refresh FAILED</b>\n<code>{e}</code>\n"
                f"Trading may fail after current token expires.")
        return False


def job_refresh_token():
    """08:30 IST daily: refresh Dhan token before market open."""
    log.info("=" * 40)
    log.info("Daily token refresh")
    log.info("=" * 40)
    ok = _refresh_dhan_token(force=False)
    mark_job("refresh_token", "ok" if ok else "fail")
    if ok:
        tg_send("🔑 Dhan token refreshed for today", silent=True)


# =====================================================================
# LIVE SCANNER SUBPROCESS MANAGER
# =====================================================================
class ScannerProcess:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.started_at: datetime | None = None
        self.restart_count = 0
        self.lock = threading.Lock()

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        with self.lock:
            if self.is_alive():
                log.info("Scanner already running.")
                return
            log_fp = open(LOG_DIR / f"scanner_{now_ist():%Y%m%d}.log", "a")
            log_fp.write(f"\n===== SCANNER START {now_ist().isoformat()} =====\n")
            log_fp.flush()
            # IMPORTANT: env passed here captures the CURRENT DHAN_ACCESS_TOKEN
            # which the token refresh has already updated in os.environ.
            self.proc = subprocess.Popen(
                [sys.executable, "-u", "intraday_pattern_scanner_v2.py"],
                cwd=str(REPO_DIR),
                stdout=log_fp, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            self.started_at = now_ist()
            log.info(f"Scanner started (pid={self.proc.pid})")
            tg_send(f"🚀 Live scanner started (pid={self.proc.pid})", silent=True)

    def stop(self, reason: str = "shutdown"):
        with self.lock:
            if not self.is_alive():
                return
            log.info(f"Stopping scanner ({reason}) pid={self.proc.pid}")
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    log.warning("Scanner didn't terminate, sending SIGKILL")
                    self.proc.kill()
                    self.proc.wait(timeout=5)
            except Exception as e:
                log.warning(f"stop error: {e}")
            self.proc = None
            tg_send(f"🛑 Scanner stopped ({reason})", silent=True)

    def watchdog(self):
        t = now_ist().time()
        if not (MARKET_OPEN <= t <= SCAN_KILL_AT):
            return
        if self.is_alive():
            return
        if self.started_at and self.started_at.date() == now_ist().date():
            if (now_ist() - self.started_at).total_seconds() < SCAN_RESTART_COOLDOWN_SEC:
                return
            self.restart_count += 1
            log.warning(f"Scanner died — restart #{self.restart_count}")
            # If the scanner died, try refreshing token before restarting
            # (in case it was an auth failure)
            _refresh_dhan_token(force=False)
            tg_send(f"⚠️ Scanner died, restarting (#{self.restart_count})")
            self.start()

    def force_kill_after_hours(self):
        if self.is_alive():
            log.info("EOD force-kill of scanner")
            self.stop(reason="EOD force-kill")


SCANNER = ScannerProcess()


# =====================================================================
# JOBS
# =====================================================================
def job_launch_scanner():
    """09:14 IST Mon-Fri: launch the live scanner (after ensuring token is fresh)."""
    try:
        # Belt-and-braces: ensure token is fresh even if 08:30 job failed
        if not _refresh_dhan_token(force=False):
            log.error("Cannot launch scanner: token refresh failed")
            tg_send("🚨 <b>Scanner NOT started</b> — token refresh failed")
            mark_job("launch_scanner", "fail", {"error": "token_refresh_failed"})
            return
        SCANNER.start()
        mark_job("launch_scanner", "ok")
    except Exception as e:
        log.exception("launch_scanner failed")
        tg_send(f"🚨 launch_scanner FAILED: {e}")
        mark_job("launch_scanner", "fail", {"error": str(e)})


def job_eod_kill_scanner():
    try:
        SCANNER.force_kill_after_hours()
        mark_job("eod_kill", "ok")
    except Exception as e:
        log.exception("eod_kill failed")
        mark_job("eod_kill", "fail", {"error": str(e)})


def job_download_data_dhan():
    rc, out, err = run_cmd(
        [sys.executable, "csv_downloader.py",
         "--mode", "dhan", "--preset", "nifty50",
         "--out", str(DATA_DIR)],
        job="download_dhan", timeout_sec=900,
    )
    if rc == 0:
        mark_job("download_dhan", "ok")
    else:
        mark_job("download_dhan", "fail", {"stderr": err[-500:]})
        tg_send(f"⚠️ Dhan data download failed (rc={rc})\n<code>{err[-500:]}</code>")


def job_weekly_optimization():
    tg_send("🔧 <b>Weekly optimization started</b>", silent=True)

    # 1. Refresh 60 days of data via yfinance
    rc, _, err = run_cmd(
        [sys.executable, "csv_downloader.py",
         "--mode", "yfinance", "--preset", "nifty50",
         "--days", "59", "--out", str(DATA_DIR)],
        job="weekly.yfinance", timeout_sec=1800,
    )
    if rc != 0:
        tg_send(f"⚠️ Weekly: yfinance failed.\n<code>{err[-300:]}</code>")
        mark_job("weekly_optimization", "fail", {"step": "yfinance"})
        return

    # 2. Parameter sweep with walk-forward
    rc, _, err = run_cmd(
        [sys.executable, "param_sweep.py",
         "--mode", "csv", "--csv-dir", str(DATA_DIR),
         "--nifty-csv", str(DATA_DIR / "NIFTY.csv"),
         "--preset", "default", "--walk-forward", "0.7",
         "--out", str(SW_DIR)],
        job="weekly.sweep", timeout_sec=5400,
    )
    if rc != 0:
        tg_send(f"⚠️ Weekly: sweep failed.\n<code>{err[-300:]}</code>")
        mark_job("weekly_optimization", "fail", {"step": "sweep"})
        return

    # 3. Fresh backtest with current defaults
    rc, _, err = run_cmd(
        [sys.executable, "backtest_harness.py",
         "--mode", "csv", "--csv-dir", str(DATA_DIR),
         "--nifty-csv", str(DATA_DIR / "NIFTY.csv"),
         "--out", str(BT_DIR)],
        job="weekly.backtest", timeout_sec=1800,
    )
    if rc != 0:
        tg_send(f"⚠️ Weekly: backtest failed.\n<code>{err[-300:]}</code>")
        mark_job("weekly_optimization", "fail", {"step": "backtest"})
        return

    # 4. Monte Carlo on newest trades CSV
    trades_files = sorted(BT_DIR.glob("trades_*.csv"), key=lambda p: p.stat().st_mtime)
    if not trades_files:
        tg_send("⚠️ Weekly: no trades CSV. Skipping MC.")
        mark_job("weekly_optimization", "fail", {"step": "mc_no_trades"})
        return

    latest_trades = trades_files[-1]
    rc, out, err = run_cmd(
        [sys.executable, "monte_carlo.py",
         "--trades", str(latest_trades),
         "--csv-dir", str(DATA_DIR),
         "--nifty-csv", str(DATA_DIR / "NIFTY.csv"),
         "--n-boot", "2000", "--n-perms", "500",
         "--out", str(MC_DIR)],
        job="weekly.mc", timeout_sec=2400,
    )
    if rc != 0:
        tg_send(f"⚠️ Weekly: Monte Carlo failed.\n<code>{err[-300:]}</code>")
        mark_job("weekly_optimization", "fail", {"step": "mc"})
        return

    p_value = _parse_mc_p_value(out)
    log.info(f"Weekly MC expectancy_R p-value: {p_value}")

    sweep_files = sorted(SW_DIR.glob("sweep_*.csv"), key=lambda p: p.stat().st_mtime)
    latest_sweep = sweep_files[-1] if sweep_files else None

    if p_value is not None and p_value < MC_P_VALUE_THRESHOLD and latest_sweep:
        rc, _, err = run_cmd(
            [sys.executable, "live_config.py", "promote",
             "--sweep", str(latest_sweep), "--force"],
            job="weekly.promote", timeout_sec=60,
        )
        if rc == 0:
            tg_send(f"✅ <b>Weekly optimization DONE</b>\n"
                    f"MC p={p_value:.4f} — new config promoted from {latest_sweep.name}")
            mark_job("weekly_optimization", "ok",
                     {"p_value": p_value, "sweep": latest_sweep.name})
        else:
            tg_send(f"⚠️ Promote failed after MC passed.\n<code>{err[-300:]}</code>")
            mark_job("weekly_optimization", "fail", {"step": "promote"})
    else:
        tg_send(f"ℹ️ <b>Weekly optimization DONE</b>\n"
                f"MC p={p_value} — NOT promoting (need p < {MC_P_VALUE_THRESHOLD}). "
                f"Live config unchanged.")
        mark_job("weekly_optimization", "ok",
                 {"p_value": p_value, "promoted": False})


def _parse_mc_p_value(stdout: str) -> float | None:
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("expectancy_R") and ":" in line:
            try:
                val = line.split(":", 1)[1].split()[0]
                return float(val)
            except Exception:
                continue
    return None


def job_health_check():
    try:
        issues = []
        st = load_state()
        jobs = st.get("jobs", {})

        # Scanner alive during market hours
        t = now_ist().time()
        if now_ist().weekday() < 5 and MARKET_OPEN <= t <= SCAN_KILL_AT:
            if not SCANNER.is_alive():
                issues.append("Scanner NOT running during market hours")

        # Data downloader stale
        if now_ist().weekday() < 5 and t >= dtime(9, 0):
            last = jobs.get("download_dhan", {}).get("last_run")
            if last:
                age_h = (now_ist() - datetime.fromisoformat(last)).total_seconds() / 3600
                if age_h > 36:
                    issues.append(f"Dhan download stale ({age_h:.1f}h old)")

        # Weekly opt stale
        last_opt = jobs.get("weekly_optimization", {}).get("last_run")
        if last_opt:
            age_d = (now_ist() - datetime.fromisoformat(last_opt)).total_seconds() / 86400
            if age_d > 10:
                issues.append(f"Weekly optimization stale ({age_d:.1f}d old)")

        # Token freshness
        try:
            from dhan_token_manager import load_token
            tok = load_token()
            if tok and tok.hours_left < 2:
                issues.append(f"Dhan token expiring soon ({tok.hours_left:.1f}h left)")
        except Exception:
            pass

        # live_config check
        lc = REPO_DIR / "live_config.json"
        if lc.exists():
            try:
                cfg = json.loads(lc.read_text())
                promoted = datetime.fromisoformat(cfg["meta"]["promoted_at"])
                age_d = (now_ist() - promoted).total_seconds() / 86400
                if age_d > 30:
                    issues.append(f"live_config.json is {age_d:.0f}d old — scanner rejects it")
            except Exception:
                issues.append("live_config.json unparseable")

        SCANNER.watchdog()

        if issues:
            log.warning(f"Health issues: {issues}")
            prev = st.get("health_prev_issues", [])
            if any(i in prev for i in issues):
                tg_send("⚠️ <b>Health issues:</b>\n" + "\n".join(f"• {i}" for i in issues))
            st["health_prev_issues"] = issues
            save_state(st)
        else:
            st["health_prev_issues"] = []
            save_state(st)

        mark_job("health_check", "ok", {"issues": issues})
    except Exception as e:
        log.exception("health_check failed")
        mark_job("health_check", "fail", {"error": str(e)})


def job_daily_summary():
    try:
        st = load_state()
        jobs = st.get("jobs", {})
        today = now_ist().date()

        lines = [f"📊 <b>Daily summary — {today}</b>\n"]

        if now_ist().weekday() < 5:
            lc = REPO_DIR / "live_config.json"
            cfg_status = "active" if lc.exists() else "hardcoded defaults"
            lines.append(f"• Scanner: {cfg_status}, restarts today: {SCANNER.restart_count}")
        else:
            lines.append(f"• Scanner: market closed (weekend)")

        # Token status
        try:
            from dhan_token_manager import load_token
            tok = load_token()
            if tok:
                lines.append(f"• Token: {tok.hours_left:.1f}h left")
        except Exception:
            pass

        d = jobs.get("download_dhan", {})
        if d.get("last_run", "").startswith(str(today)):
            lines.append(f"• Data download: ✅ ({d.get('status')})")
        else:
            lines.append(f"• Data download: not run today")

        w = jobs.get("weekly_optimization", {})
        if w:
            when = w.get("last_run", "?")[:10]
            lines.append(f"• Last weekly opt: {when} ({w.get('status')})")

        issues = st.get("health_prev_issues", [])
        if issues:
            lines.append(f"\n⚠️ Open issues:")
            for i in issues:
                lines.append(f"   • {i}")
        else:
            lines.append(f"\n✅ No open issues")

        tg_send("\n".join(lines))
        mark_job("daily_summary", "ok")
    except Exception as e:
        log.exception("daily_summary failed")
        mark_job("daily_summary", "fail", {"error": str(e)})


# =====================================================================
# GLOBAL EXCEPTION SAFETY
# =====================================================================
def _global_excepthook(exc_type, exc_value, exc_tb):
    msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))[-1500:]
    log.critical(f"UNCAUGHT: {msg}")
    tg_send(f"🚨 <b>Unhandled exception</b>\n<code>{msg[-1200:]}</code>")


sys.excepthook = _global_excepthook


def _sigterm_handler(signum, frame):
    log.info(f"Received signal {signum} — shutting down cleanly")
    tg_send(f"⏸ Orchestrator SIGTERM — shutting down", silent=True)
    try:
        SCANNER.stop(reason="orchestrator shutdown")
    except Exception:
        pass
    sys.exit(0)


signal.signal(signal.SIGTERM, _sigterm_handler)
signal.signal(signal.SIGINT,  _sigterm_handler)


# =====================================================================
# MAIN
# =====================================================================
def main():
    log.info("=" * 60)
    log.info(f"Orchestrator boot at {now_ist().isoformat()}")
    log.info(f"REPO_DIR: {REPO_DIR}")
    log.info(f"DATA_DIR: {DATA_DIR}")
    log.info("=" * 60)

    tg_send(
        f"🟢 <b>Orchestrator ONLINE</b>\n"
        f"Time: {now_ist():%Y-%m-%d %H:%M:%S IST}\n"
        f"Repo: {REPO_DIR.name}"
    )

    # ---- BOOT-TIME TOKEN REFRESH ----
    log.info("Boot-time Dhan token check")
    if not _refresh_dhan_token(force=False):
        log.error("BOOT: token refresh failed — trading will fail until fixed")
        tg_send("🚨 <b>BOOT WARNING</b>: Dhan token could not be refreshed. "
                "Trading will fail. Check DHAN_CLIENT_ID / DHAN_PIN / "
                "DHAN_TOTP_SECRET on Render.")

    sched = BackgroundScheduler(timezone=IST)

    # NEW: Daily 08:30 IST Dhan token refresh (BEFORE any 09:14 scanner launch)
    sched.add_job(job_refresh_token,
                  CronTrigger(hour=8, minute=30, timezone=IST),
                  id="refresh_token", max_instances=1, coalesce=True)

    sched.add_job(job_launch_scanner,
                  CronTrigger(day_of_week="mon-fri", hour=9, minute=14, timezone=IST),
                  id="launch_scanner", max_instances=1, coalesce=True)

    sched.add_job(job_eod_kill_scanner,
                  CronTrigger(day_of_week="mon-fri", hour=15, minute=25, timezone=IST),
                  id="eod_kill", max_instances=1, coalesce=True)

    sched.add_job(job_download_data_dhan,
                  CronTrigger(day_of_week="mon-fri", hour=15, minute=35, timezone=IST),
                  id="download_dhan", max_instances=1, coalesce=True)

    sched.add_job(job_weekly_optimization,
                  CronTrigger(day_of_week="sun", hour=19, minute=0, timezone=IST),
                  id="weekly_optimization", max_instances=1, coalesce=True)

    sched.add_job(job_health_check,
                  IntervalTrigger(minutes=15),
                  id="health_check", max_instances=1, coalesce=True)

    sched.add_job(job_daily_summary,
                  CronTrigger(hour=18, minute=0, timezone=IST),
                  id="daily_summary", max_instances=1, coalesce=True)

    sched.start()
    log.info("Scheduler started. Jobs registered:")
    for j in sched.get_jobs():
        log.info(f"  {j.id}: next run = {j.next_run_time}")

    # If we booted mid-session, launch immediately
    t = now_ist().time()
    if now_ist().weekday() < 5 and MARKET_OPEN <= t <= SCAN_KILL_AT:
        log.info("Started during market hours — launching scanner NOW")
        job_launch_scanner()

    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        log.info("Shutting down scheduler")
        sched.shutdown(wait=False)
        SCANNER.stop(reason="main loop exit")


if __name__ == "__main__":
    main()
