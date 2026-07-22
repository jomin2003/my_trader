"""
=====================================================================
 LIVE CONFIG AUTO-LOADER for intraday_pattern_scanner_v2
---------------------------------------------------------------------
 Purpose:
   * Reads the TOP row from a param_sweep CSV.
   * Applies safety gates (fitness threshold, min trades, freshness).
   * Saves the winning parameters as a versioned JSON:
         live_config.json
   * At scanner startup, `apply_live_config()` reads that JSON and
     overwrites the scanner's module-level constants IN MEMORY.
     -> No hand-editing of intraday_pattern_scanner_v2.py needed.

 Workflow:
   1. Run param_sweep.py  -> produces sweep_<ts>.csv
   2. Run this file in "promote" mode  -> writes live_config.json
      python live_config.py promote --sweep ./sweep_out/sweep_XXX.csv
   3. Live scanner auto-picks it up (see integration snippet below).

 Two modes:
   * promote  : validate + write live_config.json
   * show     : display the currently-active config
   * clear    : delete live_config.json (revert to hardcoded defaults)

 Safety gates BEFORE promotion:
   * Config must have fitness >= --min-fitness       (default: 0.15)
   * Config must have trades  >= --min-trades        (default: 30)
   * If a live_config.json already exists, --force is required
     to overwrite it (avoids accidental live config swaps).

 Freshness gate AT LOAD TIME (inside live scanner):
   * live_config.json older than MAX_AGE_DAYS is REJECTED and the
     scanner falls back to hardcoded defaults with a Telegram warning.
     Prevents you from unknowingly trading a 3-month-old regime.

=====================================================================
 INTEGRATION - add this to intraday_pattern_scanner_v2.py:

     # near the top, after imports:
     try:
         from live_config import apply_live_config
         apply_live_config(module_name="intraday_pattern_scanner_v2")
     except Exception as e:
         log.warning(f"live_config not applied: {e}")

 That's it. If live_config.json exists and is valid, its parameters
 override the hardcoded constants. Otherwise the scanner uses the
 built-in defaults exactly as before.
=====================================================================
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

IST = ZoneInfo("Asia/Kolkata")

# Default location of the live config file (next to this script)
DEFAULT_CONFIG_PATH = Path(__file__).parent / "live_config.json"

# Max age (in days) before a live config is auto-rejected
MAX_AGE_DAYS = 30

# The set of scanner constants we're allowed to override
OVERRIDABLE_ATTRS = {
    "ATR_MULTIPLIER":       float,
    "RISK_REWARD_RATIO":    float,
    "MIN_SCORE_TO_TRADE":   int,
    "REQUIRE_CONFIRMATION": bool,
    "MIN_TURNOVER_LAKHS":   int,
    "MIN_SL_PCT":           float,
    "MAX_SL_PCT":           float,
    "TOP_N_RESULTS":        int,   # maps from sweep's top_per_bar
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("live_config")


# =====================================================================
# PROMOTION LOGIC
# =====================================================================
def _cast_value(name: str, raw):
    """Coerce a value from the CSV to the right Python type."""
    target = OVERRIDABLE_ATTRS.get(name)
    if target is None:
        return raw
    try:
        if target is bool:
            if isinstance(raw, bool):
                return raw
            s = str(raw).strip().lower()
            return s in ("true", "1", "yes", "y")
        if target is int:
            return int(float(raw))
        if target is float:
            return float(raw)
    except Exception:
        return raw
    return raw


def _translate_nifty_gate(gate: str) -> dict:
    """
    Sweep uses 'off' / 'soft' / 'strict'. Live scanner has TWO flags:
      NIFTY_GATE_ENABLED (bool)  +  NIFTY_STRICT (bool)
    """
    gate = str(gate).strip().lower()
    if gate == "off":    return {"NIFTY_GATE_ENABLED": False, "NIFTY_STRICT": False}
    if gate == "soft":   return {"NIFTY_GATE_ENABLED": True,  "NIFTY_STRICT": False}
    if gate == "strict": return {"NIFTY_GATE_ENABLED": True,  "NIFTY_STRICT": True}
    return {"NIFTY_GATE_ENABLED": True, "NIFTY_STRICT": False}   # safe default


def build_config_from_sweep(sweep_csv: Path, row_index: int = 0) -> dict:
    """Read the sweep CSV and extract the requested row as a config dict."""
    df = pd.read_csv(sweep_csv)
    if df.empty:
        raise ValueError(f"Sweep CSV is empty: {sweep_csv}")
    if row_index >= len(df):
        raise ValueError(f"row_index {row_index} out of range (have {len(df)} rows)")

    # Sweep CSV is already sorted by fitness desc; take row_index-th
    row = df.iloc[row_index]

    params: dict = {}
    for attr in OVERRIDABLE_ATTRS:
        # top_per_bar in sweep maps to TOP_N_RESULTS in live scanner
        src = "top_per_bar" if attr == "TOP_N_RESULTS" else attr
        if src in row and pd.notna(row[src]):
            params[attr] = _cast_value(attr, row[src])

    # NIFTY gate translation
    if "nifty_gate" in row and pd.notna(row["nifty_gate"]):
        params.update(_translate_nifty_gate(row["nifty_gate"]))

    # Metadata for auditability
    meta = {
        "source_sweep_csv":  str(sweep_csv),
        "source_row_index":  int(row_index),
        "promoted_at":       datetime.now(IST).isoformat(),
        "promoted_at_human": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "fitness":           float(row.get("fitness", 0.0)) if "fitness" in row else None,
        "trades":            int(row.get("trades", 0)) if "trades" in row else None,
        "win_rate":          float(row.get("win_rate", 0.0)) if "win_rate" in row else None,
        "profit_factor":     float(row.get("profit_factor", 0.0)) if "profit_factor" in row else None,
        "expectancy_R":      float(row.get("expectancy_R", 0.0)) if "expectancy_R" in row else None,
        "max_drawdown_pct":  float(row.get("max_drawdown_pct", 0.0)) if "max_drawdown_pct" in row else None,
    }

    return {"meta": meta, "params": params}


def validate_config(cfg: dict, min_fitness: float, min_trades: int) -> tuple[bool, list[str]]:
    """Return (is_valid, list_of_warnings)."""
    warnings = []
    meta = cfg.get("meta", {})

    fitness = meta.get("fitness")
    trades  = meta.get("trades")

    if fitness is None or fitness < min_fitness:
        warnings.append(f"fitness {fitness} < required {min_fitness}")
    if trades is None or trades < min_trades:
        warnings.append(f"trades {trades} < required {min_trades}")

    # Sanity ranges on parameters
    p = cfg.get("params", {})
    if "ATR_MULTIPLIER" in p and not (0.5 <= p["ATR_MULTIPLIER"] <= 4.0):
        warnings.append(f"ATR_MULTIPLIER {p['ATR_MULTIPLIER']} out of [0.5, 4.0]")
    if "RISK_REWARD_RATIO" in p and not (1.0 <= p["RISK_REWARD_RATIO"] <= 5.0):
        warnings.append(f"RISK_REWARD_RATIO {p['RISK_REWARD_RATIO']} out of [1.0, 5.0]")
    if "MIN_SCORE_TO_TRADE" in p and not (1 <= p["MIN_SCORE_TO_TRADE"] <= 15):
        warnings.append(f"MIN_SCORE_TO_TRADE {p['MIN_SCORE_TO_TRADE']} out of [1, 15]")
    if "MIN_SL_PCT" in p and "MAX_SL_PCT" in p:
        if p["MIN_SL_PCT"] >= p["MAX_SL_PCT"]:
            warnings.append(f"MIN_SL_PCT >= MAX_SL_PCT (invalid clamp)")

    is_valid = len(warnings) == 0
    return is_valid, warnings


def promote(sweep_csv: Path, out_path: Path, row_index: int,
            min_fitness: float, min_trades: int, force: bool) -> dict:
    """Validate and write live_config.json."""
    log.info(f"Reading sweep row #{row_index} from {sweep_csv.name}")
    cfg = build_config_from_sweep(sweep_csv, row_index)

    valid, warnings = validate_config(cfg, min_fitness, min_trades)
    if not valid:
        log.warning("Config failed validation:")
        for w in warnings:
            log.warning(f"  * {w}")
        if not force:
            raise SystemExit(
                "Refusing to promote invalid config. Pass --force to override "
                "(NOT recommended for live trading)."
            )
        log.warning("Proceeding despite validation failures (--force).")

    if out_path.exists() and not force:
        # Show diff-friendly summary of what would change
        try:
            old = json.loads(out_path.read_text())
            log.warning(f"live_config.json already exists (fitness={old['meta'].get('fitness')})")
            log.warning(f"New config fitness = {cfg['meta'].get('fitness')}")
        except Exception:
            pass
        raise SystemExit(f"{out_path.name} exists. Pass --force to overwrite.")

    out_path.write_text(json.dumps(cfg, indent=2, default=str))
    log.info(f"Wrote {out_path}")

    # Human-readable summary
    print("\n" + "=" * 60)
    print(" LIVE CONFIG PROMOTED")
    print("=" * 60)
    print(f"  Source:       {cfg['meta']['source_sweep_csv']}")
    print(f"  Promoted at:  {cfg['meta']['promoted_at_human']}")
    print(f"  Fitness:      {cfg['meta'].get('fitness')}")
    print(f"  Trades:       {cfg['meta'].get('trades')}")
    print(f"  Win rate:     {cfg['meta'].get('win_rate')}%")
    print(f"  Profit factor: {cfg['meta'].get('profit_factor')}")
    print(f"  Expectancy_R: {cfg['meta'].get('expectancy_R')}")
    print(f"  Max DD:       {cfg['meta'].get('max_drawdown_pct')}%")
    print("\n  Parameters that will override live scanner:")
    for k, v in cfg["params"].items():
        print(f"    {k:>22}: {v}")
    print("\n  Next: launch the live scanner. It will auto-apply this config.")
    return cfg


# =====================================================================
# LOAD-TIME (called from live scanner)
# =====================================================================
def _config_age_days(cfg: dict) -> float | None:
    try:
        ts = datetime.fromisoformat(cfg["meta"]["promoted_at"])
        return (datetime.now(IST) - ts).total_seconds() / 86400.0
    except Exception:
        return None


def apply_live_config(module_name: str = "intraday_pattern_scanner_v2",
                      config_path: Path | str | None = None,
                      tg_sender=None) -> dict | None:
    """
    Called at scanner startup. Loads live_config.json (if any) and
    overwrites the target module's constants IN MEMORY.

    Args:
      module_name: name of the scanner module to patch.
      config_path: override the default location.
      tg_sender:   optional callable(text: str) to send Telegram alerts.

    Returns the applied config dict, or None if no config was applied.
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        log.info(f"No {path.name} found. Using hardcoded scanner defaults.")
        return None

    try:
        cfg = json.loads(path.read_text())
    except Exception as e:
        log.error(f"Could not parse {path}: {e}. Ignoring.")
        if tg_sender:
            tg_sender(f"⚠️ live_config.json unreadable: {e}. Using defaults.")
        return None

    # Freshness gate
    age = _config_age_days(cfg)
    if age is not None and age > MAX_AGE_DAYS:
        msg = (f"live_config.json is {age:.0f} days old (>{MAX_AGE_DAYS}). "
               f"Rejecting; falling back to hardcoded defaults.")
        log.warning(msg)
        if tg_sender:
            tg_sender(f"⚠️ {msg}\nRe-run param_sweep + live_config promote.")
        return None

    # Locate the target module
    if module_name not in sys.modules:
        try:
            __import__(module_name)
        except Exception as e:
            log.error(f"Cannot import {module_name}: {e}")
            return None
    mod = sys.modules[module_name]

    # Apply overrides
    applied = {}
    skipped = []
    params = cfg.get("params", {})
    for name, raw_val in params.items():
        # Only touch scanner-defined constants
        if not hasattr(mod, name):
            # Special cases the live scanner may not have YET; skip gracefully
            skipped.append(name)
            continue
        val = _cast_value(name, raw_val) if name in OVERRIDABLE_ATTRS else raw_val
        old = getattr(mod, name)
        setattr(mod, name, val)
        applied[name] = {"old": old, "new": val}

    # Also handle the two NIFTY flags (these may not exist yet in v2;
    # setattr creates them and the scanner can pick them up if it's
    # been wired to read them.)
    for flag in ("NIFTY_GATE_ENABLED", "NIFTY_STRICT"):
        if flag in params:
            old = getattr(mod, flag, None)
            setattr(mod, flag, bool(params[flag]))
            applied[flag] = {"old": old, "new": bool(params[flag])}

    log.info(f"Applied {len(applied)} live-config overrides from {path.name}")
    for k, v in applied.items():
        log.info(f"  {k}: {v['old']} -> {v['new']}")
    if skipped:
        log.debug(f"Skipped (attr not in scanner): {skipped}")

    if tg_sender:
        summary = ", ".join(f"{k}={v['new']}" for k, v in list(applied.items())[:5])
        tg_sender(
            f"✅ Live config applied "
            f"(fitness={cfg['meta'].get('fitness')}, "
            f"promoted {cfg['meta'].get('promoted_at_human')})\n"
            f"{summary}..."
        )

    return cfg


# =====================================================================
# CLI
# =====================================================================
def _cmd_show(path: Path):
    if not path.exists():
        print(f"No live config at {path}. (Scanner will use hardcoded defaults.)")
        return
    cfg = json.loads(path.read_text())
    age = _config_age_days(cfg)
    print(f"\nLive config at: {path}")
    print(f"Promoted at:    {cfg['meta'].get('promoted_at_human')}")
    print(f"Age (days):     {age:.1f}" if age is not None else "Age: unknown")
    print(f"Fitness:        {cfg['meta'].get('fitness')}")
    print(f"Trades:         {cfg['meta'].get('trades')}")
    print(f"Win rate:       {cfg['meta'].get('win_rate')}%")
    print(f"Profit factor:  {cfg['meta'].get('profit_factor')}")
    print(f"\nParameters:")
    for k, v in cfg.get("params", {}).items():
        print(f"  {k:>22}: {v}")
    if age is not None and age > MAX_AGE_DAYS:
        print(f"\n⚠️  Config is older than {MAX_AGE_DAYS} days — scanner will REJECT it.")


def _cmd_clear(path: Path):
    if path.exists():
        path.unlink()
        print(f"Deleted {path}. Scanner will use hardcoded defaults.")
    else:
        print("Nothing to delete.")


def main():
    ap = argparse.ArgumentParser(description="Live config manager for scanner v2")
    sub = ap.add_subparsers(dest="cmd", required=True)

    # promote
    p = sub.add_parser("promote", help="Promote a sweep row to live_config.json")
    p.add_argument("--sweep", required=True, help="Path to sweep_XXX.csv")
    p.add_argument("--row", type=int, default=0,
                   help="Row index in sweep CSV (0 = top). Default: 0")
    p.add_argument("--out", type=str, default=str(DEFAULT_CONFIG_PATH),
                   help="Output path (default: live_config.json next to this script)")
    p.add_argument("--min-fitness", type=float, default=0.15,
                   help="Reject if fitness < this. Default: 0.15")
    p.add_argument("--min-trades", type=int, default=30,
                   help="Reject if trades < this. Default: 30")
    p.add_argument("--force", action="store_true",
                   help="Overwrite existing config AND skip validation")

    # show
    s = sub.add_parser("show", help="Show current live config")
    s.add_argument("--path", type=str, default=str(DEFAULT_CONFIG_PATH))

    # clear
    c = sub.add_parser("clear", help="Delete live_config.json (revert to defaults)")
    c.add_argument("--path", type=str, default=str(DEFAULT_CONFIG_PATH))

    args = ap.parse_args()

    if args.cmd == "promote":
        promote(
            Path(args.sweep).expanduser().resolve(),
            Path(args.out).expanduser().resolve(),
            args.row, args.min_fitness, args.min_trades, args.force,
        )
    elif args.cmd == "show":
        _cmd_show(Path(args.path).expanduser().resolve())
    elif args.cmd == "clear":
        _cmd_clear(Path(args.path).expanduser().resolve())


if __name__ == "__main__":
    main()
