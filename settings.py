"""settings.py — Phase 3 (3.1): typed immutable settings. Loaded once. Pure stdlib."""
from __future__ import annotations
from dataclasses import dataclass,field,asdict
import hashlib,json
@dataclass(frozen=True)
class ApplicationSettings:
    mode:str="paper"; profile:str="paper"; audit_enabled:bool=True; exit_policy_v2_enabled:bool=False; tz:str="Asia/Kolkata"
@dataclass(frozen=True)
class MarketSettings:
    candle_interval_min:int=5; max_stocks:int=180; min_turnover_lakhs:float=25.0
    market_open:str="09:15"; market_close:str="15:30"; scan_start:str="09:30"; no_entry_after:str="14:30"
@dataclass(frozen=True)
class RiskSettings:
    max_risk_per_trade:float=500.0; max_capital_per_trade:float=25000.0; max_open_positions:int=5
    min_score_to_trade:int=6; min_sl_pct:float=0.003; max_sl_pct:float=0.015; risk_reward_ratio:float=2.0
    daily_max_trades:int=0; max_portfolio_heat:float=2000.0
@dataclass(frozen=True)
class ExecutionSettings:
    auto_trade_enabled:bool=False; slippage_bps:int=3; taxes_bps_oneway:int=6; brokerage_per_trade:float=0.0
    request_sleep_sec:float=0.22; position_poll_sec:int=20; fill_policy:str="conservative"
@dataclass(frozen=True)
class StrategySettings:
    enabled:tuple=("OB_SHORT","ORB","GAPFILL","CANDLE_STRUCT"); adx_min_threshold:int=20
@dataclass(frozen=True)
class ModelSettings:
    rr_gate_enabled:bool=False; rr_min_skill:float=0.0; rr_min_pred_r:float=0.0
    rr_model_path:str="./rr_model.txt"; rr_meta_path:str="./rr_meta.json"
    kronos_enabled:bool=True; kronos_mode:str="soft"; kexit_enabled:bool=True; vol_gate_enabled:bool=False; alloc_mode:str="shadow"
@dataclass(frozen=True)
class PersistenceSettings:
    backend:str="gist"; sqlite_path:str="./state.db"; gist_enabled:bool=True
@dataclass(frozen=True)
class NotificationSettings:
    telegram_enabled:bool=True
@dataclass(frozen=True)
class Settings:
    application:ApplicationSettings=field(default_factory=ApplicationSettings)
    market:MarketSettings=field(default_factory=MarketSettings)
    risk:RiskSettings=field(default_factory=RiskSettings)
    execution:ExecutionSettings=field(default_factory=ExecutionSettings)
    strategy:StrategySettings=field(default_factory=StrategySettings)
    model:ModelSettings=field(default_factory=ModelSettings)
    persistence:PersistenceSettings=field(default_factory=PersistenceSettings)
    notification:NotificationSettings=field(default_factory=NotificationSettings)
    def to_dict(self): return asdict(self)
    def version(self):
        return "cfg-"+hashlib.sha256(json.dumps(self.to_dict(),sort_keys=True,default=str).encode()).hexdigest()[:10]
if __name__=="__main__":
    s=Settings(); print("version:",s.version())
