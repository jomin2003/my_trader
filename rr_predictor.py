"""rr_predictor.py — Tier-3 live reader (v3 OBSERVABLE). best_rr_ex() returns
(pick,status,reasons). best_rr() byte-compatible. OFF by default. Never raises."""
from __future__ import annotations
import json,os
from typing import Dict,List,Optional,Tuple
import rr_features, reason_codes as RC
try:
    import model_diagnostics as _MD; _COV=_MD.COVERAGE
except Exception: _COV=None
MODEL_PATH=os.environ.get("RR_MODEL_PATH","./rr_model.txt"); META_PATH=os.environ.get("RR_META_PATH","./rr_meta.json")
MIN_SKILL=float(os.environ.get("RR_MIN_SKILL","0.0")); MIN_PRED_R=float(os.environ.get("RR_MIN_PRED_R","0.0"))
_ENABLED=os.environ.get("RR_GATE_ENABLED","0")=="1"
_model=None; _meta={}; _features=[]; _sl_grid=[]; _tgt_grid=[]; _is_winprob=False; _cost_frac=0.0
_loaded=False; _ok=False; _schema="UNKNOWN"; _load_status=RC.RRStatus.DISABLED
def _load():
    global _model,_meta,_features,_sl_grid,_tgt_grid,_is_winprob,_cost_frac,_loaded,_ok,_schema,_load_status
    _loaded=True; _ok=False
    if not _ENABLED: _load_status=RC.RRStatus.DISABLED; return
    if not os.path.exists(META_PATH): _load_status=RC.RRStatus.META_NOT_FOUND; return
    if not os.path.exists(MODEL_PATH): _load_status=RC.RRStatus.MODEL_NOT_FOUND; return
    try: _meta=json.load(open(META_PATH))
    except Exception: _load_status=RC.RRStatus.META_NOT_FOUND; return
    if _meta.get("model")!="lightgbm": _schema="SCHEMA_MISMATCH"; _load_status=RC.RRStatus.SCHEMA_MISMATCH; return
    _features=_meta.get("features",[]); _sl_grid=_meta.get("sl_grid",[]); _tgt_grid=_meta.get("tgt_grid",[])
    if not (_features and all(f in _features for f in rr_features.BASE_FEATURES) and _sl_grid and _tgt_grid):
        _schema="SCHEMA_MISMATCH"; _load_status=RC.RRStatus.SCHEMA_MISMATCH; return
    _schema="VALID"
    if float(_meta.get("skill_vs_mean",0.0))<=MIN_SKILL: _load_status=RC.RRStatus.LOW_CONFIDENCE
    _is_winprob=(_meta.get("objective")=="binary_winprob"); cm=_meta.get("cost_model",{})
    _cost_frac=2.0*(float(cm.get("slippage_bps",0))+float(cm.get("taxes_bps_oneway",0)))/1e4
    try: import lightgbm as lgb
    except Exception: _load_status=RC.RRStatus.LIGHTGBM_NOT_AVAILABLE; return
    try: _model=lgb.Booster(model_file=MODEL_PATH)
    except Exception: _load_status=RC.RRStatus.PREDICTION_ERROR; return
    if float(_meta.get("skill_vs_mean",0.0))>MIN_SKILL: _ok=True; _load_status=RC.RRStatus.ACCEPTED
def _ensure():
    if not _loaded: _load()
def _ready():
    if not _ENABLED: return False
    _ensure(); return _ok and _model is not None
def enabled(): return _ready()
def _cost_r(af,sl):
    if not _cost_frac or af is None or af<=0 or sl<=0: return 0.0
    return (_cost_frac/af)/sl
def _mv():
    if not _meta: return "-"
    return f"rr-{'winprob' if _meta.get('objective')=='binary_winprob' else 'netR'}-{str(_meta.get('generated_utc',''))[:10]}-s{_meta.get('skill_vs_mean','?')}"
def best_rr_ex(feat,side=1)->Tuple[Optional[dict],str,List[str]]:
    _ensure()
    if _COV: _COV.on_attempt()
    if not _ENABLED: return None,RC.RRStatus.DISABLED,[RC.Reason.RR_DISABLED]
    if _load_status in (RC.RRStatus.MODEL_NOT_FOUND,RC.RRStatus.META_NOT_FOUND,RC.RRStatus.LIGHTGBM_NOT_AVAILABLE,RC.RRStatus.SCHEMA_MISMATCH):
        if _COV: _COV.on_fallback(_load_status)
        return None,_load_status,[RC.Reason.RR_SCHEMA_INVALID if _load_status==RC.RRStatus.SCHEMA_MISMATCH else RC.Reason.RR_FALLBACK]
    if not _ok or _model is None:
        if _COV: _COV.on_fallback(RC.RRStatus.LOW_CONFIDENCE)
        return None,RC.RRStatus.LOW_CONFIDENCE,[RC.Reason.RR_CONFIDENCE_LOW,RC.Reason.RR_FALLBACK]
    if not feat:
        if _COV: _COV.on_fallback(RC.RRStatus.INSUFFICIENT_BARS)
        return None,RC.RRStatus.INSUFFICIENT_BARS,[RC.Reason.RR_INSUFFICIENT_BARS,RC.Reason.RR_FALLBACK]
    for v in feat.values():
        try: fv=float(v)
        except Exception: fv=float("nan")
        if fv!=fv or fv in (float("inf"),float("-inf")):
            if _COV: _COV.on_fallback(RC.RRStatus.INVALID_FEATURE)
            return None,RC.RRStatus.INVALID_FEATURE,[RC.Reason.NON_FINITE_VALUE,RC.Reason.RR_FALLBACK]
    try:
        af=float(feat.get("atr_frac",0.0)); batch=[]; combos=[]
        for sl in _sl_grid:
            for tg in _tgt_grid:
                if tg<=sl*0.6: continue
                batch.append([sl if f=="sl_mult" else tg if f=="tgt_mult" else float(feat.get(f,0.0)) for f in _features]); combos.append((sl,tg))
        if not batch:
            if _COV: _COV.on_fallback(RC.RRStatus.OUT_OF_RANGE)
            return None,RC.RRStatus.OUT_OF_RANGE,[RC.Reason.RR_FALLBACK]
        preds=_model.predict(batch)
    except Exception:
        if _COV: _COV.on_fallback(RC.RRStatus.PREDICTION_ERROR)
        return None,RC.RRStatus.PREDICTION_ERROR,[RC.Reason.RR_FALLBACK]
    best=None; bs=-1e18; bp=None
    for (sl,tg),pr in zip(combos,preds):
        if _is_winprob: p=float(pr); score=p*(tg/sl)-(1.0-p)-_cost_r(af,sl)
        else: p=None; score=float(pr)
        if score>bs: bs,best,bp=score,(sl,tg),p
    if best is None or bs<MIN_PRED_R:
        if _COV: _COV.on_fallback(RC.RRStatus.OUT_OF_RANGE)
        return None,RC.RRStatus.OUT_OF_RANGE,[RC.Reason.RR_CONFIDENCE_LOW,RC.Reason.RR_FALLBACK]
    pick={"sl_mult":best[0],"tgt_mult":best[1],"pred_r":round(bs,4)}
    if bp is not None: pick["pred_p"]=round(bp,4)
    return pick,RC.RRStatus.ACCEPTED,[RC.Reason.RR_SCHEMA_VALID,RC.Reason.RR_CONFIDENCE_PASSED,RC.Reason.RR_ACCEPTED]
def best_rr(feat,side=1):
    p,_s,_r=best_rr_ex(feat,side); return p
def features_from_ohlc(df):
    try: return rr_features.latest_features(df)
    except Exception as e: print(f"[rr_predictor] feature build failed ({e})"); return None
def diagnostics():
    _ensure()
    return {"loaded":bool(_meta) or _model is not None,"enabled":_ENABLED,"usable":_ok,"schema":_schema,
            "status":_load_status,"model_version":_mv(),"skill":_meta.get("skill_vs_mean") if _meta else None,
            "objective":_meta.get("objective") if _meta else None}
def rr_summary():
    d=diagnostics(); st="ON" if d["enabled"] else "OFF"
    if not _meta: return f"RR[{st}]: no model"
    return f"RR[{st}]: {d['objective']} skill={d['skill']} schema={d['schema']} {'ready' if d['usable'] else d['status']}"
if __name__=="__main__":
    os.environ["RR_GATE_ENABLED"]="1"; _ENABLED=True; _load(); print(rr_summary()); print("diagnostics:",diagnostics())
