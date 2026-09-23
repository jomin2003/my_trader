"""Tests for chart_patterns — research top-6 NSE intraday pattern engine
(flag / triangle / double-top-bottom / ABCD, ORB + VWAP pattern exits,
candle-confirmation gate) and its multi_strategy_live wiring."""
import datetime as dt
from datetime import datetime as dtm

import pandas as pd
import pytest

import chart_patterns as cp
import multi_strategy_live as msl


# ---------------------------------------------------------------------------
# Synthetic 5-min session builder (volumes scaled so the scanner's
# Rs.25L turnover filter passes on realistic prices)
# ---------------------------------------------------------------------------
def _df(rows, hour=9, minute=15):
    base = dtm.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    times = [base + dt.timedelta(minutes=5 * i) for i in range(len(rows))]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime([t.strftime("%Y-%m-%d %H:%M") for t in times])
    df["ts"] = df["ts"].dt.tz_localize("Asia/Kolkata")
    return df


def _flag_session(direction=+1):
    rows = [(100, 100.3, 99.7, 100.0, 50000)] * 20
    if direction > 0:  # bull flag: rising pole, tight downward flag
        for i in range(5):
            o = 100 + i * 0.5
            rows.append((o, o + 0.45, o - 0.05, o + 0.4, 100000))
        for i in range(4):
            o = 102.5 - i * 0.05
            rows.append((o, 102.55, 102.35, o - 0.03, 40000))
        rows.append((102.5, 102.9, 102.45, 102.8, 150000))  # breakout
    else:  # bear flag: falling pole, tight upward flag
        for i in range(5):
            o = 105 - i * 0.5
            rows.append((o, o + 0.05, o - 0.45, o - 0.4, 100000))
        for i in range(4):
            o = 102.5 + i * 0.05
            rows.append((o, 102.65, 102.45, o + 0.03, 40000))
        rows.append((102.5, 102.55, 102.1, 102.2, 150000))  # breakdown
    return _df(rows)


def _triangle_session(direction=+1):
    rows = [(100, 100.3, 99.7, 100.0, 50000)] * 10
    if direction > 0:  # ascending: flat highs ~105.5, rising lows
        form = [
            (103.5, 104.0, 103.4, 103.7), (103.7, 104.1, 103.3, 104.0),
            (104.0, 104.4, 103.1, 104.2), (104.2, 104.5, 102.9, 104.0),
            (104.0, 105.5, 103.2, 104.6), (104.6, 104.9, 103.6, 104.3),
            (104.3, 105.0, 103.7, 104.5), (104.5, 105.1, 103.5, 104.6),
            (104.6, 105.2, 103.8, 104.7), (104.7, 105.52, 104.2, 104.8),
            (104.8, 105.15, 104.3, 104.6), (104.6, 105.2, 104.1, 104.7),
            (104.7, 105.25, 104.4, 104.8), (104.8, 105.2, 104.5, 104.75),
            (104.75, 105.3, 104.6, 104.8), (104.8, 105.35, 104.7, 104.85),
        ]
        vols = [80000 - i * 7000 for i in range(16)]
        for (o, h, l, c), v in zip(form, vols):
            rows.append((o, h, l, c, v))
        rows.append((105.5, 106.1, 105.45, 105.9, 130000))  # breakout
    else:  # descending: declining swing highs, flat swing lows ~94.5
        form = [
            (95.9, 96.1, 95.5, 95.7), (95.7, 96.2, 95.4, 95.9),
            (95.9, 97.0, 95.6, 96.1), (96.1, 96.0, 94.5, 95.6),
            (95.6, 95.7, 95.0, 95.4), (95.4, 95.6, 94.9, 95.5),
            (95.5, 95.8, 95.2, 95.4), (95.4, 95.5, 94.5, 95.0),
            (94.9, 94.95, 94.8, 94.85), (94.85, 95.0, 94.7, 94.9),
            (94.9, 95.1, 94.6, 94.85), (94.85, 94.9, 94.5, 94.7),
            (94.7, 94.85, 94.6, 94.75), (94.75, 94.9, 94.65, 94.8),
            (94.8, 94.9, 94.7, 94.8), (94.8, 94.85, 94.7, 94.78),
        ]
        vols = [80000 - i * 4000 for i in range(16)]
        for (o, h, l, c), v in zip(form, vols):
            rows.append((o, h, l, c, v))
        rows.append((94.5, 94.55, 93.9, 94.1, 130000))  # breakdown
    return _df(rows)


def _double_session(direction=-1):
    rows = []
    if direction < 0:  # double top: peaks 110.9 / 111.0, trough 108.0
        px = 105.0
        for _ in range(14):
            px += 0.35
            rows.append((px - 0.2, px + 0.15, px - 0.3, px, 60000))
        rows.append((110.5, 110.9, 110.3, 110.7, 100000))      # peak 1
        for low in [110.3, 109.8, 109.2, 108.6, 108.0]:        # trough
            rows.append((low + 0.4, low + 0.6, low, low + 0.2, 50000))
        for hi in [108.6, 109.6, 111.0]:                       # peak 2
            rows.append((hi - 0.5, hi, hi - 0.7, hi - 0.2, 75000))
        for hi in [110.4, 109.8, 109.2, 108.6, 108.0, 107.9]:  # decline
            rows.append((hi, hi + 0.1, hi - 0.5, hi - 0.3, 55000))
        rows.append((108.0, 108.1, 107.2, 107.5, 150000))      # neckline break
    else:  # double bottom: troughs 109.1 / 109.0, peak 112.0
        px = 115.0
        for _ in range(14):
            px -= 0.35
            rows.append((px + 0.2, px + 0.3, px - 0.15, px, 60000))
        rows.append((109.5, 109.7, 109.1, 109.3, 100000))      # trough 1
        for hi in [109.7, 110.2, 110.8, 111.4, 112.0]:         # peak
            rows.append((hi - 0.4, hi, hi - 0.6, hi - 0.2, 50000))
        for lo in [111.4, 110.4, 109.0]:                       # trough 2
            rows.append((lo + 0.4, lo + 0.6, lo, lo + 0.2, 75000))
        for lo in [109.6, 110.2, 110.8, 111.4, 112.0, 112.1]:  # rise
            rows.append((lo, lo + 0.5, lo - 0.1, lo + 0.3, 55000))
        rows.append((111.9, 112.7, 111.9, 112.4, 150000))      # neckline break
    return _df(rows)


def _abcd_session(variant="continuation"):
    rows = [(100.5, 100.8, 100.2, 100.5, 50000)] * 10
    rows.append((100.4, 100.6, 100.0, 100.3, 90000))   # A
    rows.append((100.3, 100.9, 100.2, 100.7, 90000))
    rows.append((100.7, 101.4, 100.6, 101.2, 90000))
    rows.append((101.2, 101.9, 101.1, 101.7, 90000))
    rows.append((101.7, 102.4, 101.6, 102.2, 90000))
    rows.append((102.2, 102.8, 102.1, 102.6, 90000))
    rows.append((102.6, 103.1, 102.5, 102.9, 90000))   # B (high 103.1)
    rows.append((102.9, 103.0, 102.4, 102.7, 90000))
    rows.append((102.7, 102.8, 102.2, 102.4, 45000))
    rows.append((102.4, 102.5, 101.9, 102.1, 45000))
    rows.append((102.1, 102.2, 101.7, 101.9, 45000))
    rows.append((101.9, 102.0, 101.5, 101.8, 45000))   # C (50% retrace)
    if variant == "continuation":
        for cpx in [102.1, 102.3, 102.5, 102.6, 102.7, 102.8, 102.9]:
            rows.append((cpx - 0.2, cpx + 0.1, cpx - 0.3, cpx, 60000))
        rows.append((102.9, 103.5, 102.8, 103.3, 150000))  # close > B
    else:  # fade at D: rally stalls at D projection 104.6, reversal bar
        for cpx in [102.1, 102.6, 103.1, 103.6, 104.1, 104.4, 104.5]:
            rows.append((cpx - 0.2, cpx + 0.15, cpx - 0.3, cpx, 60000))
        rows.append((104.70, 104.75, 104.40, 104.55, 150000))
    return _df(rows)


# ---------------------------------------------------------------------------
# Candle confirmation
# ---------------------------------------------------------------------------
def test_confirm_candle_accepts_clean_breakout():
    assert cp.confirm_candle(100, 100.8, 99.9, 100.7, 2.0, 0.5, +1, 100.5)


def test_confirm_candle_rejects_low_volume():
    assert not cp.confirm_candle(100, 100.8, 99.9, 100.7, 1.0, 0.5, +1, 100.5)


def test_confirm_candle_rejects_wick_pierce():
    # close never got beyond the level
    assert not cp.confirm_candle(100, 101.0, 99.5, 100.2, 2.0, 0.5, +1, 100.5)


def test_confirm_candle_rejects_extended_bar():
    # bar range > 2x ATR: exhaustion / chase
    assert not cp.confirm_candle(100, 101.5, 99.0, 101.2, 2.0, 0.5, +1, 100.5)


def test_confirm_candle_rejects_long_opposing_wick():
    assert not cp.confirm_candle(100, 102.0, 99.5, 100.6, 2.0, 0.5, +1, 100.5)


def test_confirm_candle_rejects_weak_body():
    assert not cp.confirm_candle(100, 101.0, 99.5, 100.1, 2.0, 0.5, +1, 99.0)


def test_confirm_entry_rejects_late_session():
    df = _flag_session(+1)
    df["ts"] = df["ts"] + pd.Timedelta(hours=6)  # last bar ~17:40 IST
    assert not cp.confirm_entry(df, +1)
    assert cp.detect(df) == []


def test_detect_requires_min_bars():
    assert cp.detect(_df([(100, 101, 99, 100, 1000)] * 10)) == []


# ---------------------------------------------------------------------------
# Pattern detectors
# ---------------------------------------------------------------------------
def _assert_exits(p, direction):
    assert p.direction == direction
    assert p.entry > 0 and p.sl > 0 and p.target > 0
    if direction > 0:
        assert p.sl < p.entry < p.target
    else:
        assert p.target < p.entry < p.sl
    assert p.rr >= cp.CP_MIN_RR
    assert p.sl_method and p.tgt_method


def test_flag_long():
    df = _flag_session(+1)
    pats = cp.detect_flag(df, cp._atr(df))
    assert len(pats) == 1
    _assert_exits(pats[0], +1)
    assert pats[0].name == "Flag Long"
    assert pats[0].strategy == "FLAG"
    assert pats[0].sl_method == "flag_extreme_0.1atr"
    assert pats[0].tgt_method == "measured_pole"


def test_flag_short():
    df = _flag_session(-1)
    pats = cp.detect_flag(df, cp._atr(df))
    assert len(pats) == 1
    _assert_exits(pats[0], -1)
    assert pats[0].name == "Flag Short"


def test_triangle_long():
    df = _triangle_session(+1)
    pats = cp.detect_triangle(df, cp._atr(df))
    assert len(pats) == 1
    _assert_exits(pats[0], +1)
    assert pats[0].name == "Triangle Long"
    assert pats[0].strategy == "TRIANGLE"


def test_triangle_short():
    df = _triangle_session(-1)
    pats = cp.detect_triangle(df, cp._atr(df))
    assert len(pats) == 1
    _assert_exits(pats[0], -1)
    assert pats[0].name == "Triangle Short"


def test_double_top_short():
    df = _double_session(-1)
    pats = cp.detect_double(df, cp._atr(df))
    assert len(pats) == 1
    _assert_exits(pats[0], -1)
    assert pats[0].name == "Double Top Short"
    assert pats[0].strategy == "DOUBLE_TOP_BOTTOM"
    assert pats[0].sl_method == "second_peak_0.1atr"


def test_double_bottom_long():
    df = _double_session(+1)
    pats = cp.detect_double(df, cp._atr(df))
    assert len(pats) == 1
    _assert_exits(pats[0], +1)
    assert pats[0].name == "Double Bottom Long"


def test_abcd_continuation():
    df = _abcd_session("continuation")
    pats = cp.detect_abcd(df, cp._atr(df))
    assert len(pats) == 1
    _assert_exits(pats[0], +1)
    assert pats[0].name == "ABCD Long"
    assert pats[0].strategy == "ABCD"
    assert pats[0].detail["variant"] == "continuation"


def test_abcd_fade_at_d():
    df = _abcd_session("fade")
    pats = cp.detect_abcd(df, cp._atr(df))
    assert len(pats) == 1
    _assert_exits(pats[0], -1)
    assert pats[0].name == "ABCD Fade Short"
    assert pats[0].detail["variant"] == "fade_at_d"


def test_detect_top_level_routes():
    assert "Flag Long" in [p.name for p in cp.detect(_flag_session(+1))]
    assert "Triangle Long" in [p.name for p in cp.detect(_triangle_session(+1))]
    assert "Double Top Short" in [p.name for p in cp.detect(_double_session(-1))]
    assert "ABCD Long" in [p.name for p in cp.detect(_abcd_session("continuation"))]


# ---------------------------------------------------------------------------
# Pattern-native exits
# ---------------------------------------------------------------------------
def test_orb_exits_report_formula():
    # SL at opposite extreme - 0.1x ATR; T1 = entry + 1.5x OR width
    sl, tgt = cp.orb_exits(1100.0, 1090.0, 1101.0, +1, 5.0)
    assert sl == pytest.approx(1090.0 - 0.5)
    assert tgt == pytest.approx(1101.0 + 1.5 * 10.0)


def test_orb_exits_rejects_tiny_range():
    assert cp.orb_exits(100.2, 100.0, 100.3, +1, 1.0) is None  # width < 0.4x ATR


def test_orb_exits_rejects_chase_breakout():
    # breakout bar extended far past the extreme -> sub-1.0R geometry
    # (risk% kept inside the 0.6-1.5% rails so no clamp interferes)
    assert cp.orb_exits(1108.0, 1098.0, 1114.0, +1, 5.0) is None


def test_vwap_exits_pullback():
    # pullback long: SL below reversal candle - 0.1x ATR, target prior swing
    sl, tgt = cp.vwap_exits(105.0, +1, 0.5, 104.0 - 0.05, 107.0)
    assert sl == pytest.approx(103.95)
    assert tgt == pytest.approx(107.0)


def test_vwap_exits_applies_15r_floor():
    # swing too close -> 1.5R floor instead
    sl, tgt = cp.vwap_exits(105.0, +1, 0.5, 104.0 - 0.05, 105.2)
    risk = 105.0 - 103.95
    assert tgt == pytest.approx(105.0 + 1.5 * risk, abs=0.02)


def test_exits_ok_rejects_poor_rr():
    assert not cp.exits_ok(100.0, 99.0, 101.0, +1)      # 1R < 1.5R
    assert cp.exits_ok(100.0, 99.0, 101.5, +1)           # exactly 1.5R
    assert not cp.exits_ok(100.0, 100.0, 102.0, +1)      # zero risk


# ---------------------------------------------------------------------------
# multi_strategy_live wiring
# ---------------------------------------------------------------------------
def test_msl_detect_patterns_emits_chart_patterns():
    hits = msl.detect_patterns(_flag_session(+1))
    assert (+1, "Flag Long", 6) in hits or any(h[1] == "Flag Long" for h in hits)


def test_msl_score_signals_flag_row_carries_struct_exits():
    df = _flag_session(+1)
    hits = msl.detect_patterns(df)
    rows = msl.score_signals("CPTEST", "999001", df, hits)
    flag_rows = [r for r in rows if r["strategy"] == "FLAG"]
    assert flag_rows, "expected a FLAG row from score_signals"
    r = flag_rows[0]
    assert r["pattern"] == "Flag Long (LIVE)"
    assert r["direction"] == +1
    assert r["struct_sl"] < r["price"] < r["struct_target"]
    assert r["rr"] >= cp.CP_MIN_RR
    assert r["sl_method"] == "flag_extreme_0.1atr"
    assert r["tgt_method"] == "measured_pole"
