"""reconciler.py — Phase 6 (6.4): startup broker/local reconciliation. Halts on mismatch."""
from __future__ import annotations
from dataclasses import dataclass,field
@dataclass
class ReconcileResult:
    matched:list=field(default_factory=list); orphan_broker:list=field(default_factory=list)
    missing_broker:list=field(default_factory=list); missing_protection:list=field(default_factory=list)
    halt_new_entries:bool=False; reasons:list=field(default_factory=list)
    def summary(self):
        return f"matched={len(self.matched)} orphan_broker={len(self.orphan_broker)} missing_broker={len(self.missing_broker)} missing_protection={len(self.missing_protection)} halt={self.halt_new_entries}"
def reconcile(local_positions,broker_positions,broker_orders):
    r=ReconcileResult()
    lmap={p["symbol"].upper():p for p in local_positions}
    bmap={p["symbol"].upper():p for p in broker_positions if p.get("qty",0)}
    prot={o["symbol"].upper() for o in broker_orders if str(o.get("type","")).upper() in ("SLM","SL","LIMIT")}
    for sym,lp in lmap.items():
        if sym in bmap:
            r.matched.append(sym)
            if sym not in prot: r.missing_protection.append(sym)
        else: r.missing_broker.append(sym)
    for sym in bmap:
        if sym not in lmap: r.orphan_broker.append(sym)
    if r.orphan_broker or r.missing_broker or r.missing_protection:
        r.halt_new_entries=True
        if r.orphan_broker: r.reasons.append(f"orphan broker positions: {r.orphan_broker}")
        if r.missing_broker: r.reasons.append(f"local positions missing at broker: {r.missing_broker}")
        if r.missing_protection: r.reasons.append(f"positions without protective orders: {r.missing_protection}")
    return r
