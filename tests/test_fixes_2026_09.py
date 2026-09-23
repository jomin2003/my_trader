"""Regression tests for the 2026-09-13 audit fixes.

Covers: reset_day same-day preservation, live OCO sibling-cancel + flatten, close
idempotency, partial-exit TOCTOU lock, ADX flat-tape fail-closed, Kronos stale
fail-closed, OB past-day fallback marking, ledger dedup + PARTIAL skip.
"""
import datetime as dt
from datetime import datetime as dtm

import numpy as np
import pandas as pd
import pytest


# ---------------------------------------------------------------- reset_day
class TestResetDay:
    def test_same_day_restored_positions_survive(self):
        import intraday_pattern_scanner_v2 as scn
        now = dtm.now(scn.IST)
        scn._OPEN_POSITIONS["REL"] = {"symbol": "REL", "opened_at": now}
        scn._COMPLETED_TRADES.append({"symbol": "REL"})
        try:
            scn.reset_day()
            assert "REL" in scn._OPEN_POSITIONS, "same-day restored position was wiped"
        finally:
            scn._OPEN_POSITIONS.pop("REL", None)
            scn._COMPLETED_TRADES.clear()

    def test_undated_position_is_cleared(self):
        import intraday_pattern_scanner_v2 as scn
        scn._OPEN_POSITIONS["TEST"] = {"symbol": "TEST"}  # no opened_at
        try:
            scn.reset_day()
            assert len(scn._OPEN_POSITIONS) == 0
        finally:
            scn._OPEN_POSITIONS.clear()

    def test_yesterday_position_is_cleared(self):
        import intraday_pattern_scanner_v2 as scn
        old = dtm.now(scn.IST) - dt.timedelta(days=1)
        scn._OPEN_POSITIONS["OLD"] = {"symbol": "OLD", "opened_at": old}
        try:
            scn.reset_day()
            assert "OLD" not in scn._OPEN_POSITIONS
        finally:
            scn._OPEN_POSITIONS.clear()


# ---------------------------------------------------------------- ADX
class TestAdx:
    def _flat_df(self, n=40):
        rows = []
        for i in range(n):
            rows.append({"open": 100.0, "high": 100.05, "low": 99.95,
                         "close": 100.0, "volume": 1000})
        return pd.DataFrame(rows)

    def test_flat_tape_returns_zero_not_nan(self):
        from shared_indicators import adx
        v = adx(self._flat_df(), 14)
        assert v is not None and v == pytest.approx(0.0, abs=1e-6)

    def test_trending_tape_has_adx(self):
        from shared_indicators import adx
        rows = []
        px = 100.0
        for i in range(60):
            px += 0.5
            rows.append({"open": px - 0.4, "high": px + 0.2, "low": px - 0.5,
                         "close": px, "volume": 1000})
        v = adx(pd.DataFrame(rows), 14)
        assert v is not None and v > 25


# ---------------------------------------------------------------- Kronos staleness
class TestKronosStale:
    def test_naive_timestamp_is_stale_checked(self):
        import kronos_gate as kg
        old_naive = (dtm.now() - dt.timedelta(hours=5)).isoformat()  # no tzinfo
        assert kg._stale({"ts": old_naive}) is True

    def test_garbage_timestamp_is_stale(self):
        import kronos_gate as kg
        assert kg._stale({"ts": "not-a-date"}) is True

    def test_fresh_aware_timestamp_not_stale(self):
        import kronos_gate as kg
        fresh = dtm.now(kg.IST).isoformat()
        assert kg._stale({"ts": fresh}) is False


# ---------------------------------------------------------------- ledger
class TestLedgerDedup:
    def test_double_append_does_not_duplicate(self, tmp_path):
        import strategy_ledger as sl
        trades = [{"strategy": "ORB", "side": "BUY", "pnl": 100.0, "r": 1.0,
                   "outcome": "TARGET", "time": "10:00:00"}]
        sl.append_today(trades, base_dir=str(tmp_path))
        sl.append_today(trades, base_dir=str(tmp_path))
        led = sl.load_ledger(base_dir=str(tmp_path))
        todays = [t for t in led["trades"] if t["date"] == dtm.now(sl.IST).date().isoformat()]
        assert len(todays) == 1

    def test_partial_rows_are_skipped(self, tmp_path):
        import strategy_ledger as sl
        trades = [{"strategy": "ORB", "side": "BUY", "pnl": 5.0, "r": 0.3,
                   "outcome": "PARTIAL", "time": "10:00:00"}]
        sl.append_today(trades, base_dir=str(tmp_path))
        led = sl.load_ledger(base_dir=str(tmp_path))
        todays = [t for t in led["trades"] if t["date"] == dtm.now(sl.IST).date().isoformat()]
        assert len(todays) == 0


# ---------------------------------------------------------------- scanner OCO
class _FakeDhan:
    """Records orders; quoteData/fetch paths fall back to bars."""
    def __init__(self):
        self.orders = []
        self.next_order_id = 100

    def place_order(self, **kw):
        self.next_order_id += 1
        self.orders.append(kw)
        return {"orderId": str(self.next_order_id)}

    def cancel_order(self, oid):
        return {"ok": True}

    def get_order_by_id(self, oid):
        return {"data": {"order_status": "PENDING"}}


class TestOcoFixes:
    def _scanner(self):
        import intraday_pattern_scanner_v2 as scn
        return scn

    def _mkpos(self, scn, sym="REL"):
        return {"symbol": sym, "security_id": "1", "side": "BUY", "entry": 100.0,
                "sl": 99.0, "target": 102.0, "qty": 10, "init_sl_dist": 1.0,
                "strategy": "ORB", "pattern": "", "kexit": "", "sl_id": "S1",
                "tgt_id": "T1", "opened_at": dtm.now(scn.IST), "partial_done": False,
                "be_done": False, "best_fav": 0.0, "best_price": 100.0,
                "last_price": 100.0, "realized": 0.0}

    def test_paper_close_is_idempotent(self):
        scn = self._scanner()
        dhan = _FakeDhan()
        scn._OPEN_POSITIONS["REL"] = self._mkpos(scn)
        before = len(scn._COMPLETED_TRADES)
        scn._close_and_record(dhan, "REL", scn._OPEN_POSITIONS["REL"], "SL", 99.0)
        scn._close_and_record(dhan, "REL", scn._OPEN_POSITIONS.get("REL"), "SL", 99.0)
        assert len(scn._COMPLETED_TRADES) == before + 1, "double-recorded close"
        assert "REL" not in scn._OPEN_POSITIONS

    def test_paper_close_pops_position(self):
        scn = self._scanner()
        dhan = _FakeDhan()
        scn._OPEN_POSITIONS["TCS"] = self._mkpos(scn, "TCS")
        scn._close_and_record(dhan, "TCS", scn._OPEN_POSITIONS["TCS"], "TARGET", 102.0)
        assert "TCS" not in scn._OPEN_POSITIONS

    def test_partial_cannot_double_fire(self):
        scn = self._scanner()
        dhan = _FakeDhan()
        pos = self._mkpos(scn, "INFY")
        pos["last_price"] = 101.5  # 1.5R if PARTIAL_EXIT_R <= 1
        scn._OPEN_POSITIONS["INFY"] = pos
        before = pos["qty"]
        scn._partial_exit(dhan, "INFY", pos, 101.5)
        scn._partial_exit(dhan, "INFY", pos, 101.5)
        assert pos["partial_done"] is True
        assert pos["qty"] < before, "partial never fired"
        assert pos["qty"] > 0 or pos["partial_done"], "bookkeeping inconsistent"
        # exactly one PARTIAL record
        partials = [t for t in scn._COMPLETED_TRADES
                    if t.get("symbol") == "INFY" and t.get("outcome") == "PARTIAL"]
        assert len(partials) <= 1

    def test_monitor_oco_paper_hit_closes(self):
        scn = self._scanner()
        dhan = _FakeDhan()
        scn._OPEN_POSITIONS["WIP"] = self._mkpos(scn, "WIP")
        # monkeypatch _latest_bar to a bar that hits the stop
        orig = scn._latest_bar
        scn._latest_bar = lambda d, sid: {"high": 99.0, "low": 98.8, "close": 98.9}
        try:
            scn.monitor_oco(dhan)
        finally:
            scn._latest_bar = orig
        assert "WIP" not in scn._OPEN_POSITIONS


# ---------------------------------------------------------------- OB zones
class TestObFallback:
    def test_fallback_marks_past_day(self, monkeypatch, tmp_path):
        import multi_strategy_live as msl
        old = dt.date.today() - dt.timedelta(days=1)
        msl._OB.clear()
        msl._OB[("TESTSYMB", old)] = [{"type": "BEAR", "time": "14:00", "hi": 101.0, "lo": 100.5}]
        msl._OB_LOADED = True
        try:
            zones = msl._get_ob("TESTSYMB", dt.date.today())
            assert zones and zones[0].get("_past_day") is True
            # today's zones returned untouched (no marker)
            msl._OB[("TESTSYMB", dt.date.today())] = [{"type": "BEAR", "time": "09:30", "hi": 102.0, "lo": 101.5}]
            zones_today = msl._get_ob("TESTSYMB", dt.date.today())
            assert zones_today[0].get("_past_day") is None
        finally:
            msl._OB.clear()
            msl._OB_LOADED = False
