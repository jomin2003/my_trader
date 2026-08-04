"""promotion_policy.py — Phase 8: gate promotion on TRADING validation. No training->live."""
from __future__ import annotations
from model_manifest import is_complete,PROMOTION_STATES
MIN_GINI=0.10; MIN_COVERAGE=0.05; MIN_EXPECTANCY_AFTER_COST=0.0; REQUIRE_BASELINE_COMPARISON=True
def can_promote_to_paper(m,live_schema_hash):
    reasons=[]; ok,missing=is_complete(m)
    if not ok: reasons.append(f"manifest incomplete: {missing}")
    if m.get("feature_schema_hash")!=live_schema_hash: reasons.append("feature schema mismatch")
    vm=m.get("validation_metrics",{})
    if float(vm.get("gini",vm.get("skill_vs_mean",0)))<MIN_GINI: reasons.append(f"gini < {MIN_GINI}")
    if float(vm.get("coverage",0))<MIN_COVERAGE: reasons.append(f"coverage < {MIN_COVERAGE}")
    if float(vm.get("expectancy_after_cost",-1))<MIN_EXPECTANCY_AFTER_COST: reasons.append("expectancy after cost < 0")
    if vm.get("walk_forward_pass") is not True: reasons.append("walk-forward did not pass")
    if vm.get("leakage_checks_pass") is not True: reasons.append("leakage checks failed")
    if REQUIRE_BASELINE_COMPARISON and "baseline_comparison" not in vm: reasons.append("no baseline comparison")
    return (len(reasons)==0,reasons)
def can_promote_to_live(m):
    reasons=[]
    if m.get("promotion_state")!="paper-approved": reasons.append("must be paper-approved first (no training->live)")
    return (len(reasons)==0,reasons)
def next_state(cur): i=PROMOTION_STATES.index(cur); return PROMOTION_STATES[min(i+1,len(PROMOTION_STATES)-1)]
