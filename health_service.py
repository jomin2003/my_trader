"""health_service.py — liveness + readiness (fails if critical deps/config bad).

Single source of truth for configuration is config_registry.py. The legacy
Phase-3 typed-settings stack (settings.py / config_loader.py /
config_validator.py / feature_flags.py) was removed in the 2026-09-23
cleanup because it duplicated config_registry with divergent defaults.
"""
from __future__ import annotations

import importlib.util
import os

CRITICAL_LIBS = ["flask", "pandas", "numpy", "requests"]


def _have(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def liveness() -> dict:
    return {"status": "alive"}


def readiness(base_dir: str = ".") -> tuple[dict, int]:
    problems: list[str] = []
    for lib in CRITICAL_LIBS:
        if not _have(lib):
            problems.append(f"missing lib: {lib}")
    try:
        import config_registry as cfg

        ver = cfg.version()
        # sanity: core trading knobs must resolve to sane values
        for key in ("MAX_RISK_PER_TRADE", "MAX_OPEN_POSITIONS", "MIN_SCORE_TO_TRADE"):
            v = cfg.get(key)
            if v is None:
                problems.append(f"config missing: {key}")
        if os.getenv("RR_GATE_ENABLED", "0") in ("1", "true", "yes"):
            if not os.path.exists(os.getenv("RR_MODEL_PATH", "./rr_model.txt")):
                problems.append("RR enabled but model file missing")
    except Exception as e:  # noqa: BLE001 — readiness must never raise
        problems.append(f"config invalid: {e}")
        ver = "unknown"
    body = {
        "status": "ready" if not problems else "not_ready",
        "problems": problems,
        "config_version": ver,
    }
    return body, 200 if not problems else 503
