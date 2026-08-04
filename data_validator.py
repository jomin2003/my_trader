"""data_validator.py — Phase 7 (7.3): OHLCV data-quality validation -> quality report."""
from __future__ import annotations
from dataclasses import dataclass,field
import pandas as pd
@dataclass
class QualityReport:
    symbol:str; rows:int=0; issues:list=field(default_factory=list); ok:bool=True
    def add(self,i): self.issues.append(i); self.ok=False
    def as_dict(self): return {"symbol":self.symbol,"rows":self.rows,"ok":self.ok,"issues":self.issues}
def validate_ohlcv(df,symbol="?",interval_min=5):
    r=QualityReport(symbol=symbol,rows=len(df)); cols={c.lower():c for c in df.columns}
    for need in ("open","high","low","close"):
        if need not in cols: r.add(f"missing column {need}")
    if not r.ok: return r
    o,h,l,c=(df[cols[k]] for k in ("open","high","low","close"))
    if (h<l).any(): r.add("high < low rows")
    if (h<o).any() or (h<c).any(): r.add("high < open/close rows")
    if (l>o).any() or (l>c).any(): r.add("low > open/close rows")
    if (c<=0).any() or (o<=0).any(): r.add("non-positive prices")
    if "volume" in cols and (df[cols["volume"]]<0).any(): r.add("negative volume")
    tc=next((cols[k] for k in ("timestamp","ts","datetime","date","time") if k in cols),None)
    if tc is not None:
        ts=pd.to_datetime(df[tc],errors="coerce",utc=True)
        if ts.isna().any(): r.add("unparseable timestamps")
        if not ts.is_monotonic_increasing: r.add("timestamps not sorted")
        if ts.duplicated().any(): r.add("duplicate timestamps")
    if (c.pct_change().abs()>0.40).any(): r.add("extreme per-bar returns (>40%)")
    return r
