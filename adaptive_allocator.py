"""
=====================================================================
ADAPTIVE ALLOCATOR — the "learn from each day" brain (SHADOW-safe)
=====================================================================
Treats your 4 strategies as arms of a bandit. Each night it reads the
rolling ledger, scores each strategy's recent health, and computes a
RISK WEIGHT per strategy (0.0 - 1.5x). Strategies that are consistently
working get MORE size; ones bleeding get LESS — but never killed on noise,
and always with a floor so a benched strategy can prove itself again.

TWO MODES (control with ALLOC_MODE env):
  shadow  (default) : COMPUTE + LOG + write weights into live_config.json
                      under "shadow_weights" — the bot IGNORES them, so you
                      watch its decisions for 2-3 weeks with zero risk.
  active            : writes weights the scanner actually applies
                      (as "strategy_weights").

WHY IT WON'T BLOW YOU UP (the guardrails):
  * Uses a ROLLING window (last N trades), never a single day.
  * Needs MIN_TRADES_TRUST before it trusts a strategy at all.
  * Weight is bounded [W_FLOOR, W_CEIL] — can't zero-out or over-lever.
  * Smoothed (EWMA) so weights drift, never whipsaw day-to-day.
  * Exploration floor: a bad strategy keeps a small weight so it can
    recover and be re-learned (classic bandit exploration).

Env:
  ALLOC_MODE          "shadow" | "active"      (default shadow)
  ALLOC_W_FLOOR       min weight               (default 0.4)
  ALLOC_W_CEIL        max weight               (default 1.5)
  ALLOC_SMOOTH        EWMA smoothing 0..1      (default 0.5; higher = faster)
  (ledger env vars come from strategy_ledger.py)
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import strategy_ledger as ledger

IST = ZoneInfo("Asia/Kolkata")
log = logging.getLogger("allocator")

ALLOC_MODE  = os.getenv("ALLOC_MODE", "shadow").lower()   # shadow | active
W_FLOOR     = float(os.getenv("ALLOC_W_FLOOR", "0.4"))
W_CEIL      = float(os.getenv("ALLOC_W_CEIL", "1.5"))
SMOOTH      = float(os.getenv("ALLOC_SMOOTH", "0.5"))     # 0..1 (blend new vs old)

CONFIG_FILE = os.getenv("LIVE_CONFIG_PATH", "live_config.json")

# strategies the allocator manages (must match `strategy` tags from multi_strategy_live)
STRATEGIES = ["OB_SHORT", "ORB", "GAPFILL", "CANDLE_STRUCT"]


def _raw_health(s: dict) -> float:
    """Map a strategy's rolling stats -> a raw health score in ~[-1, +1.5].
       Combines expectancy sign, profit factor, and win rate. Untrusted
       (too few trades) returns 0.0 -> stays at neutral weight."""
    if not s or not s.get("trusted"):
        return 0.0
    exp = s.get("expectancy", 0.0)
    pf  = s.get("pf", 0.0)
    wr  = s.get("wr", 0.0)
    # normalise each into a small contribution
    exp_c = math.tanh(exp / 150.0)        # ±150 ₹/trade -> ~±0.6
    pf_c  = math.tanh((pf - 1.0))         # pf>1 positive, pf<1 negative
    wr_c  = (wr - 50.0) / 100.0           # 50% WR = 0, 60% = +0.1
    return exp_c + 0.5 * pf_c + wr_c


def _health_to_weight(h: float) -> float:
    """Map health (~[-1,1.5]) to a bounded risk weight [W_FLOOR, W_CEIL].
       h=0 (neutral/untrusted) -> ~1.0x."""
    w = 1.0 + 0.5 * h                      # h=+1 -> 1.5x, h=-1 -> 0.5x
    return round(max(W_FLOOR, min(W_CEIL, w)), 3)


def _load_config() -> dict:
    p = Path(CONFIG_FILE)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def _save_config(cfg: dict):
    Path(CONFIG_FILE).write_text(json.dumps(cfg, indent=2, default=str))


def compute_weights(base_dir: str | None = None) -> dict:
    """Core: read ledger stats -> per-strategy weight (smoothed vs previous).
       Returns {strategy: {weight, health, stats}} for logging + apply."""
    stats = ledger.strategy_stats(base_dir)
    cfg = _load_config()
    prev = cfg.get("strategy_weights", {}) or cfg.get("shadow_weights", {})

    result = {}
    for strat in STRATEGIES:
        s = stats.get(strat, {"n": 0, "trusted": False})
        health = _raw_health(s)
        target_w = _health_to_weight(health)
        old_w = float(prev.get(strat, {}).get("weight", 1.0)) if isinstance(prev.get(strat), dict) else float(prev.get(strat, 1.0)) if prev else 1.0
        # EWMA smoothing so weights drift, never whipsaw
        new_w = round(old_w + SMOOTH * (target_w - old_w), 3)
        new_w = round(max(W_FLOOR, min(W_CEIL, new_w)), 3)
        result[strat] = {
            "weight": new_w,
            "target": target_w,
            "prev": old_w,
            "health": round(health, 3),
            "n": s.get("n", 0),
            "trusted": s.get("trusted", False),
            "expectancy": s.get("expectancy", 0.0),
            "wr": s.get("wr", 0.0),
            "pf": s.get("pf", 0.0),
        }
    return result


def run(base_dir: str | None = None) -> dict:
    """Nightly entry point. Computes weights and writes them into
       live_config.json — under 'shadow_weights' (shadow mode, ignored by
       bot) or 'strategy_weights' (active mode, applied by scanner)."""
    weights = compute_weights(base_dir)
    cfg = _load_config()
    key = "strategy_weights" if ALLOC_MODE == "active" else "shadow_weights"
    # store just {strategy: weight} for the scanner, keep full detail alongside
    cfg[key] = {k: v["weight"] for k, v in weights.items()}
    cfg["allocator_detail"] = {
        "mode": ALLOC_MODE,
        "computed_at": datetime.now(IST).isoformat(),
        "weights": weights,
    }
    _save_config(cfg)

    # backup to Gist so it survives Render cold starts
    try:
        from gist_storage import backup_to_gist
        backup_to_gist(Path(CONFIG_FILE).parent, files=[Path(CONFIG_FILE).name])
    except Exception as e:
        log.info(f"allocator: gist backup skipped ({e})")

    log.info(f"allocator[{ALLOC_MODE}] weights: "
             + ", ".join(f"{k}={v['weight']}" for k, v in weights.items()))
    return weights


def summary_text(base_dir: str | None = None) -> str:
    """Human-readable summary for Telegram/report — shows what the
       allocator WOULD do (shadow) or IS doing (active)."""
    weights = compute_weights(base_dir)
    mode = "SHADOW 👀 (not applied)" if ALLOC_MODE != "active" else "ACTIVE ⚙️ (applied)"
    L = [f"🧠 <b>Adaptive Allocator</b> — {mode}",
         "━━━━━━━━━━━━━━━━━━━━━"]
    for strat, v in weights.items():
        arrow = "▲" if v["weight"] > v["prev"] else ("▼" if v["weight"] < v["prev"] else "▬")
        trust = "" if v["trusted"] else " (learning)"
        L.append(f"{arrow} {strat}: {v['weight']}x{trust}\n"
                 f"   n={v['n']} · exp ₹{v['expectancy']:+.0f} · "
                 f"WR {v['wr']:.0f}% · PF {v['pf']}")
    if mode.startswith("SHADOW"):
        L.append("\n<i>Shadow mode: these weights are logged only. "
                 "Set ALLOC_MODE=active to apply.</i>")
    return "\n".join(L)


def get_weight(strategy: str, base_dir: str | None = None) -> float:
    """Called by the scanner (active mode only) to size a strategy's risk.
       In shadow mode this always returns 1.0 (no effect)."""
    if ALLOC_MODE != "active":
        return 1.0
    cfg = _load_config()
    w = (cfg.get("strategy_weights", {}) or {}).get(strategy, 1.0)
    try:
        return float(w)
    except Exception:
        return 1.0


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-dir", default=".")
    ap.add_argument("--print", action="store_true", help="just print summary")
    args = ap.parse_args()
    if args.print:
        print(summary_text(args.base_dir))
    else:
        run(args.base_dir)
        print(summary_text(args.base_dir))

