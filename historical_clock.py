"""historical_clock.py — Phase 5 (5.3): replay clock. Future is inaccessible (no lookahead)."""
from __future__ import annotations
class HistoricalClock:
    def __init__(self,timestamps): self._ts=list(timestamps); self._i=-1
    def advance(self):
        if self._i+1<len(self._ts): self._i+=1; return self._ts[self._i]
        return None
    def now(self): return self._ts[self._i] if self._i>=0 else None
    def is_market_open(self): return 0<=self._i<len(self._ts)
    def remaining(self): return max(0,len(self._ts)-self._i-1)
