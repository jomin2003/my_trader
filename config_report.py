"""config_report.py — Phase 0 (0.2): sanitized startup config dump. Secret-safe, stdlib only."""
from __future__ import annotations
import importlib.util, json, os, re, hashlib
from pathlib import Path
SECRET_ENV=["DHAN_CLIENT_ID","DHAN_ACCESS_TOKEN","DHAN_PIN","DHAN_TOTP_SECRET","TG_BOT_TOKEN",
"TG_CHAT_ID","CRON_SECRET","GITHUB_TOKEN","GIST_TOKEN","GITHUB_GIST_ID","KRONOS_GIST_ID","VOL_GIST_ID","LEDGER_GIST_ID"]
MODEL_FILES=["rr_model.txt","rr_meta.json"]; _TRUE={"1","true","yes","on"}
def _b(n,d): return os.getenv(n,d).strip().lower() in _TRUE
def _lit(base,var,default):
    e=os.getenv(var)
    if e is not None: return e.strip().lower() in _TRUE if isinstance(default,bool) else e
    src=base/"intraday_pattern_scanner_v2.py"
    if not src.exists(): return default
    try:
        m=re.search(rf"^{re.escape(var)}\s*=\s*(True|False|\d+)",src.read_text(errors="ignore"),re.M)
        if m: v=m.group(1); return (v=="True") if v in("True","False") else int(v)
    except Exception: pass
    return default
def _has(m):
    try: return importlib.util.find_spec(m) is not None
    except Exception: return False
def config_version(base_dir=None)->str:
    base=Path(base_dir) if base_dir else Path(__file__).resolve().parent
    keys=["RR_GATE_ENABLED","KRONOS_ENABLED","KRONOS_MODE","KEXIT_ENABLED","VOL_GATE_ENABLED",
          "ALLOC_MODE","EXIT_POLICY_V2_ENABLED","MIN_SL_PCT","MAX_SL_PCT","EXIT_MIN_RR","MAX_RISK_PER_TRADE"]
    blob="|".join(f"{k}={os.getenv(k,'')}" for k in keys)
    lc=base/"live_config.json"
    if lc.exists():
        try: blob+="|lc="+hashlib.sha256(lc.read_bytes()).hexdigest()[:8]
        except Exception: pass
    return "cfg-"+hashlib.sha256(blob.encode()).hexdigest()[:10]
def build_report(base_dir=None)->dict:
    base=Path(base_dir) if base_dir else Path(__file__).resolve().parent
    auto=bool(_lit(base,"AUTO_TRADE_ENABLED",False))
    km=os.getenv("KRONOS_MODE","soft")
    return {"trading_mode":"PAPER" if not auto else "LIVE","auto_trading":auto,
    "dhan_sdk_loaded":_has("dhanhq"),"config_version":config_version(base),
    "components":{"rr_model":{"enabled":_b("RR_GATE_ENABLED","0"),"state":"enabled" if _b("RR_GATE_ENABLED","0") else "bypassed (neutral)"},
    "kronos":{"enabled":_b("KRONOS_ENABLED","true"),"mode":km,"state":(f"enabled, {km} mode" if _b('KRONOS_ENABLED','true') else "bypassed")},
    "vol_gate":{"enabled":_b("VOL_GATE_ENABLED","0"),"state":"enabled" if _b("VOL_GATE_ENABLED","0") else "disabled (neutral 1.0x)"},
    "allocator":{"mode":os.getenv("ALLOC_MODE","shadow"),"state":("active (applied)" if os.getenv("ALLOC_MODE","shadow")=="active" else "shadow (logged, not applied)")},
    "exit_policy_v2":{"enabled":_b("EXIT_POLICY_V2_ENABLED","0"),"state":"v2 authority" if _b("EXIT_POLICY_V2_ENABLED","0") else "legacy path"},
    "audit":{"enabled":_b("AUDIT_ENABLED","1"),"state":"on" if _b("AUDIT_ENABLED","1") else "off"}},
    "secrets":{k:("SET" if os.getenv(k,"").strip() else "MISSING") for k in SECRET_ENV},
    "model_files":{f:(base/f).exists() for f in MODEL_FILES}}
def format_report(rep=None,base_dir=None)->str:
    r=rep or build_report(base_dir); c=r["components"]
    L=["="*52,"CONFIGURATION REPORT (sanitized)","="*52,
    f"Trading mode:       {r['trading_mode']}",
    f"Dhan SDK:           {'loaded' if r['dhan_sdk_loaded'] else 'NOT INSTALLED'}",
    f"RR model:           {c['rr_model']['state']}",f"Kronos:             {c['kronos']['state']}",
    f"Volatility gate:    {c['vol_gate']['state']}",f"Allocator:          {c['allocator']['state']}",
    f"Exit policy:        {c['exit_policy_v2']['state']}",f"Audit layer:        {c['audit']['state']}",
    f"Auto trading:       {'ENABLED' if r['auto_trading'] else 'disabled'}",
    f"Config version:     {r['config_version']}","-"*52,"Secrets (value never shown):"]
    for k,v in r["secrets"].items(): L.append(f"  {k}={v}")
    L.append("-"*52); L.append("Model files:")
    for f,p in r["model_files"].items(): L.append(f"  {f}: {'present' if p else 'MISSING'}")
    L.append("="*52); return "\n".join(L)
def assert_no_secret_values(text):
    for k in SECRET_ENV:
        v=os.getenv(k,"").strip()
        if len(v)>=6 and v in text: raise AssertionError(f"secret {k} leaked")
if __name__=="__main__":
    import argparse; ap=argparse.ArgumentParser(); ap.add_argument("--json",action="store_true"); a=ap.parse_args()
    print(json.dumps(build_report(),indent=2) if a.json else format_report())
