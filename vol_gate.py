"""
vol_gate.py  —  scanner-side reader for the Tier-2 volatility forecast
======================================================================
Mirrors kronos_gate.py: reads vol_forecast.json (from a Gist or local file),
caches it, and exposes a per-symbol `vol_scale(symbol)` multiplier the scanner
can apply to ATR stop distances.

SAFETY / NON-DOUBLE-COUNT:
  * OFF by default. Set env VOL_GATE_ENABLED=1 to activate.
  * If your Kronos-adaptive exits are already scaling SL by predicted vol,
    leave this OFF (or use it only for symbols Kronos has no view on).
  * Any failure, stale file, missing symbol, or low model skill  -> returns
    1.0 (neutral) so the scanner behaves EXACTLY as it does today.

Env:
  VOL_GATE_ENABLED     "1" to enable (default off -> always neutral)
  VOL_GIST_ID          gist id holding vol_forecast.json  (optional)
  GIST_TOKEN           PAT with gist scope                (optional, for private)
  VOL_FORECAST_PATH    local fallback path (default ./vol_forecast.json)
  VOL_MAX_AGE_HOURS    ignore forecast older than this    (default 30)
  VOL_MIN_SKILL        require meta.skill_vs_mean >= this (default 0.0)
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Optional

GIST_FILENAME = os.environ.get("VOL_GIST_FILENAME", "vol_forecast.json")
LOCAL_PATH = os.environ.get("VOL_FORECAST_PATH", "./vol_forecast.json")
MAX_AGE_HOURS = float(os.environ.get("VOL_MAX_AGE_HOURS", "30"))
MIN_SKILL = float(os.environ.get("VOL_MIN_SKILL", "0.0"))
_CACHE_TTL = 900  # re-read at most every 15 min

_enabled = os.environ.get("VOL_GATE_ENABLED", "0") == "1"
_cache: dict = {}
_cache_ts: float = 0.0


def _fetch_raw() -> Optional[dict]:
    gid = os.environ.get("VOL_GIST_ID", "")
    token = os.environ.get("GIST_TOKEN", "")
    # try Gist first
    if gid:
        try:
            import requests
            headers = {"Accept": "application/vnd.github+json"}
            if token:
                headers["Authorization"] = f"token {token}"
            r = requests.get(f"https://api.github.com/gists/{gid}",
                             headers=headers, timeout=10)
            r.raise_for_status()
            files = r.json().get("files", {})
            if GIST_FILENAME in files:
                return json.loads(files[GIST_FILENAME]["content"])
        except Exception:
            pass
    # local fallback
    try:
        with open(LOCAL_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def _load() -> dict:
    global _cache, _cache_ts
    if time.time() - _cache_ts < _CACHE_TTL and _cache:
        return _cache
    raw = _fetch_raw() or {}
    _cache = raw
    _cache_ts = time.time()
    return raw


def _is_fresh_and_skilled(fc: dict) -> bool:
    if not fc or "symbols" not in fc:
        return False
    # freshness
    try:
        gen = datetime.fromisoformat(fc["generated_utc"])
        if gen.tzinfo is None:
            gen = gen.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - gen).total_seconds() / 3600.0
        if age_h > MAX_AGE_HOURS:
            return False
    except Exception:
        return False
    # skill guard: don't trust an unskilled/fallback model
    meta = fc.get("meta", {})
    if meta.get("model") != "lightgbm":
        return False
    # skill must strictly EXCEED the threshold: a 0-skill model == guessing
    # the mean, so we refuse to move stops on it (stays neutral 1.0).
    if float(meta.get("skill_vs_mean", 0.0)) <= MIN_SKILL:
        return False
    return True


def vol_scale(symbol: str) -> float:
    """
    Return a multiplier for the ATR stop distance for `symbol`.
    1.0 = neutral (default). >1 widen stops, <1 tighten.
    Always safe: returns 1.0 unless enabled AND forecast is fresh+skilled.
    """
    if not _enabled or not symbol:
        return 1.0
    fc = _load()
    if not _is_fresh_and_skilled(fc):
        return 1.0
    row = fc["symbols"].get(str(symbol).upper())
    if not row:
        return 1.0
    try:
        return float(row.get("scale", 1.0))
    except Exception:
        return 1.0


def vol_regime(symbol: str) -> str:
    """Human-readable regime label for alerts. '' if unknown/disabled."""
    if not _enabled or not symbol:
        return ""
    fc = _load()
    if not _is_fresh_and_skilled(fc):
        return ""
    row = fc.get("symbols", {}).get(str(symbol).upper())
    return row.get("regime", "") if row else ""


def vol_summary() -> str:
    """One-liner for /status, like kronos_summary()."""
    fc = _load()
    if not fc:
        return "Vol: no forecast"
    meta = fc.get("meta", {})
    n = len(fc.get("symbols", {}))
    fresh = "fresh" if _is_fresh_and_skilled(fc) else "stale/unskilled"
    state = "ON" if _enabled else "OFF"
    return (f"Vol[{state}]: {meta.get('model','?')} "
            f"skill={meta.get('skill_vs_mean','?')} · {n} syms · {fresh}")


if __name__ == "__main__":
    # quick self-test against a local vol_forecast.json
    os.environ["VOL_GATE_ENABLED"] = "1"
    _enabled = True
    print(vol_summary())
    for s in ["SBIN", "AXISBANK", "UNKNOWNXYZ"]:
        print(f"  {s:12s} scale={vol_scale(s)}  regime={vol_regime(s)!r}")
