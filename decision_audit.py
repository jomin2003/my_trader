"""decision_audit.py — Phase 1 (1.1+1.5): unified decision record + structured log.
Flag AUDIT_ENABLED (default on). AUDIT_ENABLED=0 -> no-op (rollback). Stdlib only."""
from __future__ import annotations
import json,logging,os,threading
from dataclasses import dataclass,field,asdict
from datetime import datetime,timezone,timedelta
from typing import Any,Optional
import reason_codes as RC
IST=timezone(timedelta(hours=5,minutes=30)); log=logging.getLogger("decision_audit")
AUDIT_ENABLED=os.getenv("AUDIT_ENABLED","1").strip().lower() in {"1","true","yes","on"}
AUDIT_RING=int(os.getenv("AUDIT_RING","200"))
def _iso(): return datetime.now(IST).isoformat()
def make_decision_id(strategy,symbol,side,signal_time=None):
    t=signal_time or datetime.now(IST); return f"{strategy}-{symbol}-{t.strftime('%Y%m%dT%H%M%S')}-{side}".upper()
@dataclass
class DecisionRecord:
    decision_id:str; symbol:str; strategy:str; side:str; signal_time:str
    baseline_exit_source:str="unknown"; final_exit_source:str="unknown"; direction_gate:str="unknown"
    rr_model_status:str=RC.RRStatus.DISABLED; kronos_status:str="na"; allocator_mode:str="shadow"
    risk_weight:float=1.0; model_version:Optional[str]=None; config_version:Optional[str]=None
    stop_price:Optional[float]=None; target_price:Optional[float]=None; expected_rr:Optional[float]=None
    qty:Optional[int]=None; outcome:str="evaluated"; reject_reason:Optional[str]=None; reason_codes:list=field(default_factory=list)
    def add_reason(self,*codes):
        for c in codes:
            if c and c not in self.reason_codes: self.reason_codes.append(c)
        return self
    def to_dict(self): return asdict(self)
    def to_json(self): return json.dumps(self.to_dict(),default=str)
    def telegram_line(self):
        rr=self.rr_model_status
        note=(f"RR:{rr}" if rr in (RC.RRStatus.ACCEPTED,RC.RRStatus.DISABLED) else f"RR:{rr}->fallback")
        return (f"🧾 <b>Decision</b> {self.decision_id}\n   exit={self.final_exit_source} · {note} · kronos={self.kronos_status}\n"
                f"   alloc={self.allocator_mode} · risk={self.risk_weight:.2f}x · model={self.model_version or '-'} · cfg={self.config_version or '-'}")
_LOCK=threading.Lock(); _RING=[]; _BY={}
def record(dec):
    if not AUDIT_ENABLED: return dec
    d=dec.to_dict()
    with _LOCK:
        _RING.append(d)
        if len(_RING)>AUDIT_RING: del _RING[:-AUDIT_RING]
        _BY[dec.symbol.upper()]=d
    log_event(RC.Event.EXIT_RESOLVED,decision_id=dec.decision_id,symbol=dec.symbol,
              exit_source=dec.final_exit_source,rr_status=dec.rr_model_status,risk_weight=dec.risk_weight)
    return dec
def last_for_symbol(symbol):
    with _LOCK: return _BY.get(symbol.upper())
def recent(n=20):
    with _LOCK: return list(_RING[-n:])
def clear():
    with _LOCK: _RING.clear(); _BY.clear()
def log_event(event,**f):
    p={"event":event,"ts":_iso(),**f}
    if not AUDIT_ENABLED: return p
    if event not in RC.LOG_EVENTS: p["_unknown_event"]=True
    try: log.info("AUDIT %s",json.dumps(p,default=str))
    except Exception: pass
    return p
def signal_created(**f): return log_event(RC.Event.SIGNAL_CREATED,**f)
def signal_rejected(**f): return log_event(RC.Event.SIGNAL_REJECTED,**f)
def model_prediction(**f): return log_event(RC.Event.MODEL_PREDICTION,**f)
def exit_resolved(**f): return log_event(RC.Event.EXIT_RESOLVED,**f)
def order_submitted(**f): return log_event(RC.Event.ORDER_SUBMITTED,**f)
def order_rejected(**f): return log_event(RC.Event.ORDER_REJECTED,**f)
def position_changed(**f): return log_event(RC.Event.POSITION_CHANGED,**f)
def fallback_invoked(**f): return log_event(RC.Event.FALLBACK_INVOKED,**f)
def state_restored(**f): return log_event(RC.Event.STATE_RESTORED,**f)
