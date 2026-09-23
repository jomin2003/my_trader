"""portfolio_risk.py — Phase 9 (9.2): portfolio heat + pre-entry risk gate."""
from __future__ import annotations
from dataclasses import dataclass
from exposure_tracker import ExposureTracker
@dataclass(frozen=True)
class RiskLimits:
    max_portfolio_heat:float=2000.0; max_symbol_notional:float=50000.0; max_sector_notional:float=150000.0; max_correlated:int=3
def portfolio_heat(positions): return round(sum(abs(p["entry"]-p["sl"])*p["qty"] for p in positions),2)
def check_new_entry(candidate,positions,limits,tracker=None):
    reasons=[]; tracker=tracker or ExposureTracker()
    cr=abs(candidate["entry"]-candidate["sl"])*candidate["qty"]; nh=portfolio_heat(positions)+cr
    if nh>limits.max_portfolio_heat: reasons.append(f"portfolio heat {nh:.0f} > {limits.max_portfolio_heat:.0f}")
    tot=tracker.totals(positions+[candidate|{"side":candidate.get("side","BUY")}]); sym=candidate["symbol"].upper()
    if tot["per_symbol"].get(sym,0)>limits.max_symbol_notional: reasons.append(f"symbol notional over limit for {sym}")
    sec=tracker.sector_map.get(sym,"UNKNOWN")
    if tot["per_sector"].get(sec,0)>limits.max_sector_notional: reasons.append(f"sector notional over limit for {sec}")
    return (len(reasons)==0,reasons)
