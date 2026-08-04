"""exit_policy.py — Phase 2: THE single exit-policy authority. Flag EXIT_POLICY_V2_ENABLED."""
from __future__ import annotations
import os
from typing import Optional
import reason_codes as RC
from exit_models import ExitProposal,ExitPlan,ExitConstraints,invalid_plan
from exit_validation import validate_plan
EXIT_POLICY_V2_ENABLED=os.getenv("EXIT_POLICY_V2_ENABLED","0").strip().lower() in {"1","true","yes","on"}
def build_baseline_proposal(entry,direction,atr_val,*,struct_sl=None,struct_tgt=None,strat_sl=None,strat_tgt=None,atr_mult=1.5,rr_ratio=2.0):
    if struct_sl is not None and struct_tgt is not None:
        p=ExitProposal(RC.ExitSource.STRUCTURE,float(struct_sl),float(struct_tgt),reason=RC.Reason.EXIT_STRUCTURE_USED)
        if p.sides_ok(entry,direction): return p
    if strat_sl is not None and strat_tgt is not None:
        p=ExitProposal(RC.ExitSource.STRATEGY,float(strat_sl),float(strat_tgt),reason=RC.Reason.EXIT_STRATEGY_USED)
        if p.sides_ok(entry,direction): return p
    if atr_val is None or atr_val<=0: atr_val=entry*0.005
    sd=atr_mult*atr_val
    return ExitProposal(RC.ExitSource.ATR,round(entry-direction*sd,2),round(entry+direction*rr_ratio*sd,2),reason=RC.Reason.EXIT_ATR_FALLBACK)
def build_rr_proposal(entry,direction,atr_val,rr_pick,model_version=None):
    if not rr_pick: return None
    if atr_val is None or atr_val<=0: atr_val=entry*0.005
    try: sd=float(rr_pick["sl_mult"])*atr_val; td=float(rr_pick["tgt_mult"])*atr_val
    except Exception: return None
    conf=rr_pick.get("pred_p",rr_pick.get("pred_r"))
    return ExitProposal(RC.ExitSource.RR_MODEL,round(entry-direction*sd,2),round(entry+direction*td,2),
                        confidence=(float(conf) if conf is not None else None),model_version=model_version,reason=RC.Reason.EXIT_RR_USED)
def apply_kronos_constraint(entry,direction,base,kronos_target,min_rr=1.2):
    if kronos_target is None: return base,"no_view",[RC.Reason.KRONOS_NO_VIEW]
    br=abs(base.target_price-entry); kr=abs(kronos_target-entry)
    if kr>=br: return base,"no_view",[RC.Reason.KRONOS_NO_VIEW]
    sd=base.stop_distance(entry); fr=max(kr,min_rr*sd); nt=round(entry+direction*fr,2)
    return (ExitProposal(base.source,base.stop_price,nt,confidence=base.confidence,model_version=base.model_version,reason=base.reason),
            "constraint_applied",[RC.Reason.KRONOS_CONSTRAINT_APPLIED])
def resolve(decision_id,entry,direction,atr_val,*,baseline,rr_proposal=None,rr_status=RC.RRStatus.DISABLED,kronos_target=None,qty=None,constraints=None):
    c=constraints or ExitConstraints.from_env(); reasons=[]; rejected=[]
    rr_acc=(rr_proposal is not None and rr_status==RC.RRStatus.ACCEPTED and rr_proposal.sides_ok(entry,direction))
    if rr_acc:
        chosen=rr_proposal; reasons+=[RC.Reason.RR_ACCEPTED,RC.Reason.EXIT_RR_USED]; rejected.append(baseline.source)
    else:
        chosen=baseline; reasons.append(baseline.reason)
        if rr_proposal is not None: rejected.append(RC.ExitSource.RR_MODEL)
        if rr_status not in (RC.RRStatus.ACCEPTED,RC.RRStatus.DISABLED): reasons.append(RC.Reason.RR_FALLBACK)
    chosen,kstatus,kr=apply_kronos_constraint(entry,direction,chosen,kronos_target,min_rr=c.min_rr); reasons+=kr
    if rr_acc and kstatus=="constraint_applied": reasons.append(RC.Reason.KRONOS_SKIPPED_RR_OWNS_EXIT)
    ok,vr=validate_plan(entry,chosen.stop_price,chosen.target_price,direction,qty=qty,constraints=c)
    if not ok: return invalid_plan(decision_id,vr[0],rejected=tuple(dict.fromkeys(rejected)))
    reasons+=vr; sd=abs(entry-chosen.stop_price); rr=abs(chosen.target_price-entry)/sd if sd>0 else 0.0
    return ExitPlan(decision_id,chosen.stop_price,chosen.target_price,round(sd,4),round(rr,3),
                    chosen.source,tuple(dict.fromkeys(rejected)),tuple(dict.fromkeys(reasons)),True)
