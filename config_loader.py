"""config_loader.py — Phase 3 (3.2): load settings ONCE (base.yaml <- profile <- env)."""
from __future__ import annotations
import os
from functools import lru_cache
from settings import (Settings,ApplicationSettings,MarketSettings,RiskSettings,ExecutionSettings,
StrategySettings,ModelSettings,PersistenceSettings,NotificationSettings)
_TRUE={"1","true","yes","on"}
_CONFIG_DIR=os.getenv("CONFIG_DIR",os.path.join(os.path.dirname(os.path.abspath(__file__)),"configs"))
def _yaml(path):
    try: import yaml
    except Exception: return {}
    if not os.path.exists(path): return {}
    with open(path) as f: return yaml.safe_load(f) or {}
def _merge(a,b):
    o=dict(a)
    for k,v in b.items(): o[k]=_merge(o[k],v) if isinstance(v,dict) and isinstance(o.get(k),dict) else v
    return o
def _b(v,d): return d if v is None else str(v).strip().lower() in _TRUE
_ENV_MAP={"APP_PROFILE":("application","profile",str),"AUDIT_ENABLED":("application","audit_enabled","bool"),
"EXIT_POLICY_V2_ENABLED":("application","exit_policy_v2_enabled","bool"),"AUTO_TRADE_ENABLED":("execution","auto_trade_enabled","bool"),
"MAX_RISK_PER_TRADE":("risk","max_risk_per_trade",float),"MAX_OPEN_POSITIONS":("risk","max_open_positions",int),
"MIN_SL_PCT":("risk","min_sl_pct",float),"MAX_SL_PCT":("risk","max_sl_pct",float),"EXIT_MIN_RR":("risk","risk_reward_ratio",float),
"RR_GATE_ENABLED":("model","rr_gate_enabled","bool"),"RR_MIN_SKILL":("model","rr_min_skill",float),
"KRONOS_ENABLED":("model","kronos_enabled","bool"),"KRONOS_MODE":("model","kronos_mode",str),
"KEXIT_ENABLED":("model","kexit_enabled","bool"),"VOL_GATE_ENABLED":("model","vol_gate_enabled","bool"),
"ALLOC_MODE":("model","alloc_mode",str),"PERSISTENCE_BACKEND":("persistence","backend",str)}
_CLS={"application":ApplicationSettings,"market":MarketSettings,"risk":RiskSettings,"execution":ExecutionSettings,
"strategy":StrategySettings,"model":ModelSettings,"persistence":PersistenceSettings,"notification":NotificationSettings}
def _apply_env(m):
    for env,(sec,fld,cast) in _ENV_MAP.items():
        raw=os.getenv(env)
        if raw is None: continue
        s=m.setdefault(sec,{})
        if cast=="bool": s[fld]=_b(raw,False)
        else:
            try: s[fld]=cast(raw)
            except Exception: pass
    return m
def _build(m):
    kw={}
    for name,cls in _CLS.items():
        sec=m.get(name,{}) or {}; known={f for f in cls.__dataclass_fields__}
        clean={k:v for k,v in sec.items() if k in known}
        if name=="strategy" and isinstance(clean.get("enabled"),list): clean["enabled"]=tuple(clean["enabled"])
        kw[name]=cls(**clean)
    return Settings(**kw)
@lru_cache(maxsize=1)
def load_settings(profile=None):
    prof=profile or os.getenv("APP_PROFILE","paper")
    m=_merge(_yaml(os.path.join(_CONFIG_DIR,"base.yaml")),_yaml(os.path.join(_CONFIG_DIR,f"{prof}.yaml")))
    m.setdefault("application",{})["profile"]=prof; m["application"].setdefault("mode",prof)
    return _build(_apply_env(m))
def reload_settings(profile=None):
    load_settings.cache_clear(); return load_settings(profile)
if __name__=="__main__":
    s=load_settings(); print("profile:",s.application.profile,"version:",s.version())
