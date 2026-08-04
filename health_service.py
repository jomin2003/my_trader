"""health_service.py — Phase 10: liveness + readiness (fails if critical deps/config bad)."""
from __future__ import annotations
import importlib.util,os
CRITICAL_LIBS=["flask","pandas","numpy","requests"]
def _have(m):
    try: return importlib.util.find_spec(m) is not None
    except Exception: return False
def liveness(): return {"status":"alive"}
def readiness(base_dir="."):
    problems=[]
    for lib in CRITICAL_LIBS:
        if not _have(lib): problems.append(f"missing lib: {lib}")
    try:
        from config_loader import load_settings
        import config_validator as V
        s=load_settings(); V.validate(s,base_dir=base_dir); cfg=s.version()
    except Exception as e: problems.append(f"config invalid: {e}"); cfg="unknown"
    if os.getenv("RR_GATE_ENABLED","0") in ("1","true","yes"):
        if not os.path.exists(os.getenv("RR_MODEL_PATH","./rr_model.txt")): problems.append("RR enabled but model file missing")
    return ({"status":"ready" if not problems else "not_ready","problems":problems,"config_version":cfg},200 if not problems else 503)
