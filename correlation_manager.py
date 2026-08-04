"""correlation_manager.py — Phase 9 (9.3): block clustering of correlated names."""
from __future__ import annotations
class CorrelationManager:
    def __init__(self,corr=None,threshold=0.7,max_correlated=3):
        self.corr={}
        for (a,b),v in (corr or {}).items(): self.corr[(a.upper(),b.upper())]=v; self.corr[(b.upper(),a.upper())]=v
        self.threshold=threshold; self.max_correlated=max_correlated
    def rho(self,a,b):
        a,b=a.upper(),b.upper()
        return 1.0 if a==b else self.corr.get((a,b),0.0)
    def correlated_count(self,symbol,open_symbols): return sum(1 for s in open_symbols if self.rho(symbol,s)>=self.threshold)
    def allow(self,symbol,open_symbols):
        n=self.correlated_count(symbol,open_symbols)
        return (False,f"{n} correlated positions (>= {self.max_correlated})") if n>=self.max_correlated else (True,"")
