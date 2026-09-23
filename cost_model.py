"""cost_model.py — Phase 5 (5.1): THE single cost model. scanner+backtest import this."""
from __future__ import annotations
from dataclasses import dataclass
@dataclass(frozen=True)
class CostModel:
    slippage_bps:float=3.0; taxes_bps_oneway:float=6.0; brokerage_per_trade:float=0.0; tick_size:float=0.05
    def round_to_tick(self,price):
        if self.tick_size<=0: return round(price,2)
        return round(round(price/self.tick_size)*self.tick_size,2)
    def roundtrip_fraction(self): return 2.0*(self.slippage_bps+self.taxes_bps_oneway)/1e4
    def entry_fill(self,price,side):
        slip=self.slippage_bps/1e4
        return self.round_to_tick(price*(1+slip) if side.upper()=="BUY" else price*(1-slip))
    def exit_fill(self,price,side):
        slip=self.slippage_bps/1e4
        return self.round_to_tick(price*(1-slip) if side.upper()=="BUY" else price*(1+slip))
    def net_pnl(self,entry_px,exit_px,qty,side):
        d=1 if side.upper()=="BUY" else -1; gross=(exit_px-entry_px)*qty*d
        taxes=(entry_px+exit_px)*qty*(self.taxes_bps_oneway/1e4)
        return round(gross-taxes-2*self.brokerage_per_trade,2)
DEFAULT=CostModel()


def apply_costs(entry_px, exit_px, qty, side,
                slippage_bps=3.0, taxes_bps_oneway=6.0, brokerage_per_trade=0.0):
    """Single shared round-trip cost formula.

    Slippage is applied to fill prices (adverse), taxes on traded notional,
    brokerage on both legs. This is the formula previously copy-pasted in
    intraday_pattern_scanner_v2.py and backtest_harness.py — keep it here.
    """
    slip = slippage_bps / 10_000
    if str(side).upper() == "BUY":
        eff_entry = entry_px * (1 + slip)
        eff_exit = exit_px * (1 - slip)
        gross = (eff_exit - eff_entry) * qty
    else:
        eff_entry = entry_px * (1 - slip)
        eff_exit = exit_px * (1 + slip)
        gross = (eff_entry - eff_exit) * qty
    taxes = (entry_px + exit_px) * qty * (taxes_bps_oneway / 10_000)
    return gross - taxes - brokerage_per_trade * 2
