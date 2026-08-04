"""fill_model.py — Phase 5 (5.2): same-candle SL/TGT policy. Recorded in backtest metadata."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
@dataclass(frozen=True)
class FillResult:
    outcome:str; price:Optional[float]; policy:str
def resolve_same_bar(direction,entry,sl,tgt,bar_high,bar_low,policy="conservative"):
    if direction>0: hit_sl=bar_low<=sl; hit_tgt=bar_high>=tgt
    else: hit_sl=bar_high>=sl; hit_tgt=bar_low<=tgt
    if hit_sl and hit_tgt:
        return FillResult("TGT",tgt,policy) if policy=="optimistic" else FillResult("SL",sl,policy)
    if hit_tgt: return FillResult("TGT",tgt,policy)
    if hit_sl: return FillResult("SL",sl,policy)
    return FillResult("NONE",None,policy)
