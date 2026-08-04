"""exposure_tracker.py — Phase 9 (9.1): track exposures across open positions."""
from __future__ import annotations
from collections import defaultdict
class ExposureTracker:
    def __init__(self,sector_map=None): self.sector_map={k.upper():v for k,v in (sector_map or {}).items()}
    def _risk(self,p): return abs(p["entry"]-p["sl"])*p["qty"]
    def totals(self,positions):
        ln=sn=tr=0.0; ps=defaultdict(float); pst=defaultdict(float); pse=defaultdict(float)
        for p in positions:
            notional=p["entry"]*p["qty"]; tr+=self._risk(p)
            if p["side"].upper()=="BUY": ln+=notional
            else: sn+=notional
            ps[p["symbol"].upper()]+=notional; pst[p.get("strategy","?")]+=notional
            pse[self.sector_map.get(p["symbol"].upper(),"UNKNOWN")]+=notional
        return {"total_open_risk":round(tr,2),"long_exposure":round(ln,2),"short_exposure":round(sn,2),
        "per_symbol":dict(ps),"per_strategy":dict(pst),"per_sector":dict(pse)}
