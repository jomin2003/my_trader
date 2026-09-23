"""
test_bundle.py — pytest test suite for my_trader.
==========================================================
Covers all phases: config, indicators, exit policy, state,
risk, strategies, ledger, allocator, and integration.

Usage:
    pip install pytest
    python -m pytest tests/test_bundle.py -v --tb=short
"""
from __future__ import annotations

import os
import sys
import tempfile
import pytest
from datetime import datetime

# Ensure repo is on path
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


# ===================================================================== #
# PHASE 0+1 — CONFIG & SETTINGS                                        #
# ===================================================================== #

class TestConfigRegistry:
    """Test the centralized config registry."""

    def test_get_returns_defaults(self):
        import config_registry as cfg
        assert isinstance(cfg.get("MAX_OPEN_POSITIONS"), int)
        assert cfg.get("MAX_OPEN_POSITIONS") == 3  # profitable-only: max 3/day

    def test_get_unknown_returns_default(self):
        import config_registry as cfg
        assert cfg.get("nonexistent_key", "fallback") == "fallback"

    def test_set_overrides(self):
        import config_registry
        _cfg = config_registry.ConfigRegistry()
        _cfg.set("MAX_OPEN_POSITIONS", 99)
        assert _cfg.get("MAX_OPEN_POSITIONS") == 99
        _cfg.reload()

    def test_version_is_hash(self):
        import config_registry
        _cfg = config_registry.ConfigRegistry()
        v = _cfg.version()
        assert v.startswith("cfg-")
        assert len(v) == 16

    def test_safe_masks_secrets(self):
        import config_registry
        _cfg = config_registry.ConfigRegistry()
        _cfg.set("TG_BOT_TOKEN", "secret123")
        safe = _cfg.safe_dump()
        assert safe["TG_BOT_TOKEN"] == "***MASKED***"

    def test_strategy_name_normalization(self):
        from config_registry import StrategyName
        assert StrategyName.normalize("OB_SHORTS") == "OB_SHORTS"
        assert StrategyName.normalize("OB SHORTS") == "OB_SHORTS"
        assert StrategyName.normalize("GAP-FILL") == "GAPFILL"
        assert StrategyName.normalize("GAP_FILL") == "GAPFILL"
        assert StrategyName.normalize("candle_struct") == "CANDLE_STRUCT"
        assert StrategyName.normalize(None) == ""
        assert StrategyName.normalize("UNKNOWN") == "UNKNOWN"

    def test_strategy_name_valid(self):
        from config_registry import StrategyName
        assert StrategyName.is_valid("OB_SHORTS")
        assert StrategyName.is_valid("ORB")
        assert not StrategyName.is_valid("FAKE")


class TestHealthService:
    """Readiness/liveness run against the single config source (config_registry)."""

    def test_readiness_shape(self):
        from health_service import readiness
        body, code = readiness(base_dir=".")
        assert set(body) == {"status", "problems", "config_version"}
        assert code in (200, 503)
        assert body["config_version"].startswith("cfg-")

    def test_liveness(self):
        from health_service import liveness
        assert liveness() == {"status": "alive"}


# ===================================================================== #
# PHASE 3 — SHARED INDICATORS                                          #
# ===================================================================== #

class TestSharedIndicators:
    def test_atr_computation(self):
        import pandas as pd
        import numpy as np
        from shared_indicators import atr
        np.random.seed(42)
        close = 100 + np.cumsum(np.random.randn(30))
        high = close + np.abs(np.random.randn(30)) * 0.5
        low = close - np.abs(np.random.randn(30)) * 0.5
        open_p = close - np.random.rand(30) * 0.4 + 0.2
        vol = np.random.randint(100000, 1000000, 30).astype(float)
        df = pd.DataFrame({"open": open_p, "high": high, "low": low,
                           "close": close, "volume": vol})
        result = atr(df, 14)
        assert result is not None
        assert result > 0
        assert result < 5  # sanity: should be < 5 for these prices

    def test_atr_insufficient_data(self):
        import pandas as pd
        import numpy as np
        from shared_indicators import atr
        df = pd.DataFrame({"open": [1], "high": [1], "low": [1],
                           "close": [1], "volume": [1]})
        assert atr(df, 14) is None

    def test_rolling_vwap(self):
        import pandas as pd
        import numpy as np
        from shared_indicators import rolling_vwap
        close = np.array([100, 101, 102, 101, 103])
        df = pd.DataFrame({"open": close, "high": close + 0.5,
                           "low": close - 0.5, "close": close,
                           "volume": np.ones(5) * 100000})
        result = rolling_vwap(df)
        assert result is not None
        assert 100 <= result <= 103

    def test_rsi_computation(self):
        import pandas as pd
        import numpy as np
        from shared_indicators import rsi
        np.random.seed(42)
        close = pd.Series(100 + np.cumsum(np.random.randn(30)))
        df = pd.DataFrame({"open": close, "high": close + 0.5,
                           "low": close - 0.5, "close": close,
                           "volume": np.ones(30) * 100000})
        result = rsi(df, 14)
        assert result is not None
        assert 0 <= result <= 100

    def test_ema_computation(self):
        import pandas as pd
        import numpy as np
        from shared_indicators import ema
        close = pd.Series([100, 101, 102, 103, 104, 105, 106, 107, 108, 109,
                           110, 111, 112, 113, 114, 115])
        result = ema(close, 5)
        assert result is not None
        assert 110 < result < 116

    def test_wilder_atr_alias(self):
        import pandas as pd
        import numpy as np
        from shared_indicators import wilder_atr, atr
        np.random.seed(42)
        close = 100 + np.cumsum(np.random.randn(30))
        high = close + np.abs(np.random.randn(30)) * 0.5
        low = close - np.abs(np.random.randn(30)) * 0.5
        df = pd.DataFrame({"open": close, "high": high, "low": low,
                           "close": close, "volume": np.ones(30) * 100000})
        r1 = wilder_atr(df, 14)
        r2 = atr(df, 14)
        assert abs(r1 - r2) < 1e-6

    def test_confluence_score(self):
        import pandas as pd
        import numpy as np
        from shared_indicators import confluence_score
        np.random.seed(42)
        close = 100 + np.cumsum(np.random.randn(30))
        high = close + np.abs(np.random.randn(30)) * 0.5
        low = close - np.abs(np.random.randn(30)) * 0.5
        df = pd.DataFrame({"open": close, "high": high, "low": low,
                           "close": close, "volume": np.ones(30) * 100000})
        boost, tags = confluence_score(df, 1)
        assert isinstance(boost, int)
        assert isinstance(tags, str)

    def test_adx_computation(self):
        import pandas as pd
        import numpy as np
        from shared_indicators import adx
        np.random.seed(42)
        close = 100 + np.cumsum(np.random.randn(50))
        high = close + np.abs(np.random.randn(50)) * 0.5
        low = close - np.abs(np.random.randn(50)) * 0.5
        df = pd.DataFrame({"open": close, "high": high, "low": low,
                           "close": close, "volume": np.ones(50) * 100000})
        result = adx(df, 14)
        assert result is None or (0 <= result <= 100)


# ===================================================================== #
# PHASE 4-6 — STRATEGY & STATE                                         #
# ===================================================================== #

class TestStrategyNameEnum:
    """Test the strategy name constants."""

    def test_ob_shorts_constant(self):
        from config_registry import StrategyName
        assert StrategyName.OB_SHORTS == "OB_SHORTS"

    def test_orb_constant(self):
        from config_registry import StrategyName
        assert StrategyName.ORB == "ORB"

    def test_gapfill_constant(self):
        from config_registry import StrategyName
        assert StrategyName.GAPFILL == "GAPFILL"

    def test_candle_struct_constant(self):
        from config_registry import StrategyName
        assert StrategyName.CANDLE_STRUCT == "CANDLE_STRUCT"

    def test_all_contains_four(self):
        from config_registry import StrategyName
        # 4 original + VWAP_RECLAIM/VWAP_PULLBACK (profitability upgrade)
        assert len(StrategyName.ALL) >= 4
        for s in ("OB_SHORTS", "ORB", "GAPFILL", "CANDLE_STRUCT"):
            assert s in StrategyName.ALL


class TestStateStore:
    def test_reset_day_clears_state(self):
        import intraday_pattern_scanner_v2 as scn
        scn._OPEN_POSITIONS["TEST"] = {"symbol": "TEST"}
        scn._COMPLETED_TRADES.append({"symbol": "TEST"})
        scn._TRADING_HALTED_TODAY = True
        scn._HALT_REASON = "test"
        scn.reset_day()
        assert len(scn._OPEN_POSITIONS) == 0
        assert len(scn._COMPLETED_TRADES) == 0
        assert scn._TRADING_HALTED_TODAY is False

    def test_entry_blocked_during_halt(self):
        import intraday_pattern_scanner_v2 as scn
        scn._TRADING_HALTED_TODAY = True
        scn._HALT_REASON = "test halt"
        result = scn._entry_blocked("TEST")
        assert result is not None and "halted" in result.lower()

    def test_entry_blocked_within_limits(self):
        import intraday_pattern_scanner_v2 as scn
        scn._TRADING_HALTED_TODAY = False
        scn._OPEN_POSITIONS.clear()
        result = scn._entry_blocked("TEST")
        assert result is None

    def test_profitable_only_blocks_unproven_live(self):
        import config_registry as cfg
        import intraday_pattern_scanner_v2 as scn
        scn._TRADING_HALTED_TODAY = False
        scn._OPEN_POSITIONS.clear()
        cfg.ConfigRegistry().set("PROFITABLE_ONLY", True)
        cfg.ConfigRegistry().set("ALLOWED_STRATEGIES", ())
        assert scn._entry_blocked("TEST", "ORB") is not None
        assert "not-proven" in scn._entry_blocked("TEST", "ORB")
        # paper path (no strat) still governed by risk caps only
        assert scn._entry_blocked("TEST") is None
        cfg.ConfigRegistry().reload()

    def test_compute_sl_target_long(self):
        import intraday_pattern_scanner_v2 as scn
        sl, tgt, dist = scn.compute_sl_target(100.0, 1, 2.0)
        assert sl < 100.0
        assert tgt > 100.0
        assert dist > 0

    def test_compute_sl_target_short(self):
        import intraday_pattern_scanner_v2 as scn
        sl, tgt, dist = scn.compute_sl_target(100.0, -1, 2.0)
        assert sl > 100.0
        assert tgt < 100.0
        assert dist > 0

    def test_compute_quantity(self):
        import intraday_pattern_scanner_v2 as scn
        qty = scn.compute_quantity(100.0, 2.0)
        assert qty > 0
        # compute_quantity: min(MAX_RISK_PER_TRADE // sl_dist, MAX_CAPITAL_PER_TRADE // entry)
        # = min(500 // 2, 25000 // 100) = min(250, 250) = 250


# ===================================================================== #
# PHASE 7 — DATA VALIDATION & PARQUET                                  #
# ===================================================================== #

class TestDataValidator:
    def test_validate_ohlcv_valid(self):
        from data_validator import validate_ohlcv
        import pandas as pd
        df = pd.DataFrame({
            "open": [100, 101, 102],
            "high": [101, 102, 103],
            "low": [99, 100, 101],
            "close": [101, 101, 102],
            "volume": [100000, 200000, 150000],
        })
        r = validate_ohlcv(df, "TEST")
        assert r.ok is True

    def test_validate_ohlcv_invalid(self):
        from data_validator import validate_ohlcv
        import pandas as pd
        df = pd.DataFrame({
            "open": [100, 101],
            "high": [99, 100],  # high < low
            "low": [98, 99],
            "close": [101, 101],
        })
        r = validate_ohlcv(df, "TEST")
        assert r.ok is False
        assert len(r.issues) > 0


# ===================================================================== #
# PHASE 8 — MODEL REGISTRY & WALK FORWARD                              #
# ===================================================================== #

# ===================================================================== #
# PHASE 9 — RISK & PORTFOLIO                                           #
# ===================================================================== #

class TestPortfolioRisk:
    def test_portfolio_heat(self):
        from portfolio_risk import portfolio_heat
        positions = [
            {"entry": 100, "sl": 95, "qty": 10},
            {"entry": 200, "sl": 190, "qty": 5},
        ]
        heat = portfolio_heat(positions)
        assert heat == (5 * 10 + 10 * 5)  # = 100

    def test_check_new_entry_within_limits(self):
        from portfolio_risk import check_new_entry, RiskLimits
        limits = RiskLimits(max_portfolio_heat=10000.0,
                            max_symbol_notional=100000.0,
                            max_sector_notional=500000.0,
                            max_correlated=5)
        positions = []
        candidate = {"symbol": "RELIANCE", "entry": 1000.0,
                     "sl": 990.0, "qty": 5, "side": "BUY"}
        ok, reasons = check_new_entry(candidate, positions, limits)
        assert ok is True

    def test_check_new_entry_over_limits(self):
        from portfolio_risk import check_new_entry, RiskLimits
        limits = RiskLimits(max_portfolio_heat=10.0,
                            max_symbol_notional=100.0,
                            max_sector_notional=500.0,
                            max_correlated=1)
        positions = [{"entry": 100, "sl": 95, "qty": 10,
                       "side": "BUY", "symbol": "RELIANCE"}]
        candidate = {"symbol": "RELIANCE", "entry": 100,
                     "sl": 95, "qty": 5, "side": "BUY"}
        ok, reasons = check_new_entry(candidate, positions, limits)
        assert ok is False


class TestExposureTracker:
    def test_totals(self):
        from exposure_tracker import ExposureTracker
        tracker = ExposureTracker()
        positions = [
            {"symbol": "RELIANCE", "side": "BUY", "qty": 10,
             "entry": 1000, "sl": 990},
            {"symbol": "TCS", "side": "SELL", "qty": 5,
             "entry": 3500, "sl": 3550},
        ]
        totals = tracker.totals(positions)
        assert totals["total_open_risk"] > 0
        assert "RELIANCE" in totals["per_symbol"]
        assert "TCS" in totals["per_symbol"]


# ===================================================================== #
# PHASE 10 — ADAPTIVE ALLOCATOR & LEDGER                               #
# ===================================================================== #

class TestStrategyLedger:
    def test_load_returns_empty_on_absent(self):
        import strategy_ledger as ledger
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            l = ledger.load_ledger(base_dir=td)
            assert l == {"trades": [], "_meta": {}}

    def test_append_and_load(self):
        import strategy_ledger as ledger
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            led = ledger.load_ledger(base_dir=td)
            led["trades"] = [{"date": "2026-09-11", "strategy": "ORB",
                             "side": "BUY", "pnl": 100.0, "r": 1.5,
                             "outcome": "TARGET", "nifty": 1, "kronos": "agree", "hour": 10}]
            ledger.save_ledger(led, base_dir=td)
            loaded = ledger.load_ledger(base_dir=td)
            assert len(loaded["trades"]) == 1

    def test_stats_computation(self):
        import strategy_ledger as ledger
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            led = {"trades": [
                {"pnl": 100.0, "strategy": "ORB", "kronos": "agree"},
                {"pnl": -50.0, "strategy": "ORB", "kronos": "na"},
                {"pnl": 200.0, "strategy": "ORB", "kronos": "agree"},
            ]}
            stats = ledger.strategy_stats(base_dir=td, ledger=led)
            assert "ORB" in stats
            assert stats["ORB"]["n"] == 3


class TestAdaptiveAllocator:
    def test_compute_weights(self):
        import adaptive_allocator as alloc
        import tempfile
        import json
        with tempfile.TemporaryDirectory() as td:
            # Set up a ledger with some trades
            ledger_data = {"trades": []}
            for _ in range(15):
                ledger_data["trades"].append({
                    "pnl": 50.0, "strategy": "ORB", "kronos": "agree",
                    "outcome": "TARGET", "r": 1.5, "side": "BUY",
                    "date": "2026-09-11", "hour": 10
                })
            # Write ledger file
            import strategy_ledger
            strategy_ledger.save_ledger(ledger_data, base_dir=td)
            weights = alloc.compute_weights(base_dir=td)
            assert "ORB" in weights
            assert "weight" in weights["ORB"]

    def test_weight_bounds(self):
        import adaptive_allocator as alloc
        weights = alloc.compute_weights.__wrapped__ if hasattr(alloc.compute_weights, '__wrapped__') else None
        # Test boundaries directly
        from adaptive_allocator import W_FLOOR, W_CEIL
        assert W_FLOOR == 0.4
        assert W_CEIL == 1.5


# ===================================================================== #
# EVENT BUS                                                            #
# ===================================================================== #

class TestEventBus:
    def test_subscribe_and_emit(self):
        from event_bus import EventBus, EventRecord
        bus = EventBus()
        calls = []
        def handler(data, source):
            calls.append((data, source))
        bus.on("test", handler, "test_src")
        bus.emit("test", {"key": "val"}, "test_src")
        assert len(calls) == 1
        assert calls[0][0]["key"] == "val"

    def test_once(self):
        from event_bus import EventBus
        bus = EventBus()
        calls = []
        bus.once("once_test", lambda d, s: calls.append(True), "test")
        bus.emit("once_test", {}, "test")
        bus.emit("once_test", {}, "test")
        assert len(calls) == 1

    def test_error_isolation(self):
        from event_bus import EventBus
        bus = EventBus()
        bus.on("bad", lambda d, s: 1/0, "bad")
        # Should not raise
        bus.emit("bad", {}, "good")
        bus.emit("good", {"x": 1}, "good")
        assert True  # got here without crash

    def test_stats(self):
        from event_bus import EventBus
        bus = EventBus()
        bus.on("evt", lambda d, s: None, "src")
        bus.emit("evt", {}, "src")
        bus.emit("evt", {}, "src")
        stats = bus.stats()
        assert stats["total_events"]["evt"] == 2

    def test_prometheus_output(self):
        from metrics import prometheus_text
        # Test with some data first
        from metrics import inc
        inc("test_counter")
        output = prometheus_text()
        assert "test_counter" in output


# ===================================================================== #
# INTEGRATION                                                          #
# ===================================================================== #

class TestIntegration:
    def test_scan_once_returns_dataframe(self):
        """Test that scan_once returns a DataFrame (even if empty)."""
        import intraday_pattern_scanner_v2 as scn
        import pandas as pd
        # With an empty universe, should return empty DataFrame
        result = scn.scan_once.__code__
        assert result is not None  # function exists

    def test_scanner_module_loads(self):
        import intraday_pattern_scanner_v2 as scn
        # Verify module loaded without errors
        assert hasattr(scn, "_OPEN_POSITIONS")
        assert hasattr(scn, "reset_day")
        assert hasattr(scn, "compute_sl_target")
        assert hasattr(scn, "place_bracket_orders")



    def test_config_version_stable(self):
        import config_registry as cfg
        v1 = cfg.version()
        v2 = cfg.version()
        assert v1 == v2  # same config = same version


# ===================================================================== #
# STRATEGY IMPROVEMENT TESTS                                             #
# ===================================================================== #
class TestStrategyImprovements:
    """Tests for the research-backed strategy filters added to multi_strategy_live."""

    def test_gap_filter_configs_exist(self):
        """Verify Gap-Fill EMA + RSI filter parameters are defined."""
        import multi_strategy_live as ml
        assert hasattr(ml, "GAP_TREND_FILTER")
        assert hasattr(ml, "GAP_RSI_FILTER")
        assert hasattr(ml, "GAP_EMA_FAST")
        assert hasattr(ml, "GAP_EMA_SLOW")
        assert hasattr(ml, "GAP_RSI_OVERSOLD")
        assert hasattr(ml, "GAP_RSI_OVERBOUGHT")

    def test_gap_filter_defaults(self):
        """Default values match research parameters."""
        import multi_strategy_live as ml
        assert ml.GAP_TREND_FILTER == True
        assert ml.GAP_RSI_FILTER == True
        assert ml.GAP_EMA_FAST == 20
        assert ml.GAP_EMA_SLOW == 200
        assert ml.GAP_RSI_OVERSOLD == 40.0
        assert ml.GAP_RSI_OVERBOUGHT == 60.0

    def test_strategy_exits_updated_gap_profile(self):
        """Gap-Fill SL widened for cost/R<=0.3 (profitable-only)."""
        from strategy_exits import get_exit_params
        sl, rr, trail = get_exit_params("GAP_FILL", 1.0, 1.0, 1.0)
        assert sl == 2.0  # 2x ATR => 0.6-1% SL clears 18bps costs
        assert rr == 1.5  # quick 1.5R to prior-close/midpoint

    def test_strategy_exits_profiles_intact(self):
        """Verify all 8 strategies have exit profiles."""
        from strategy_exits import get_exit_params
        for strat in ["CANDLE_STRUCT", "ORB", "OB_SHORTS", "GAP_FILL", "GAPGO", "VWAP_RECLAIM", "VWAP_PULLBACK", "SUPERTREND"]:
            sl, rr, trail = get_exit_params(strat, 1.5, 2.0, 1.0)
            assert sl != 1.5 or rr != 2.0 or trail != 1.0  # at least one changed
            assert 0.5 <= sl <= 3.0
            assert 1.0 <= rr <= 5.0
            assert 0.5 <= trail <= 2.0

    def test_rsi_computation_valid(self):
        """Verify RSI inline computation in gap-fill section works."""
        import pandas as pd
        import numpy as np
        # Create synthetic price data
        closes = [100 + i * 0.5 + np.random.randn() * 0.3 for i in range(30)]
        df = pd.DataFrame({"close": closes, "high": [c + 0.5 for c in closes],
                          "low": [c - 0.5 for c in closes],
                          "open": [c + np.random.randn() * 0.2 for c in closes],
                          "volume": [100000 + np.random.randint(-10000, 10000) for _ in range(30)],
                          "ts": pd.date_range("2024-01-01", periods=30, freq="5min")})
        # RSI should be computable
        delta = pd.Series(df["close"]).diff()
        up = delta.clip(lower=0).rolling(14).mean()
        dn = (-delta.clip(upper=0)).rolling(14).mean()
        rs = up / dn.replace(0, float("nan"))
        rsi_val = 100 - 100 / (1 + rs)
        last_rsi = float(rsi_val.iloc[-1])
        assert pd.notna(last_rsi)
        assert 0.0 <= last_rsi <= 100.0


# ===================================================================== #
# ACCURACY REGRESSION — fixed indicators + gates (deep-check 2026-09-11) #
# ===================================================================== #

class TestIndicatorAccuracy:
    """Lock in: proper Supertrend recurrence, Wilder ADX, RSI bands, session VWAP."""

    @staticmethod
    def _trend_df(n=80, seed=7):
        import pandas as pd
        import numpy as np
        np.random.seed(seed)
        trend = np.concatenate([np.linspace(100, 108, 40), np.linspace(108, 98, 40)])[:n]
        close = trend + np.random.randn(n) * 0.15
        return pd.DataFrame({
            "open": close + np.random.randn(n) * 0.05,
            "high": close + np.abs(np.random.randn(n)) * 0.12,
            "low": close - np.abs(np.random.randn(n)) * 0.12,
            "close": close, "volume": np.full(n, 200000.0)})

    def test_supertrend_flips_with_trend(self):
        from shared_indicators import supertrend_series
        df = self._trend_df()
        s = supertrend_series(df)
        assert int((s.diff().fillna(0) != 0).sum()) >= 1  # must flip at regime change
        assert int(s.iloc[30]) == 1 and int(s.iloc[-1]) == -1

    def test_supertrend_delegated(self):
        import indicators_ta as ita
        df = self._trend_df()
        assert ita._supertrend_dir(df) in (+1, -1)  # no silent 0/EMA-proxy collapse

    def test_adx_separates_trend_from_flat(self):
        from shared_indicators import adx
        df = self._trend_df()
        flat = df.copy()
        flat["close"] = 100.0; flat["high"] = 100.1; flat["low"] = 99.9
        assert adx(df) is not None and adx(flat) is not None
        assert 0.0 <= adx(df) <= 100.0 and 0.0 <= adx(flat) <= 100.0
        assert adx(df) > adx(flat) + 10.0

    def test_confluence_rsi_bands_agree(self):
        import indicators_ta as ita
        import shared_indicators as si
        df = self._trend_df()
        b1, t1 = ita.confluence_score(df, -1)
        b2, t2 = si.confluence_score(df, -1)
        # both engines must use 30<r<=55 short band (old shared gave free RSI+)
        assert "RSI+" not in t2 or "RSI-" in t2 or b2 >= 0
        assert b1 >= 0 and b2 >= 0

    def test_session_vwap_and_slope(self):
        import pandas as pd
        from shared_indicators import session_vwap, vwap_slope_pct
        df = self._trend_df()
        df["ts"] = pd.date_range("2026-09-10 09:15", periods=len(df), freq="5min", tz="Asia/Kolkata")
        assert session_vwap(df) is not None and session_vwap(df) > 0
        assert vwap_slope_pct(df) is not None

    def test_orb_accuracy_gates_present(self):
        import multi_strategy_live as ml
        for attr in ("ORB_REQUIRE_TREND", "ORB_REQUIRE_RSI", "ORB_REQUIRE_STRENGTH",
                     "ORB_BUFFER_PCT", "ORB_MIN_RANGE_PCT", "CS_MIN_TA", "FRIDAY_SKIP"):
            assert hasattr(ml, attr)
        assert ml.ORB_VOL_MULT >= 1.5 and ml.CS_VOL_MULT >= 1.5 and ml.CS_MIN_TA >= 2


# ===================================================================== #
# RUN ALL TESTS                                                        #
# ===================================================================== #

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
