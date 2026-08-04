"""exit_models.py — Phase 2 (2.1): typed immutable exit contracts."""
from __future__ import annotations
from dataclasses import dataclass,replace
from typing import Optional
@dataclass(frozen=True)
class ExitProposal:
    source:str; stop_price:float; target_price:float
    confidence:Optional[float]=None; model_version:Optional[str]=None; reason:str=""
    def stop_distance(self,e): return abs(e-self.stop_price)
    def reward_distance(self,e): return abs(self.target_price-e)
    def rr(self,e):
        sd=self.stop_distance(e); return (self.reward_distance(e)/sd) if sd>0 else 0.0
    def sides_ok(self,e,d):
        return (self.stop_price<e<self.target_price) if d>0 else (self.target_price<e<self.stop_price)
@dataclass(frozen=True)
class ExitPlan:
    decision_id:str; stop_price:float; target_price:float; initial_risk_per_unit:float
    expected_rr:float; chosen_source:str; rejected_sources:tuple=(); reason_codes:tuple=()
    valid:bool=True; reject_reason:Optional[str]=None
    def as_dict(self):
        return {"decision_id":self.decision_id,"stop_price":self.stop_price,"target_price":self.target_price,
        "initial_risk_per_unit":self.initial_risk_per_unit,"expected_rr":self.expected_rr,
        "chosen_source":self.chosen_source,"rejected_sources":list(self.rejected_sources),
        "reason_codes":list(self.reason_codes),"valid":self.valid,"reject_reason":self.reject_reason}
@dataclass(frozen=True)
class ExitConstraints:
    min_sl_pct:float=0.003; max_sl_pct:float=0.015; min_rr:float=1.2; max_risk_per_trade:float=500.0
    @classmethod
    def from_env(cls,**ov):
        import os
        def _f(n,d):
            try: return float(os.getenv(n,str(d)))
            except Exception: return d
        b=cls(_f("MIN_SL_PCT",0.003),_f("MAX_SL_PCT",0.015),_f("EXIT_MIN_RR",1.2),_f("MAX_RISK_PER_TRADE",500.0))
        return replace(b,**ov) if ov else b
def invalid_plan(did,code,rejected=()):
    return ExitPlan(did,float("nan"),float("nan"),0.0,0.0,"none",rejected,(code,),False,code)
