"""
=====================================================================
STRATEGY LEDGER — persistent per-strategy performance memory
=====================================================================
The MEMORY layer of the self-improving loop. Render wipes disk on every
restart, so the ledger lives in a GitHub Gist (same trick as
live_config.json). Every completed trade is appended with the context
that matters, so the allocator can learn WHAT works and WHEN.

What it stores (strategy_ledger.json):
{
  "trades": [
     {"date":"2026-07-28","strategy":"CANDLE_STRUCT","side":"SELL",
      "pnl":140.3,"r":1.8,"outcome":"TARGET",
      "nifty":-1,"kronos":"agree","hour":11},
     ...
  ],
  "_meta": {"updated": "...", "n": 1234}
}

Rolling window: only the last LEDGER_MAX_TRADES are kept (default 1500),
so it never bloats the Gist.

SAFE: if Gist creds are missing it degrades to a local file. Nothing here
places orders or changes behaviour — it only records + serves stats.

Env:
    GITHUB_TOKEN     PAT with Gists: read/write   (reuses your existing one)
    LEDGER_GIST_ID   gist id for the ledger (falls back to GITHUB_GIST_ID)
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("ledger")

GIST_API = "https://api.github.com/gists"
LEDGER_FILE = "strategy_ledger.json"
LEDGER_MAX_TRADES = int(os.getenv("LEDGER_MAX_TRADES", "1500"))

# how many recent trades define a strategy's "rolling" performance
ROLL_TRADES = int(os.getenv("LEDGER_ROLL_TRADES", "40"))
# minimum trades before we trust a strategy's stats at all
MIN_TRADES_TRUST = int(os.getenv("LEDGER_MIN_TRUST", "12"))

# ---------------------------------------------------------------------
def _gid() -> str:
    return (os.getenv("LEDGER_GIST_ID", "").strip()
            or os.getenv("GITHUB_GIST_ID", "").strip())

def _headers() -> dict:
    tok = os.getenv("GITHUB_TOKEN", "").strip()
    h = {"Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h

def _local_path(base_dir: str | None = None) -> Path:
    return Path(base_dir or ".") / LEDGER_FILE

# ---------------------------------------------------------------------
def load_ledger(base_dir: str | None = None) -> dict:
    "Load the ledger from Gist (preferred) or local file. Never raises."
    gid = _gid()
    tok = os.getenv("GITHUB_TOKEN", "").strip()
    if gid and tok:
        try:
            r = requests.get(f"{GIST_API}/{gid}", headers=_headers(), timeout=10)
            if r.status_code == 200:
                node = r.json().get("files", {}).get(LEDGER_FILE)
                if node:
                    content = node.get("content")
                    if content is None and node.get("raw_url"):
                        content = requests.get(node["raw_url"], headers=_headers(),
                                               timeout=10).text
                    if content:
                        return json.loads(content)
        except Exception as e:
            log.warning(f"ledger gist load failed: {e}")
    # local fallback
    p = _local_path(base_dir)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"trades": [], "_meta": {}}

def save_ledger(ledger: dict, base_dir: str | None = None) -> bool:
    "Write ledger to local file + Gist. Returns True if Gist push ok."
    ledger.setdefault("_meta", {})
    ledger["_meta"]["updated"] = datetime.now(IST).isoformat()
    ledger["_meta"]["n"] = len(ledger.get("trades", []))
    payload = json.dumps(ledger, indent=2)
    _local_path(base_dir).write_text(payload)

    gid = _gid(); tok = os.getenv("GITHUB_TOKEN", "").strip()
    if not gid or not tok:
        log.warning("ledger: no Gist creds — saved locally only")
        return False
    try:
        r = requests.patch(f"{GIST_API}/{gid}", headers=_headers(),
                           json={"files": {LEDGER_FILE: {"content": payload}}},
                           timeout=20)
        ok = r.status_code == 200
        if not ok:
            log.warning(f"ledger gist push HTTP {r.status_code}: {r.text[:150]}")
        return ok
    except Exception as e:
        log.warning(f"ledger gist push error: {e}")
        return False

# ---------------------------------------------------------------------
def append_today(trades: list[dict], nifty_trend: int = 0,
                 base_dir: str | None = None) -> dict:
    """Append today's completed trades to the ledger with context.
       `trades` = your scanner's _COMPLETED_TRADES (list of dicts).
       Returns the updated ledger."""
    led = load_ledger(base_dir)
    rows = led.setdefault("trades", [])
    today = datetime.now(IST).date().isoformat()

    for t in trades:
        # parse the kronos tag into a compact label
        kro = str(t.get("kronos", "")).lower()
        kro_label = ("agree" if "agree" in kro else
                     "disagree" if "disagree" in kro else
                     "na")
        # hour of day from the trade's time string (HH:MM:SS)
        hour = None
        try:
            hour = int(str(t.get("time", "")).split(":")[0])
        except Exception:
            pass
        rows.append({
            "date":     today,
            "strategy": t.get("strategy", "?"),
            "side":     t.get("side", ""),
            "pnl":      float(t.get("pnl", 0.0)),
            "r":        float(t.get("r", 0.0)),
            "outcome":  t.get("outcome", ""),
            "nifty":    int(nifty_trend),
            "kronos":   kro_label,
            "hour":     hour,
        })

    # trim to rolling max
    if len(rows) > LEDGER_MAX_TRADES:
        led["trades"] = rows[-LEDGER_MAX_TRADES:]
    save_ledger(led, base_dir)
    log.info(f"ledger: appended {len(trades)} trades (total {len(led['trades'])})")
    return led

# ---------------------------------------------------------------------
def _stats(pnls: list[float]) -> dict:
    "Basic performance stats for a list of trade PnLs."
    n = len(pnls)
    if n == 0:
        return {"n": 0, "wr": 0.0, "expectancy": 0.0, "pf": 0.0, "net": 0.0}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_w = sum(wins)
    gross_l = -sum(losses)
    pf = (gross_w / gross_l) if gross_l > 0 else (999.0 if gross_w > 0 else 0.0)
    return {
        "n": n,
        "wr": round(len(wins) / n * 100, 1),
        "expectancy": round(sum(pnls) / n, 2),
        "pf": round(min(pf, 999.0), 2),
        "net": round(sum(pnls), 2),
    }

def strategy_stats(base_dir: str | None = None, ledger: dict | None = None) -> dict:
    """Return rolling stats per strategy (last ROLL_TRADES each), plus
       context breakdowns (nifty regime, kronos state). Used by the
       allocator and the /report."""
    led = ledger or load_ledger(base_dir)
    rows = led.get("trades", [])
    by_strat = defaultdict(list)
    for r in rows:
        by_strat[r.get("strategy", "?")].append(r)

    out = {}
    for strat, tr in by_strat.items():
        recent = tr[-ROLL_TRADES:]
        pnls = [x["pnl"] for x in recent]
        s = _stats(pnls)
        # context: performance when nifty agrees vs kronos agrees
        s["kronos_agree"] = _stats([x["pnl"] for x in recent if x.get("kronos") == "agree"])
        s["kronos_na"]    = _stats([x["pnl"] for x in recent if x.get("kronos") == "na"])
        s["trusted"] = s["n"] >= MIN_TRADES_TRUST
        out[strat] = s
    return out

