"""
=====================================================================
KRONOS GATE (runs on RENDER — lightweight, NO torch)
=====================================================================
This is the client side of the Kronos integration. It does NOT run the
model. It only READS the tiny kronos_forecast.json that the GitHub
Actions forecaster publishes to your Gist, caches it, and exposes a
simple gate/score-boost that the scanner uses — exactly like the
existing NIFTY trend gate.

Zero heavy deps: just `requests` (already in requirements).

Env vars (set in Render dashboard):
    KRONOS_ENABLED       "true"/"false"           (default true)
    KRONOS_MODE          "soft" | "strict"        (default soft)
    KRONOS_MIN_UP_PROB   confidence for agreement (default 0.55)
    KRONOS_MAX_STALE_MIN reject forecast older than this (default 90)
    KRONOS_BOOST         score added when Kronos agrees (default 2)
    KRONOS_PENALTY       score removed when it disagrees (default 2)
    KRONOS_GIST_ID       gist holding kronos_forecast.json
                         (falls back to GITHUB_GIST_ID)
    GITHUB_TOKEN         PAT with Gists: read
Modes:
    soft   -> never blocks a trade; only adjusts the score (+boost / -penalty)
    strict -> blocks trades that disagree with a confident Kronos view
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("kronos_gate")

KRONOS_ENABLED   = os.getenv("KRONOS_ENABLED", "true").lower() == "true"
KRONOS_MODE      = os.getenv("KRONOS_MODE", "soft").lower()          # soft|strict
MIN_UP_PROB      = float(os.getenv("KRONOS_MIN_UP_PROB", "0.55"))
MAX_STALE_MIN    = float(os.getenv("KRONOS_MAX_STALE_MIN", "90"))
KRONOS_BOOST     = int(os.getenv("KRONOS_BOOST", "2"))
KRONOS_PENALTY   = int(os.getenv("KRONOS_PENALTY", "2"))
_CACHE_TTL_SEC   = int(os.getenv("KRONOS_CACHE_TTL", "300"))         # refresh every 5 min
_OUT_FILE        = "kronos_forecast.json"
GIST_API         = "https://api.github.com/gists"

_CACHE = {"data": {}, "fetched": 0.0, "meta": {}}

# =====================================================================
# LOADING
# =====================================================================
def _gist_id() -> str:
    return (os.getenv("KRONOS_GIST_ID", "").strip()
            or os.getenv("GITHUB_GIST_ID", "").strip())

def _headers() -> dict:
    tok = os.getenv("GITHUB_TOKEN", "").strip()
    h = {"Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h

def _fetch_forecast() -> dict:
    "Pull kronos_forecast.json from the gist. Returns {} on any failure."
    gid = _gist_id()
    if not gid:
        return {}
    try:
        r = requests.get(f"{GIST_API}/{gid}", headers=_headers(), timeout=10)
        if r.status_code != 200:
            log.warning(f"Kronos gist HTTP {r.status_code}: {r.text[:120]}")
            return {}
        files = r.json().get("files", {})
        node = files.get(_OUT_FILE)
        if not node:
            return {}
        content = node.get("content")
        if content is None and node.get("truncated") and node.get("raw_url"):
            content = requests.get(node["raw_url"], headers=_headers(), timeout=10).text
        return json.loads(content) if content else {}
    except Exception as e:
        log.warning(f"Kronos fetch error: {e}")
        return {}

def _load(force: bool = False):
    "Refresh the in-memory cache if stale."
    if not KRONOS_ENABLED:
        return
    now = time.time()
    if not force and (now - _CACHE["fetched"]) < _CACHE_TTL_SEC and _CACHE["data"]:
        return
    data = _fetch_forecast()
    if data:
        _CACHE["meta"] = data.pop("_meta", {})
        _CACHE["data"] = data
        _CACHE["fetched"] = now
        log.info(f"Kronos forecast loaded: {len(data)} symbols "
                 f"(generated {_CACHE['meta'].get('generated_at')})")

def reload_forecast():
    "Force an immediate refresh (e.g. after a Gist restore)."
    _load(force=True)

# =====================================================================
# LOOKUPS
# =====================================================================
def _stale(view: dict) -> bool:
    ts = view.get("ts") or _CACHE["meta"].get("generated_at")
    if not ts:
        return True
    try:
        age_min = (datetime.now(IST) - datetime.fromisoformat(ts)).total_seconds() / 60.0
        return age_min > MAX_STALE_MIN
    except Exception:
        return False

def get_view(symbol: str) -> dict | None:
    "Return the raw Kronos view for a symbol, or None if unavailable/stale."
    if not KRONOS_ENABLED:
        return None
    _load()
    v = _CACHE["data"].get(symbol.upper())
    if not v or _stale(v):
        return None
    return v

def kronos_check(symbol: str, direction: int) -> tuple[bool, int, str]:
    """Decide how Kronos views a proposed trade.
       Returns (allow, score_adjust, reason).
         allow        : False only in STRICT mode on a confident disagreement
         score_adjust : +BOOST if Kronos agrees, -PENALTY if it disagrees, else 0
         reason       : short human string for logs / the daily report
    """
    if not KRONOS_ENABLED:
        return True, 0, "kronos:off"
    v = get_view(symbol)
    if v is None:
        return True, 0, "kronos:na"     # no view -> never block, no adjustment

    up = v.get("up_prob", 0.5)
    kdir = v.get("dir", 0)
    # confidence that the market moves in the TRADE's direction
    conf = up if direction > 0 else (1.0 - up)
    agrees = (direction > 0 and kdir > 0) or (direction < 0 and kdir < 0)
    disagrees = (direction > 0 and kdir < 0) or (direction < 0 and kdir > 0)
    confident = conf >= MIN_UP_PROB or (1.0 - conf) >= MIN_UP_PROB

    if agrees and conf >= MIN_UP_PROB:
        return True, KRONOS_BOOST, f"kronos:agree({conf:.2f})"
    if disagrees and confident:
        if KRONOS_MODE == "strict":
            return False, 0, f"kronos:veto({conf:.2f})"
        return True, -KRONOS_PENALTY, f"kronos:disagree({conf:.2f})"
    return True, 0, f"kronos:neutral({conf:.2f})"

def kronos_summary() -> str:
    "One-liner for /status."
    if not KRONOS_ENABLED:
        return "Kronos: off"
    _load()
    m = _CACHE["meta"]
    n = len(_CACHE["data"])
    if not n:
        return "Kronos: no forecast yet"
    return (f"Kronos: {n} symbols, {KRONOS_MODE} mode, "
            f"gen {m.get('generated_at', '?')}, model {m.get('model', '?')}")

