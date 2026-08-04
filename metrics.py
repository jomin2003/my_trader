"""metrics.py — Phase 10: lightweight in-process metrics."""
from __future__ import annotations
import threading
from collections import defaultdict
_lock=threading.Lock(); _counters=defaultdict(float); _gauges={}
TRACKED=["scan_duration_sec","symbols_processed","market_data_failures","model_acceptance_rate","fallback_rate",
"order_rejection_rate","reconciliation_mismatches","slippage_expected_vs_actual","portfolio_heat",
"daily_realized_pnl","daily_unrealized_pnl","state_save_failures"]
def inc(name,by=1.0):
    with _lock: _counters[name]+=by
def gauge(name,value):
    with _lock: _gauges[name]=value
def snapshot():
    with _lock: return {"counters":dict(_counters),"gauges":dict(_gauges)}
def prometheus_text():
    s=snapshot(); L=[]
    for k,v in s["counters"].items(): L.append(f"# TYPE {k} counter\n{k} {v}")
    for k,v in s["gauges"].items(): L.append(f"# TYPE {k} gauge\n{k} {v}")
    return "\n".join(L)
