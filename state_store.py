"""
=====================================================================
STATE STORE — crash/cold-start-safe persistence for critical runtime state
=====================================================================
THE PROBLEM
-----------
Render's free tier is EPHEMERAL. A cold start (missed keepalive, instance
recycle, redeploy) wipes all in-memory state. Today that silently loses:
  * _OPEN_POSITIONS      -> open trades forgotten (orphaned SL/TGT if live)
  * _TRADING_HALTED_TODAY -> circuit breaker resets (bot resumes a bad day)
  * manual /stop flag     -> kill switch flips back ON
  * _TRADED_TODAY / caps  -> dedup + daily trade cap reset (re-entries)
  * _CONSECUTIVE_LOSSES   -> loss-streak halt resets

THE FIX
-------
Snapshot these to a GitHub Gist (same mechanism as live_config.json) on a
throttled cadence, and RESTORE them on boot. Small JSON, safe, idempotent.

SAFE BY DESIGN
  * Only restores state saved EARLIER TODAY (IST). Yesterday's state is
    ignored so a stale snapshot never revives dead positions.
  * All fields optional; missing keys just skip. Never raises to caller.
  * Throttled writes (default every 60s) so we don't hammer the Gist API.

Env:
  GITHUB_TOKEN     PAT with Gists: read/write   (reuse your existing one)
  STATE_GIST_ID    gist id for state.json (falls back to GITHUB_GIST_ID)
  STATE_WRITE_SEC  min seconds between Gist writes (default 60)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("state_store")

GIST_API = "https://api.github.com/gists"
STATE_FILE = "state.json"
WRITE_SEC = int(os.getenv("STATE_WRITE_SEC", "60"))

_LAST_WRITE = 0.0
_WRITE_LOCK = threading.Lock()


def _gid() -> str:
    return (os.getenv("STATE_GIST_ID", "").strip()
            or os.getenv("GITHUB_GIST_ID", "").strip())

def _headers() -> dict:
    tok = os.getenv("GITHUB_TOKEN", "").strip()
    h = {"Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h

def _today() -> str:
    return datetime.now(IST).date().isoformat()

# ---------------------------------------------------------------------
# SERIALISATION — positions hold a datetime (opened_at); make JSON-safe
# ---------------------------------------------------------------------
def _pos_to_json(pos: dict) -> dict:
    d = dict(pos)
    oa = d.get("opened_at")
    if hasattr(oa, "isoformat"):
        d["opened_at"] = oa.isoformat()
    return d

def _pos_from_json(d: dict) -> dict:
    d = dict(d)
    oa = d.get("opened_at")
    if isinstance(oa, str):
        try:
            d["opened_at"] = datetime.fromisoformat(oa)
        except Exception:
            d["opened_at"] = datetime.now(IST)
    return d

# ---------------------------------------------------------------------
# SNAPSHOT (scanner state -> Gist)
# ---------------------------------------------------------------------
def build_snapshot(scn, app_state: dict | None = None) -> dict:
    """Collect the critical fields from the scanner module + app STATE."""
    snap = {"date": _today(), "saved_at": datetime.now(IST).isoformat()}
    try:
        with scn._POS_LOCK:
            snap["open_positions"] = {k: _pos_to_json(v)
                                       for k, v in scn._OPEN_POSITIONS.items()}
            snap["traded_today"]   = sorted(list(scn._TRADED_TODAY))
            snap["positions_opened"] = int(scn._POSITIONS_OPENED)
            snap["halted_today"]   = bool(scn._TRADING_HALTED_TODAY)
            snap["halt_reason"]    = scn._HALT_REASON
            snap["consecutive_losses"] = int(scn._CONSECUTIVE_LOSSES)
            snap["symbol_cooldown"] = {k: v.isoformat()
                                        for k, v in scn._SYMBOL_COOLDOWN.items()}
            # completed trades are also in the ledger, but keep a same-day copy
            snap["completed_trades"] = list(scn._COMPLETED_TRADES)
            snap["signals_today"]    = list(scn._SIGNALS_TODAY)
    except Exception as e:
        log.warning(f"snapshot collect failed: {e}")
    if app_state is not None:
        snap["manual_halt"] = bool(app_state.get("trading_halted", False))
    return snap

def save_state(scn, app_state: dict | None = None, force: bool = False) -> bool:
    """Throttled write of the snapshot to the Gist. Returns True if written."""
    global _LAST_WRITE
    now = time.time()
    if not force and (now - _LAST_WRITE) < WRITE_SEC:
        return False
    if not _WRITE_LOCK.acquire(blocking=False):
        return False
    try:
        snap = build_snapshot(scn, app_state)
        payload = json.dumps(snap, default=str)
        gid = _gid(); tok = os.getenv("GITHUB_TOKEN", "").strip()
        # local copy always
        try:
            Path(STATE_FILE).write_text(payload)
        except Exception:
            pass
        if not gid or not tok:
            _LAST_WRITE = now
            return False
        r = requests.patch(f"{GIST_API}/{gid}", headers=_headers(),
                           json={"files": {STATE_FILE: {"content": payload}}},
                           timeout=15)
        _LAST_WRITE = now
        if r.status_code != 200:
            log.warning(f"state gist push HTTP {r.status_code}: {r.text[:120]}")
            return False
        return True
    except Exception as e:
        log.warning(f"save_state error: {e}")
        return False
    finally:
        try:
            _WRITE_LOCK.release()
        except Exception:
            pass

# ---------------------------------------------------------------------
# RESTORE (Gist -> scanner state) — only if saved EARLIER TODAY
# ---------------------------------------------------------------------
def _fetch_snapshot() -> dict | None:
    gid = _gid(); tok = os.getenv("GITHUB_TOKEN", "").strip()
    if not gid or not tok:
        # try local fallback
        p = Path(STATE_FILE)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except Exception:
                return None
        return None
    try:
        r = requests.get(f"{GIST_API}/{gid}", headers=_headers(), timeout=10)
        if r.status_code != 200:
            return None
        node = r.json().get("files", {}).get(STATE_FILE)
        if not node:
            return None
        content = node.get("content")
        if content is None and node.get("raw_url"):
            content = requests.get(node["raw_url"], headers=_headers(), timeout=10).text
        return json.loads(content) if content else None
    except Exception as e:
        log.warning(f"state fetch error: {e}")
        return None

def restore_state(scn, app_state: dict | None = None) -> dict:
    """Restore critical state on boot — ONLY if the snapshot is from TODAY.
       Returns a small dict describing what was restored (for logging/alert)."""
    info = {"restored": False, "reason": "", "positions": 0}
    snap = _fetch_snapshot()
    if not snap:
        info["reason"] = "no snapshot"
        return info
    if snap.get("date") != _today():
        info["reason"] = f"stale ({snap.get('date')} != today)"
        return info  # never revive yesterday's state

    try:
        with scn._POS_LOCK:
            op = snap.get("open_positions", {})
            scn._OPEN_POSITIONS.clear()
            for k, v in op.items():
                scn._OPEN_POSITIONS[k] = _pos_from_json(v)
            scn._TRADED_TODAY.clear()
            scn._TRADED_TODAY.update(tuple(x) if isinstance(x, list) else x
                                      for x in snap.get("traded_today", []))
            scn._POSITIONS_OPENED = int(snap.get("positions_opened", 0))
            scn._TRADING_HALTED_TODAY = bool(snap.get("halted_today", False))
            scn._HALT_REASON = snap.get("halt_reason", "")
            scn._CONSECUTIVE_LOSSES = int(snap.get("consecutive_losses", 0))
            cd = snap.get("symbol_cooldown", {})
            scn._SYMBOL_COOLDOWN.clear()
            for k, v in cd.items():
                try:
                    scn._SYMBOL_COOLDOWN[k] = datetime.fromisoformat(v)
                except Exception:
                    pass
            # restore same-day blotter so /report is continuous after a restart
            ct = snap.get("completed_trades")
            if isinstance(ct, list):
                scn._COMPLETED_TRADES.clear(); scn._COMPLETED_TRADES.extend(ct)
            sg = snap.get("signals_today")
            if isinstance(sg, list):
                scn._SIGNALS_TODAY.clear(); scn._SIGNALS_TODAY.extend(sg)
        if app_state is not None and "manual_halt" in snap:
            app_state["trading_halted"] = bool(snap["manual_halt"])
        info.update({"restored": True,
                     "positions": len(scn._OPEN_POSITIONS),
                     "halted": scn._TRADING_HALTED_TODAY,
                     "reason": "ok"})
        log.info(f"state restored: {info['positions']} open positions, "
                 f"halted={scn._TRADING_HALTED_TODAY}, "
                 f"opened_today={scn._POSITIONS_OPENED}")
    except Exception as e:
        info["reason"] = f"restore error: {e}"
        log.warning(info["reason"])
    return info

