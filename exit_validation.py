"""exit_validation.py — Phase 2 (Rule 3): reject invalid exit plans. Pure."""
from __future__ import annotations
import math
import reason_codes as RC
from exit_models import ExitConstraints
def _fin(*v): return all(isinstance(x,(int,float)) and math.isfinite(x) for x in v)
def check_finite(e,s,t): return (True,RC.Reason.PLAN_VALID) if _fin(e,s,t) else (False,RC.Reason.NON_FINITE_VALUE)
def check_sides(e,s,t,d):
    if d>0:
        if not s<e: return False,RC.Reason.SL_WRONG_SIDE
        if not t>e: return False,RC.Reason.TGT_WRONG_SIDE
    else:
        if not s>e: return False,RC.Reason.SL_WRONG_SIDE
        if not t<e: return False,RC.Reason.TGT_WRONG_SIDE
    return True,RC.Reason.PLAN_VALID
def check_stop_distance(e,s,c):
    if e<=0: return False,RC.Reason.NON_FINITE_VALUE
    p=abs(e-s)/e
    if p<c.min_sl_pct or p>c.max_sl_pct: return False,RC.Reason.SL_DISTANCE_OUT_OF_LIMITS
    return True,RC.Reason.PLAN_VALID
def check_min_rr(e,s,t,c):
    sd=abs(e-s)
    if sd<=0: return False,RC.Reason.SL_DISTANCE_OUT_OF_LIMITS
    if abs(t-e)/sd<c.min_rr: return False,RC.Reason.RR_BELOW_MIN
    return True,RC.Reason.PLAN_VALID
def check_quantity(q): return (False,RC.Reason.QTY_ZERO) if (q is None or q<=0) else (True,RC.Reason.PLAN_VALID)
def check_max_risk(e,s,q,c):
    if abs(e-s)*max(q,0)>c.max_risk_per_trade+1e-6: return False,RC.Reason.MAX_RISK_EXCEEDED
    return True,RC.Reason.PLAN_VALID
def validate_plan(entry,stop,target,direction,qty=None,constraints=None):
    c=constraints or ExitConstraints.from_env()
    for ok,code in (check_finite(entry,stop,target),check_sides(entry,stop,target,direction),
                    check_stop_distance(entry,stop,c),check_min_rr(entry,stop,target,c)):
        if not ok: return False,[code]
    if qty is not None:
        for ok,code in (check_quantity(qty),check_max_risk(entry,stop,qty,c)):
            if not ok: return False,[code]
    return True,[RC.Reason.PLAN_VALID]
