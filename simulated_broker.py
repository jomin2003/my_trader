"""simulated_broker.py — Phase 5: paper/backtest broker using shared cost_model. Deterministic."""
from __future__ import annotations
from typing import List
from cost_model import CostModel,DEFAULT as DC
class SimulatedBroker:
    def __init__(self,cost:CostModel=DC): self.cost=cost; self._orders=[]; self._positions=[]; self._oid=0
    def place(self,intent):
        self._oid+=1; oid=f"SIM-{self._oid}"
        fill=self.cost.entry_fill(intent.price or 0.0,intent.side) if intent.price else None
        rec={"order_id":oid,"symbol":intent.symbol,"side":intent.side,"qty":intent.qty,"type":intent.order_type,"status":"COMPLETE","fill_price":fill}
        self._orders.append(rec); return rec
    def cancel(self,order_id): return {"order_id":order_id,"status":"CANCELLED"}
    def positions(self): return list(self._positions)
    def orders(self): return list(self._orders)
