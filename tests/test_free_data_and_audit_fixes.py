"""Tests for free_data_provider.py and the 2026-10 workflow & audit fixes."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, time as dtime, timedelta

dtime_max = dtime(23, 59, 59)
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

IST = ZoneInfo("Asia/Kolkata")


# =====================================================================
# 1. FreeMarketDataClient & HybridMarketClient unit tests
# =====================================================================
def test_symbol_resolution_and_yahoo_ticker():
    import free_data_provider as fdp

    uni_df = pd.DataFrame([
        {"symbol": "RELIANCE", "security_id": "2885"},
        {"symbol": "HDFCBANK", "security_id": "1333"},
    ])
    fdp.register_universe_mapping(uni_df)
    assert fdp.resolve_symbol("2885") == "RELIANCE"
    assert fdp.resolve_symbol("RELIANCE") == "RELIANCE"
    assert fdp.resolve_symbol("13", exchange_segment="IDX_I", instrument_type="INDEX") == "^NSEI"
    assert fdp.resolve_symbol("13", exchange_segment="NSE_EQ", instrument_type="EQUITY") == "ABB"

    assert fdp.to_yahoo_ticker("RELIANCE") == "RELIANCE.NS"
    assert fdp.to_yahoo_ticker("TATAMOTORS") == "TMPV.NS"
    assert fdp.to_yahoo_ticker("^NSEI") == "^NSEI"
    assert fdp.to_yahoo_ticker("NIFTY") == "^NSEI"


def test_free_client_intraday_and_quote(monkeypatch):
    import free_data_provider as fdp

    client = fdp.FreeMarketDataClient(cache_ttl_sec=60.0)
    fdp.register_symbol("RELIANCE", "2885")

    now_ist = datetime.now(IST).replace(hour=10, minute=15, second=0, microsecond=0)
    ts_list = [now_ist - timedelta(minutes=5 * i) for i in range(10, -1, -1)]
    fake_df = pd.DataFrame({
        "ts": pd.Series(ts_list),
        "open": [2500.0 + i for i in range(11)],
        "high": [2505.0 + i for i in range(11)],
        "low": [2495.0 + i for i in range(11)],
        "close": [2502.0 + i for i in range(11)],
        "volume": [10000 * (i + 1) for i in range(11)],
    })
    fake_meta = {"regularMarketPrice": 2512.0}

    monkeypatch.setattr(
        client,
        "_fetch_yahoo_chart",
        lambda yahoo_ticker, interval_str="15m", range_str="5d": (fake_df.copy(), dict(fake_meta)),
    )

    resp = client.intraday_minute_data(
        security_id="2885",
        exchange_segment="NSE_EQ",
        instrument_type="EQUITY",
        from_date=(now_ist.date() - timedelta(days=2)).isoformat(),
        to_date=now_ist.date().isoformat(),
        interval=5,
    )
    assert resp["status"] == "success"
    assert len(resp["data"]["open"]) == 11
    assert resp["data"]["close"][-1] == 2512.0

    # Dict-style quote_data({"NSE_EQ": ["2885"]})
    q1 = client.quote_data({"NSE_EQ": ["2885"]})
    assert q1["status"] == "success"
    assert q1["data"]["data"]["NSE_EQ"]["2885"]["last_price"] == 2512.0

    # Keyword-style quoteData(security_id="2885", exchange_segment="NSE_EQ")
    q2 = client.quoteData(security_id="2885", exchange_segment="NSE_EQ")
    assert q2["status"] == "success"
    assert q2["data"]["LTP"] == 2512.0


def test_hybrid_client_falls_back_when_dhan_data_fails(monkeypatch):
    import free_data_provider as fdp

    class BrokenDhanDataClient:
        def intraday_minute_data(self, **kwargs):
            return {"status": "failure", "remarks": "Data API not subscribed", "data": {}}

        def quote_data(self, securities):
            return {"status": "failure", "data": {}}

        def place_order(self, **kwargs):
            return {"status": "success", "data": {"orderId": "LIVE-123"}}

    free_c = fdp.FreeMarketDataClient(cache_ttl_sec=60.0)
    now_ist = datetime.now(IST).replace(hour=10, minute=0, second=0, microsecond=0)
    fake_df = pd.DataFrame({
        "ts": pd.Series([now_ist]),
        "open": [100.0], "high": [102.0], "low": [99.0], "close": [101.5], "volume": [5000],
    })
    monkeypatch.setattr(
        free_c,
        "_fetch_yahoo_chart",
        lambda yahoo_ticker, interval_str="15m", range_str="5d": (fake_df.copy(), {"regularMarketPrice": 101.5}),
    )
    fdp.register_symbol("INFY", "999")

    hybrid = fdp.HybridMarketClient(BrokenDhanDataClient(), free_client=free_c)
    r = hybrid.intraday_minute_data(
        security_id="999", exchange_segment="NSE_EQ", instrument_type="EQUITY",
        from_date=now_ist.date().isoformat(), to_date=now_ist.date().isoformat(), interval=5,
    )
    assert r["status"] == "success"
    assert r["data"]["close"] == [101.5]
    assert hybrid._dhan_data_failed is True

    # Order execution still delegates to the real Dhan client
    ord_resp = hybrid.place_order(security_id="999", quantity=1)
    assert ord_resp["data"]["orderId"] == "LIVE-123"


# =====================================================================
# 2. Scanner & Live Order / OCO / State / Risk fixes
# =====================================================================
def test_order_status_and_fill_parsing():
    import intraday_pattern_scanner_v2 as scn

    class DummyDhan:
        def get_order_by_id(self, oid):
            if oid == "1":
                return {"status": "success", "data": {"orderStatus": "TRADED", "averageTradedPrice": 250.5}}
            if oid == "2":
                return {"status": "success", "data": [{"order_status": "COMPLETED", "tradedPrice": 300.0}]}
            return {"status": "success", "data": {"orderStatus": "PENDING"}}

    d = DummyDhan()
    st1 = scn._order_status(d, "1")
    assert st1 == "TRADED"
    assert scn._is_order_filled(st1) is True

    st2 = scn._order_status(d, "2")
    assert st2 == "COMPLETED"
    assert scn._is_order_filled(st2) is True

    st3 = scn._order_status(d, "3")
    assert scn._is_order_filled(st3) is False


def test_place_order_numeric_prices_for_sl():
    import intraday_pattern_scanner_v2 as scn

    captured = {}

    class DummyDhan:
        STOP_LOSS_MARKET = "STOP_LOSS_MARKET"

        def place_order(self, **kwargs):
            captured.update(kwargs)
            return {"status": "success", "data": {"orderId": "ORD-99"}}

    resp = scn._place(DummyDhan(), "2885", "SELL", 10, "SL-M", price=0, trigger_price=2480.0)
    oid = scn._order_id(resp)
    assert oid == "ORD-99"
    assert captured["order_type"] == "STOP_LOSS_MARKET"
    assert isinstance(captured["price"], float)
    assert isinstance(captured["trigger_price"], float)
    assert captured["trigger_price"] == 2480.0


def test_restore_state_survives_reset_day_when_open_positions_empty(tmp_path, monkeypatch):
    import intraday_pattern_scanner_v2 as scn
    import state_store

    state_file = tmp_path / "state.json"
    monkeypatch.setattr(state_store, "STATE_FILE", str(state_file))
    monkeypatch.delenv("STATE_GIST_ID", raising=False)
    monkeypatch.delenv("GITHUB_GIST_ID", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    scn._OPEN_POSITIONS.clear()
    scn._TRADED_TODAY.clear()
    scn._COMPLETED_TRADES.clear()
    scn._TRADED_TODAY.add("RELIANCE")
    scn._COMPLETED_TRADES.append({
        "symbol": "RELIANCE", "side": "BUY", "qty": 5, "entry": 2500.0,
        "exit": 2415.9, "pnl": -420.5, "total_pnl": -420.5,
        "outcome": "SL", "strategy": "ORB", "time": "10:15", "r": -1.0,
    })
    state_store.save_state(scn, force=True)

    # Simulate process restart on same day with 0 open positions
    scn._TRADED_TODAY.clear()
    scn._COMPLETED_TRADES.clear()
    scn._LAST_RESTORE_DATE = None

    info = state_store.restore_state(scn)
    assert info["restored"] is True
    assert "RELIANCE" in scn._TRADED_TODAY
    assert len(scn._COMPLETED_TRADES) == 1

    # Calling reset_day() on the first scan after restore must NOT wipe restored state
    scn.reset_day()
    assert "RELIANCE" in scn._TRADED_TODAY
    assert len(scn._COMPLETED_TRADES) == 1


def test_latest_bar_quote_and_monitor_oco_stale_bar_guard(monkeypatch):
    import intraday_pattern_scanner_v2 as scn

    now_ist = datetime.now(IST)

    class DummyQuoteDhan:
        def quote_data(self, securities):
            return {
                "status": "success",
                "data": {
                    "data": {
                        "NSE_EQ": {
                            "2885": {
                                "last_price": 103.5,
                                "ohlc": {"open": 100.0, "high": 104.0, "low": 101.0, "close": 102.0},
                            }
                        }
                    }
                },
            }

    bar = scn._latest_bar(DummyQuoteDhan(), "2885")
    assert bar is not None
    assert bar["high"] == 104.0
    assert bar["low"] == 101.0
    assert bar["close"] == 103.5

    # Verify monitor_oco skips bars whose timestamp is older than opened_at - CANDLE_INTERVAL_MIN
    scn._OPEN_POSITIONS.clear()
    scn._OPEN_POSITIONS["RELIANCE"] = {
        "symbol": "RELIANCE",
        "security_id": "2885",
        "side": "BUY",
        "qty": 10,
        "entry": 100.0,
        "sl": 95.0,
        "target": 110.0,
        "init_sl_dist": 5.0,
        "opened_at": now_ist,
        "strategy": "ORB",
    }
    stale_bar = {
        "high": 150.0,  # would hit target=110 if not ignored as pre-entry stale bar
        "low": 99.0,
        "close": 101.0,
        "ts": now_ist - timedelta(minutes=30),
    }
    monkeypatch.setattr(scn, "_latest_bar", lambda dhan, sid: stale_bar)
    monkeypatch.setattr(scn, "FORCE_EXIT_TIME", dtime_max)
    scn.monitor_oco(DummyQuoteDhan())
    assert "RELIANCE" in scn._OPEN_POSITIONS
    scn._OPEN_POSITIONS.clear()


# =====================================================================
# 3. Strategy Ledger, Live Config, Precompute OBs, Seed Gap, Logging
# =====================================================================
def test_strategy_ledger_disagree_and_total_pnl(tmp_path, monkeypatch):
    import strategy_ledger as sl

    monkeypatch.delenv("LEDGER_GIST_ID", raising=False)
    monkeypatch.delenv("GITHUB_GIST_ID", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    trades = [
        {
            "symbol": "TCS",
            "strategy": "ORB",
            "side": "BUY",
            "entry": 3500.0,
            "exit": 3520.0,
            "qty": 5,
            "pnl": 100.0,
            "total_pnl": 250.0,  # includes partial leg PnL
            "r": 1.5,
            "outcome": "TARGET",
            "kronos": "disagree(0.75)",
            "time": "10:35:00",
        }
    ]
    led = sl.append_today(trades, nifty_trend=1, base_dir=str(tmp_path))
    assert len(led["trades"]) == 1
    row = led["trades"][0]
    assert row["kronos"] == "disagree"
    assert float(row["pnl"]) == 250.0
    assert row["symbol"] == "TCS"

    # Dedup: calling again with same trade appends 0
    led2 = sl.append_today(trades, nifty_trend=1, base_dir=str(tmp_path))
    assert len(led2["trades"]) == 1


def test_live_config_restores_defaults_when_missing_or_expired(tmp_path):
    import intraday_pattern_scanner_v2 as scn
    import live_config as lc

    orig_mult = scn.ATR_MULTIPLIER
    cfg_file = tmp_path / "live_config.json"

    try:
        # 1. Apply valid config (with timezone-aware IST timestamp)
        cfg_file.write_text(json.dumps({
            "meta": {"promoted_at": datetime.now(IST).isoformat(), "fitness": 1.5},
            "params": {"ATR_MULTIPLIER": 2.25, "MAX_RISK_PER_TRADE": 999999},  # MAX_RISK_PER_TRADE must be ignored
        }))
        lc.apply_live_config(config_path=cfg_file)
        assert scn.ATR_MULTIPLIER == 2.25
        assert scn.MAX_RISK_PER_TRADE != 999999

        # 2. Remove config -> must restore baseline default
        cfg_file.unlink()
        lc.apply_live_config(config_path=cfg_file)
        assert scn.ATR_MULTIPLIER == orig_mult
    finally:
        scn.ATR_MULTIPLIER = orig_mult


def test_precompute_ob_confirmed_time_prevents_lookahead():
    import precompute_order_blocks as pob
    import multi_strategy_live as msl

    # Create 25 bars on Day 1 (for Wilder ATR warmup) + 8 bars on Day 2 starting 09:15 IST
    day1_base = pd.Timestamp("2026-10-02 09:15:00", tz=IST)
    day2_base = pd.Timestamp("2026-10-05 09:15:00", tz=IST)
    rows = []
    for k in range(22):
        t = day1_base + pd.Timedelta(minutes=15 * k)
        rows.append({"ts": t, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10000})

    # Day 2 bars (indices 22..29, timestamps 09:15..11:00)
    for k in range(8):
        t = day2_base + pd.Timedelta(minutes=15 * k)
        if k == 2:  # 09:45 bar: green candle before sharp drop -> BEAR OB
            rows.append({"ts": t, "open": 100.0, "high": 102.0, "low": 99.5, "close": 101.5, "volume": 10000})
        elif k in (3, 4, 5):  # 10:00, 10:15, 10:30 bars drop sharply below prior10 low (99.0)
            rows.append({"ts": t, "open": 99.0, "high": 99.5, "low": 92.0, "close": 93.0, "volume": 15000})
        else:
            rows.append({"ts": t, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10000})

    df15 = pd.DataFrame(rows)
    obs = pob.find_order_blocks(df15, "TESTSYM")
    assert len(obs) >= 1
    assert obs[0]["ob_time"] == "09:45"
    assert obs[0]["confirmed_time"] == "10:30"
    assert obs[0]["confirmed_time"] > obs[0]["ob_time"]


def test_seed_today_gap_cold_start_without_existing_csv(tmp_path):
    import seed_today_gap as stg

    gap_csv = tmp_path / "gap_data.csv"
    pdhl_csv = tmp_path / "pdhl_data.csv"

    now_ist = datetime.now(IST)
    today = now_ist.date()
    yday = stg._prev_trading_day(today)

    t_yday = int(datetime(yday.year, yday.month, yday.day, 15, 15, tzinfo=IST).timestamp())
    t_today = int(datetime(today.year, today.month, today.day, 9, 15, tzinfo=IST).timestamp())

    class DummyDhan:
        is_free_provider = True

        def intraday_minute_data(self, **kwargs):
            return {
                "status": "success",
                "data": {
                    "timestamp": [t_yday, t_today],
                    "open": [1000.0, 1010.0],   # 1.0% gap up
                    "high": [1015.0, 1012.0],
                    "low": [990.0, 1008.0],
                    "close": [1000.0, 1011.0],
                    "volume": [50000, 30000],
                },
            }

    orig_pdhl = stg._pdhl_path
    stg._pdhl_path = lambda: pdhl_csv
    try:
        uni = pd.DataFrame([{"symbol": "RELIANCE", "security_id": "2885"}])
        n = stg.seed_today(DummyDhan(), uni, gap_csv_path=str(gap_csv))
        assert n == 1
        assert gap_csv.exists()
        assert pdhl_csv.exists()
    finally:
        stg._pdhl_path = orig_pdhl


def test_logging_redaction_filter_masks_env_secrets(monkeypatch):
    import logging_config as lc

    monkeypatch.setenv("TG_WEBHOOK_SECRET", "super-secret-webhook-token-999")
    rf = lc.RedactionFilter()
    rec = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=1,
        msg="Header was super-secret-webhook-token-999 in request",
        args=(), exc_info=None,
    )
    rf.filter(rec)
    assert "super-secret-webhook-token-999" not in rec.msg
    assert "***REDACTED***" in rec.msg
