"""config_validator.py — Phase 3 (3.3): fail-fast on unsafe combos."""
from __future__ import annotations
import os
from settings import Settings
PLACEHOLDER={"","YOUR_CLIENT_ID","YOUR_ACCESS_TOKEN","YOUR_TELEGRAM_BOT_TOKEN","YOUR_TELEGRAM_CHAT_ID","changeme","placeholder"}
class ConfigError(RuntimeError): pass
def _ph(name): return os.getenv(name,"").strip() in PLACEHOLDER
def _exists(p): return bool(p) and os.path.exists(p)
def validate(s:Settings,*,base_dir=".",persistence_available=None):
    errors=[]; passed=[]
    live=(s.application.mode=="live"); auto=s.execution.auto_trade_enabled
    if live and auto:
        for c in ("DHAN_CLIENT_ID","DHAN_ACCESS_TOKEN"):
            if _ph(c): errors.append(f"LIVE+AUTO_TRADE but {c} missing/placeholder")
        passed.append("live_credentials_checked")
    if s.model.rr_gate_enabled:
        mp=s.model.rr_model_path if _exists(s.model.rr_model_path) else os.path.join(base_dir,"rr_model.txt")
        meta=s.model.rr_meta_path if _exists(s.model.rr_meta_path) else os.path.join(base_dir,"rr_meta.json")
        if not _exists(mp): errors.append("RR enabled but rr_model.txt missing")
        if not _exists(meta): errors.append("RR enabled but rr_meta.json missing")
        else:
            try:
                import json,rr_features
                m=json.load(open(meta)); feats=m.get("features",[])
                if not all(f in feats for f in rr_features.BASE_FEATURES): errors.append("RR enabled but feature schema mismatch")
            except Exception as e: errors.append(f"RR meta unreadable: {e}")
        passed.append("rr_artifacts_checked")
    if s.application.exit_policy_v2_enabled and s.model.kexit_enabled:
        if os.getenv("KEXIT_CONSTRAINT_ONLY","1").strip().lower() not in _true():
            errors.append("two exit authorities active (set KEXIT_CONSTRAINT_ONLY=1)")
    passed.append("single_exit_authority_checked")
    if live and persistence_available is False: errors.append("live mode but persistence unavailable")
    passed.append("persistence_checked")
    r=s.risk
    if not (0<r.min_sl_pct<r.max_sl_pct<0.2): errors.append(f"risk: SL pct range invalid ({r.min_sl_pct}..{r.max_sl_pct})")
    if r.max_risk_per_trade<=0 or r.max_risk_per_trade>1e7: errors.append(f"risk: max_risk_per_trade out of range")
    if r.max_open_positions<=0 or r.max_open_positions>100: errors.append(f"risk: max_open_positions out of range")
    if r.risk_reward_ratio<=0: errors.append("risk: risk_reward_ratio must be > 0")
    passed.append("risk_ranges_checked")
    if errors: raise ConfigError("Startup config validation FAILED:\n  - "+"\n  - ".join(errors))
    return passed
def _true(): return {"1","true","yes","on"}
if __name__=="__main__":
    from config_loader import load_settings
    s=load_settings()
    try: print("config OK. passed:",validate(s),"| version:",s.version())
    except ConfigError as e: print(e); raise SystemExit(1)
