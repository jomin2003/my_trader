"""
config_registry.py — SINGLE SOURCE OF TRUTH for all configuration.
===================================================================
Replaces 20+ scattered os.getenv() calls across the codebase.
Provides:
  * typed access with defaults
  * hot-reload without restart
  * config versioning (hash of all resolved values)
  * secret-safe serialization (auto-masks configured secrets)
  * environment override via YAML file (./config.yaml, optional)

Usage:
  import config_registry as cfg
  cfg.get("DHAN_CLIENT_ID")   # typed value: env > config.yaml > default
  cfg.get("MAX_OPEN_POSITIONS")
  cfg.version()               # hash of all resolved values

Env vars still work — this is a thin wrapper, not a replacement.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import yaml  # type: ignore

log = logging.getLogger("config")

# --------------------------------------------------------------------------- #
# DEFAULTS — every configurable value lives here, not in scattered os.getenv() #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class _Defaults:
    # ---- DHAN ----
    DHAN_CLIENT_ID: str = ""
    DHAN_ACCESS_TOKEN: str = ""
    DHAN_PIN: str = ""
    DHAN_TOTP_SECRET: str = ""

    # ---- TELEGRAM ----
    TG_BOT_TOKEN: str = ""
    TG_CHAT_ID: str = ""

    # ---- GITHUB ----
    GITHUB_TOKEN: str = ""
    GITHUB_GIST_ID: str = ""
    CRON_SECRET: str = ""
    LEDGER_GIST_ID: str = ""
    STATE_GIST_ID: str = ""

    # ---- KRONOS ----
    KRONOS_GIST_ID: str = ""
    KRONOS_ENABLED: bool = True
    KRONOS_MODE: str = "soft"
    KRONOS_MIN_UP_PROB: float = 0.55
    KRONOS_MAX_STALE_MIN: float = 90.0
    KRONOS_BOOST: int = 2
    KRONOS_PENALTY: int = 2

    # ---- VOL GATE ----
    VOL_GATE_ENABLED: bool = False
    VOL_GIST_ID: str = ""
    VOL_MIN_SKILL: float = 0.0
    VOL_MAX_AGE_HOURS: float = 30.0

    # ---- STRATEGY PARAMS (PROFITABLE-ONLY: SL>=0.6% so cost/R<=0.3) ----
    ATR_MULTIPLIER: float = 2.0
    RISK_REWARD_RATIO: float = 2.0
    MIN_SCORE_TO_TRADE: int = 8
    REQUIRE_CONFIRMATION: bool = False
    MIN_TURNOVER_LAKHS: int = 25
    MIN_SL_PCT: float = 0.006
    MAX_SL_PCT: float = 0.015
    TOP_N_RESULTS: int = 20
    MAX_STOCKS: int = 180
    MIN_CANDLES_NEEDED: int = 4
    CANDLE_INTERVAL_MIN: int = 5
    ADX_MIN_THRESHOLD: int = 20
    ADX_FILTER_ENABLED: bool = True
    MIN_RR: float = 1.2
    MAX_RR: float = 5.0

    # ---- RISK (PROFITABLE-ONLY: max 3/day, halt -2%) ----
    MAX_RISK_PER_TRADE: float = 500.0
    MAX_CAPITAL_PER_TRADE: float = 25000.0
    MAX_OPEN_POSITIONS: int = 3
    DAILY_MAX_TRADES: int = 3
    DAILY_MAX_LOSS: float = 2000.0
    DAILY_PROFIT_TARGET: float = 0.0
    MAX_CONSECUTIVE_LOSSES: int = 3
    LOSS_COOLDOWN_MINUTES: int = 30
    MAX_PORTFOLIO_HEAT: float = 2000.0

    # ---- EXECUTION ----
    AUTO_TRADE_ENABLED: bool = False
    SLIPPAGE_BPS: int = 3
    # FIX: honest Dhan intraday cost structure. Old model (6 bps one-way taxes, zero
    # brokerage) undercharged ~Rs15-20/trade. Real NSE equity intraday:
    #   STT 0.025% sell-side only (~1.25 bps one-way effective)
    #   + NSE txn 0.297 bps/side + SEBI 0.001 bps + stamp 0.3 bps buy-side
    #   + GST 18% on (brokerage + txn + SEBI)
    # => ~2 bps one-way non-brokerage, PLUS Dhan Rs20 min brokerage/order + GST
    #    = Rs23.6/order effective. Round trip at Rs25k notional ≈ 29 bps total.
    TAXES_BPS_ONEWAY: int = 2
    BROKERAGE_PER_TRADE: float = 23.6
    REQUEST_SLEEP_SEC: float = 0.22
    POSITION_POLL_SEC: int = 20
    FILL_POLICY: str = "conservative"
    # Parallel fetch workers for scan_once. 1 = sequential (legacy behavior).
    # >1 parallelises only the network fetch phase; pattern detection and
    # scoring stay sequential (score_signals uses a module-global scratch df).
    SCAN_WORKERS: int = 1

    # ---- TIME ----
    MARKET_OPEN_HOUR: int = 9
    MARKET_OPEN_MIN: int = 15
    MARKET_CLOSE_HOUR: int = 15
    MARKET_CLOSE_MIN: int = 30
    SCAN_START_HOUR: int = 9
    SCAN_START_MIN: int = 30
    NO_ENTRY_AFTER_HOUR: int = 14
    NO_ENTRY_AFTER_MIN: int = 30
    MARKET_TZ: str = "Asia/Kolkata"

    # ---- ALLOCATOR ----
    ALLOC_MODE: str = "shadow"
    ALLOC_W_FLOOR: float = 0.4
    ALLOC_W_CEIL: float = 1.5
    ALLOC_SMOOTH: float = 0.5
    ALLOC_MAX_STEP: float = 0.15

    # ---- EXIT POLICY ----
    EXIT_POLICY_V2_ENABLED: bool = False
    EXIT_MIN_RR: float = 1.2
    EXIT_MAX_RR: float = 5.0
    KEXIT_ENABLED: bool = True
    KEXIT_VOL_REF: float = 0.30
    KEXIT_MIN_MULT: float = 0.7
    KEXIT_MAX_MULT: float = 1.6
    KEXIT_TGT_CAP_FRAC: float = 0.9
    KEXIT_MIN_RR: float = 1.2
    VOL_SCALE_LO: float = 0.65
    VOL_SCALE_HI: float = 1.60

    # ---- RR PREDICTOR ----
    RR_GATE_ENABLED: bool = False
    RR_MIN_SKILL: float = 0.0
    RR_MIN_PRED_R: float = 0.0
    RR_MODEL_PATH: str = "./rr_model.txt"
    RR_META_PATH: str = "./rr_meta.json"

    # ---- PERSISTENCE ----
    BACKEND: str = "gist"
    GIST_ENABLED: bool = True
    LEDGER_MAX_TRADES: int = 1500
    LEDGER_ROLL_TRADES: int = 40
    MIN_TRADES_TRUST: int = 12
    STATE_WRITE_SEC: int = 60
    CONFIG_MAX_AGE_DAYS: int = 30

    # ---- TELEMETRY ----
    AUDIT_ENABLED: bool = True
    AUDIT_RING: int = 200
    TELEGRAM_ENABLED: bool = True
    TELEGRAM_RATE_LIMIT: int = 10

    # ---- STRATEGIES (code capability) + PROFITABLE-ONLY allowlist ----
    # STRATEGIES_ENABLED = what the code CAN trade. ALLOWED_STRATEGIES = what the
    # backtest gate PROVED (PF>=1.3, expR>0.15, n>=30, 2/3 folds PF>1.0).
    # 59d/10-sym yfinance audit 2026-09-11: NOTHING passed walk-forward
    # (CANDLE PF 0.99/0.44/6.11 unstable; all else PF<1.0) => default NONE.
    # Paper still logs signals; live entries require allowlist membership.
    STRATEGIES_ENABLED: tuple = ("OB_SHORTS", "ORB", "GAPFILL", "GAPGO", "CANDLE_STRUCT", "VWAP_RECLAIM", "VWAP_PULLBACK", "SUPERTREND", "MR_VWAP_FADE", "PDHL_BREAK", "FLAG", "TRIANGLE", "DOUBLE_TOP_BOTTOM", "ABCD")
    ALLOWED_STRATEGIES: tuple = ()
    PROFITABLE_ONLY: bool = True

    # ---- NIFTY REGIME GATE ----
    # Read by the scanner at import; live_config can flip them at runtime
    # when a sweep promotes a nifty_gate setting.
    NIFTY_GATE_ENABLED: bool = False
    NIFTY_STRICT: bool = False


# --------------------------------------------------------------------------- #
# STRATEGY NAME NORMALIZATION — single enum to rule them all                   #
# --------------------------------------------------------------------------- #

class StrategyName:
    """Canonical strategy identifiers. All code uses these constants."""
    OB_SHORTS = "OB_SHORTS"
    ORB = "ORB"
    GAPFILL = "GAPFILL"
    GAPGO = "GAPGO"
    CANDLE_STRUCT = "CANDLE_STRUCT"
    VWAP_RECLAIM = "VWAP_RECLAIM"
    VWAP_PULLBACK = "VWAP_PULLBACK"
    SUPERTREND = "SUPERTREND"
    MR_VWAP_FADE = "MR_VWAP_FADE"
    PDHL_BREAK = "PDHL_BREAK"
    FLAG = "FLAG"
    TRIANGLE = "TRIANGLE"
    DOUBLE_TOP_BOTTOM = "DOUBLE_TOP_BOTTOM"
    ABCD = "ABCD"

    ALL = (OB_SHORTS, ORB, GAPFILL, GAPGO, CANDLE_STRUCT, VWAP_RECLAIM, VWAP_PULLBACK,
           SUPERTREND, MR_VWAP_FADE, PDHL_BREAK,
           FLAG, TRIANGLE, DOUBLE_TOP_BOTTOM, ABCD)

    @staticmethod
    def normalize(name: str) -> str:
        """Normalize any alias to canonical form."""
        if name is None:
            return ""
        n = str(name).strip().upper()
        # Aliases -> canonical
        aliases = {
            "OB": StrategyName.OB_SHORTS,
            "OB_SHORT": StrategyName.OB_SHORTS,
            "OB SHORTS": StrategyName.OB_SHORTS,
            "OB_SHORTS": StrategyName.OB_SHORTS,
            "OB_BEAR_STAR": StrategyName.OB_SHORTS,
            "OBBEARSTAR": StrategyName.OB_SHORTS,
            "GAP": StrategyName.GAPFILL,
            "GAP_FILL": StrategyName.GAPFILL,
            "GAPFILL": StrategyName.GAPFILL,
            "GAP-FILL": StrategyName.GAPFILL,
            "GAPGO": StrategyName.GAPGO,
            "GAP_GO": StrategyName.GAPGO,
            "GAP-GO": StrategyName.GAPGO,
            "CANDLE": StrategyName.CANDLE_STRUCT,
            "CANDLE_STRUCT": StrategyName.CANDLE_STRUCT,
            "CANDLE-STRUCT": StrategyName.CANDLE_STRUCT,
            "CS": StrategyName.CANDLE_STRUCT,
            "VWAP": StrategyName.VWAP_RECLAIM,
            "VWAP_RECLAIM": StrategyName.VWAP_RECLAIM,
            "VWAP_PULLBACK": StrategyName.VWAP_PULLBACK,
            "VWAP PULLBACK": StrategyName.VWAP_PULLBACK,
            "VWAP RECLAIM": StrategyName.VWAP_RECLAIM,
            "SUPERTREND": StrategyName.SUPERTREND,
            "ST": StrategyName.SUPERTREND,
            "SUPERLONG": StrategyName.SUPERTREND,
            "SUPERSHORT": StrategyName.SUPERTREND,
            "MR": StrategyName.MR_VWAP_FADE,
            "MR_FADE": StrategyName.MR_VWAP_FADE,
            "VWAPFADE": StrategyName.MR_VWAP_FADE,
            "VWAP_FADE": StrategyName.MR_VWAP_FADE,
            "VWAP FADE": StrategyName.MR_VWAP_FADE,
            "PDHL": StrategyName.PDHL_BREAK,
            "PDHL_BREAK": StrategyName.PDHL_BREAK,
            "PDHL BREAK": StrategyName.PDHL_BREAK,
            "FLAG": StrategyName.FLAG,
            "TRIANGLE": StrategyName.TRIANGLE,
            "DOUBLETOP": StrategyName.DOUBLE_TOP_BOTTOM,
            "DOUBLE_TOP": StrategyName.DOUBLE_TOP_BOTTOM,
            "DOUBLEBOTTOM": StrategyName.DOUBLE_TOP_BOTTOM,
            "DOUBLE_BOTTOM": StrategyName.DOUBLE_TOP_BOTTOM,
            "ABCD": StrategyName.ABCD,
        }
        return aliases.get(n, n)

    @staticmethod
    def is_valid(name: str) -> bool:
        return name in StrategyName.ALL


# --------------------------------------------------------------------------- #
# CONFIG REGISTRY                                                             #
# --------------------------------------------------------------------------- #

class ConfigRegistry:
    """
    Singleton config registry.  All modules import this instead of doing
    os.getenv() directly.

    Thread-safe.  Supports hot-reload.  Produces a config version hash.
    """

    _instance: "ConfigRegistry | None" = None
    _lock = threading.RLock()

    def __new__(cls) -> "ConfigRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        # Only init once
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._cache: dict[str, Any] = {}
        self._cache_version: str = ""
        self._reload_lock = threading.RLock()
        self._defaults = _Defaults()
        self._yaml_path = Path("config.yaml")

    # ---- public access -------------------------------------------------- #

    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value.  Falls back to default, then env, then _Defaults."""
        with self._reload_lock:
            if key in self._cache:
                return self._cache[key]
            value = self._resolve(key)
            if value is None and default is not None:
                value = default
            self._cache[key] = value
            return value

    def set(self, key: str, value: Any) -> None:
        """Explicitly set a config value (for testing / overrides)."""
        with self._reload_lock:
            self._cache[key] = value
            self._cache_version = ""  # invalidate version

    def all(self) -> dict[str, Any]:
        """Return all resolved config values as a dict."""
        with self._reload_lock:
            for f in fields(self._defaults):
                key = f.name
                if key not in self._cache:
                    self._cache[key] = self._resolve(key)
            return dict(self._cache)

    def reload(self) -> None:
        """Clear the cache and re-resolve everything.  Safe to call at any time."""
        with self._reload_lock:
            self._cache.clear()
            self._cache_version = ""
            log.info("Config cache cleared — next access re-resolves")

    def version(self) -> str:
        """Hash of all resolved config values.  Changes when config changes."""
        if self._cache_version:
            return self._cache_version
        all_vals = self.all()
        serialized = json.dumps(all_vals, sort_keys=True, default=str)
        self._cache_version = "cfg-" + hashlib.sha256(serialized.encode()).hexdigest()[:12]
        return self._cache_version

    def safe_dump(self) -> dict:
        """Return config dict with secrets masked.  Safe for logging/telegram."""
        safe: dict[str, Any] = {}
        secret_keys = {
            "DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN", "DHAN_PIN", "DHAN_TOTP_SECRET",
            "TG_BOT_TOKEN", "TG_CHAT_ID", "CRON_SECRET", "GITHUB_TOKEN",
            "GITHUB_GIST_ID", "LEDGER_GIST_ID", "STATE_GIST_ID",
            "KRONOS_GIST_ID", "VOL_GIST_ID", "GIST_TOKEN",
        }
        for k, v in self.all().items():
            if k in secret_keys:
                safe[k] = "***MASKED***" if v else ""
            else:
                safe[k] = v
        return safe

    # ---- internal resolution -------------------------------------------- #

    def _resolve(self, key: str) -> Any:
        """Resolve a single key: env > yaml > defaults."""
        # 1. Environment variable
        env_val = os.getenv(key)
        if env_val is not None:
            return self._cast(key, env_val)

        # 2. YAML file (if it exists)
        if self._yaml_path.exists():
            try:
                with open(self._yaml_path) as fh:
                    yaml_cfg = yaml.safe_load(fh)
                if yaml_cfg and key in yaml_cfg:
                    return yaml_cfg[key]
            except Exception:
                pass

        # 3. Default
        default = getattr(self._defaults, key, None)
        return default

    @staticmethod
    def _cast(key: str, raw: str) -> Any:
        """Coerce a string env var to its expected type."""
        defaults = _Defaults()
        target_type = type(getattr(defaults, key, str))
        if target_type is bool:
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        if target_type is int:
            return int(float(raw))
        if target_type is float:
            return float(raw)
        if target_type is tuple:
            return tuple(x.strip() for x in raw.split(","))
        return raw


# --------------------------------------------------------------------------- #
# MODULE-LEVEL INSTANCE                                                     #
# --------------------------------------------------------------------------- #

_config: ConfigRegistry | None = None

def get(key: str = ..., default: Any = ...) -> Any:
    """Module-level convenience access."""
    global _config
    if _config is None:
        _config = ConfigRegistry()
    if key is ...:
        return _config.all()
    return _config.get(key, default)

def reload() -> None:
    """Hot-reload all config values."""
    global _config
    if _config:
        _config.reload()

def version() -> str:
    """Config version hash."""
    global _config
    if _config is None:
        _config = ConfigRegistry()
    return _config.version()


# --------------------------------------------------------------------------- #
# ONBOARDING HELPERS                                                        #
# --------------------------------------------------------------------------- #

def ensure_env() -> list[str]:
    """
    Check which required env vars are missing.  Returns list of missing keys.
    Call at boot — if non-empty, the bot should refuse to start or use defaults.
    """
    required = [
        "DHAN_CLIENT_ID", "DHAN_ACCESS_TOKEN",
        "TG_BOT_TOKEN", "TG_CHAT_ID", "GITHUB_TOKEN", "GITHUB_GIST_ID",
    ]
    missing = [k for k in required if not os.getenv(k)]
    return missing


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    cr = ConfigRegistry()
    print(f"Config version: {cr.version()}")
    missing = ensure_env()
    if missing:
        print(f"Missing env vars: {missing}")
    else:
        print("All required env vars present ✓")
    print(f"ATR_MULTIPLIER: {cr.get('ATR_MULTIPLIER')}")
    print(f"MAX_OPEN_POSITIONS: {cr.get('MAX_OPEN_POSITIONS')}")
