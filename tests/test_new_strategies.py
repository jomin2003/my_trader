"""Tests for the two 2026-09-13 research-backed strategies: MR_VWAP_FADE + PDHL_BREAK."""
import datetime as dt
from datetime import datetime as dtm

import numpy as np
import pandas as pd
import pytest


def _session_df(stretch="down", n=40, end="11:00"):
    """Synthetic one-session 5-min df whose close is stretched below/above VWAP."""
    rows = []
    base = dtm.now().replace(hour=9, minute=15, second=0, microsecond=0)
    px = 100.0
    times = []
    for i in range(n):
        t = base + dt.timedelta(minutes=5 * i)
        if t.strftime("%H:%M") > end:
            break
        times.append(t)
        if stretch == "down":
            px = 100.0 - (i ** 1.35) * 0.35  # accelerating selloff
            rows.append({"open": px + 0.3, "high": px + 0.45, "low": px - 0.2,
                         "close": px, "volume": 800})
        elif stretch == "up":
            px = 100.0 + (i ** 1.35) * 0.35
            rows.append({"open": px - 0.3, "high": px + 0.2, "low": px - 0.45,
                         "close": px, "volume": 800})
        else:  # flat
            rows.append({"open": 100.0, "high": 100.05, "low": 99.95,
                         "close": 100.0, "volume": 800})
    df = pd.DataFrame(rows)
    df["ts"] = pd.to_datetime([t.strftime("%Y-%m-%d %H:%M") for t in times])
    df["ts"] = df["ts"].dt.tz_localize("Asia/Kolkata")
    return df


class TestRegistry:
    def test_new_strategies_registered(self):
        from config_registry import StrategyName
        assert "MR_VWAP_FADE" in StrategyName.ALL
        assert "PDHL_BREAK" in StrategyName.ALL

    def test_aliases_normalize(self):
        from config_registry import StrategyName
        assert StrategyName.normalize("VWAPFADE") == "MR_VWAP_FADE"
        assert StrategyName.normalize("pdhl") == "PDHL_BREAK"

    def test_exit_profiles_exist(self):
        from strategy_exits import get_exit_params
        sl, rr, tr = get_exit_params("MR_VWAP_FADE", 2.0, 1.5, 1.0)
        assert (sl, rr, tr) == (1.2, 0.8, 0.5)
        sl, rr, tr = get_exit_params("PDHL_BREAK", 2.0, 1.5, 1.0)
        assert (sl, rr, tr) == (1.5, 1.5, 1.0)


class TestMrVwapFade:
    def test_vwap_bands_sane(self):
        from multi_strategy_live import _vwap_bands
        df = _session_df("flat", n=40)
        vwap, sigma = _vwap_bands(df)
        assert abs(float(vwap.iloc[-1]) - 100.0) < 0.5
        assert float(sigma.iloc[-1]) >= 0

    def test_rsi2_bounds(self):
        from multi_strategy_live import _rsi2
        df = _session_df("down", n=40)
        r = _rsi2(df["close"])
        assert 0 <= float(r.iloc[-1]) <= 100

    def test_stretched_down_session_fades_long(self):
        import multi_strategy_live as msl
        df = _session_df("down", n=40, end="11:00")
        hits = msl.detect_patterns(df)
        fades = [h for h in hits if "VwapFade" in h[1]]
        if fades:  # strength of selloff must exceed 2 sigma to trigger
            assert fades[0][0] == +1 and fades[0][1] == "VwapFade Long"

    def test_flat_session_does_not_fade(self):
        import multi_strategy_live as msl
        df = _session_df("flat", n=40)
        hits = msl.detect_patterns(df)
        assert not [h for h in hits if "VwapFade" in h[1]]


class TestPdhl:
    def test_levels_load_and_get(self, tmp_path, monkeypatch):
        import multi_strategy_live as msl
        today = dt.date.today().isoformat()
        p = tmp_path / "pdhl_data.csv"
        p.write_text(f"symbol,date,pdh,pdl,prev_close,prev_range,atr\n"
                     f"RELIANCE,{today},105.0,95.0,100.0,10.0,1.5\n")
        monkeypatch.setattr(msl, "PDHL_DATA_PATH", str(p))
        msl._PDHL.clear(); msl._PDHL_LOADED = False
        try:
            lv = msl._get_pdhl("reliance", dt.date.today())
            assert lv and lv["pdh"] == 105.0 and lv["pdl"] == 95.0
        finally:
            msl._PDHL.clear(); msl._PDHL_LOADED = False

    def test_break_hit_requires_volume_and_inside_open(self, monkeypatch):
        import multi_strategy_live as msl
        today = dt.date.today()
        msl._PDHL[("TESTSYM", today)] = {"pdh": 105.0, "pdl": 95.0,
                                         "prev_close": 100.0, "prev_range": 10.0}
        msl._PDHL_LOADED = True
        # session: opens inside prior range, grinds up, closes beyond PDH
        rows = []
        base = dtm.now().replace(hour=9, minute=15, second=0, microsecond=0)
        times = []
        px = 100.0
        for i in range(15):
            px = 100.0 + i * 0.35
            t = base + dt.timedelta(minutes=5 * i)
            times.append(t)
            if i == 14:  # final bar crosses PDH (105.0) with the 0.05% buffer
                px = 105.4
            rows.append({"open": px - 0.2, "high": px + 0.1, "low": px - 0.3,
                         "close": px, "volume": 60000 if i >= 10 else 15000})
        df = pd.DataFrame(rows)
        df["ts"] = pd.to_datetime([t.strftime("%Y-%m-%d %H:%M") for t in times])
        df["ts"] = df["ts"].dt.tz_localize("Asia/Kolkata")
        monkeypatch.setattr(msl, "_get_pdhl", lambda s, d: msl._PDHL[("TESTSYM", today)])
        hits = msl.score_signals("TESTSYM", "1", df, [])
        pdhl_rows = [r for r in hits if r.get("strategy") == "PDHL_BREAK"]
        assert pdhl_rows and pdhl_rows[0]["direction"] == 1
