# Strategy Research & Profitability Review — 2026-09

Deep-dive on the 8 strategies in this repo: what published evidence says about
each, what 59 days of real NIFTY50 5-min data (2026-06-23 → 2026-09-11, 47
symbols) says when backtested with the repo's own cost model, what was changed,
and what honestly does not work.

## 1. Sources

| Topic | Source | Key finding used |
|---|---|---|
| ORB | Concretum Research, "A Profitable Day Trading Strategy for the U.S. Equity Market" (7,000+ US stocks, 2016–2023) | 5-min ORB beats 15/30/60-min; trade only in the direction of the first candle; **relative volume is the dominant filter** (<100% RVOL: −0.02R, ≥100%: +0.08R); tight ATR stop, EOD flat |
| ORB (India) | dailybulls 30-min ORB backtest on Nifty futures (Jul–Oct 2025, 42 trades) | Fixed 1.5R cap beat ATR-trail and RSI-exit variants (57% WR, 0.28R avg); 1.5×ATR stop was too tight |
| Gaps | Vortex Capital "The Gap Map" (6,552 gap events, 2022–2026) + thetrading.tools SPY/QQQ gap-fill stats (1993+) | Small gaps (≤0.25%) fill same-day ~80% (fade works); **gaps >2% do NOT extend** (50.5% continuation); the 1–2% band is the only continuation edge (53.7%); continuation evidence peaks 09:45–10:30, not at the bell |
| VWAP | Prop-firm VWAP reclaim playbooks (2026) | Reclaim edge concentrated in the first 30–60 min; require confirmed closes + volume; stop below reclaim bar |
| Candlesticks | Marshall/Young/Rose (2006, DJIA bootstrap); Lund student thesis (OMXS30) | Bootstrap studies find **no predictive power** for classic patterns — standalone candle trades are ~coin-flips; only confluence can rescue them |
| Supertrend | Quantzee settings guide (2026) | (10, 3.0) on 5-min charts reacts hours late; intraday guidance (7, 2.0); always pair with trend-quality filter |
| SMC / Order blocks | FXNX 1,000-trade mechanical SMC audit | Standalone OB bounce rate 43.1% (negative edge); **FVG is the component with real edge** (64.8% mitigation) |

## 2. What the repo's own data says (baseline, 59 days, 47 NIFTY50 names)

Backtest harness with realistic costs (3 bps slippage + 6 bps one-way taxes
≈ 0.18% round trip), one entry per symbol per day, max 3 concurrent positions,
NIFTY soft gate, live exit profiles:

```
trades=233  wr=34.8%  PF=0.59  expR=-0.247  net=-Rs8,194  maxDD=9.3%
outcomes: 80 SL | 31 TARGET | 122 SQUAREOFF (52% never reach either level)
```

Per strategy (net ₹): VWAP Reclaim Long **−3,506** (66 trades) · SuperShort
**−1,381** · SuperLong −1,264 · Shooting Star −862 · Evening Star −882 ·
Morning Star −674 · VWAP Reclaim Short **+560** · ORB 2 trades (starved).

Structural findings:

1. **Backtest/live exit mismatch (bug, fixed).** The harness computed exits
   from global `RISK_REWARD_RATIO=2.0` instead of the live per-strategy
   profiles (RR 1.5), so every historical backtest validated different exits
   than the bot actually trades. Fixed in `backtest_harness.py`.
2. **MIN_SL_PCT floor dominates.** 5-min ATR on mega-caps is ~0.2–0.35%, so
   2×ATR ≈ 0.4–0.7% and the 0.6% floor clamps almost every stop to 0.6%.
   Changing the ATR multiplier barely moves anything — the floor is the real
   stop, and cost/R = 0.18/0.6 = 0.3 by construction.
3. **"Supertrend" is not a Supertrend.** `indicators_ta._supertrend_dir` is a
   single-bar band check that falls back to EMA20 direction; its
   `length`/`mult` args are near-no-ops (verified: identical flips for (10,3)
   and (7,2)). The live strategy is effectively an EMA-cross.
4. **ORB is starved, not bad.** ~0.08 raw ORB signals/symbol-day survive the
   stacked gates (range quality + close-confirm + prev-inside + vol 1.5× +
   VWAP + ADX). Concretum's version fires on every first-range break.
5. **The universe is the headwind.** NIFTY50 mega-caps are the most
   mean-reverting, lowest-RVOL-dispersion names on the exchange. The evidence
   base for every one of these patterns (Concretum "stocks in play", top-20
   RVOL; Vortex's crowded-but-real 1–2% gap band) lives in *active*, gappy,
   higher-volume names — the opposite corner of the market from this universe.

## 3. A/B experiments (15-symbol subset, same window)

| Variant | Trades | PF | expR | Net ₹ | Verdict |
|---|---|---|---|---|---|
| Baseline | 203 | 0.68 | −0.187 | −5,076 | — |
| Exits SL 1.2×ATR | 203 | 0.67 | −0.188 | −5,121 | no effect (floor clamps) |
| Exits SL 1.2×, RR 2.0 | 199 | 0.66 | −0.199 | −5,445 | worse |
| VWAP entry ≤11:30 | 171 | 0.54 | −0.273 | −6,559 | **worse** (research didn't transfer) |
| Supertrend (7,2.0) | 203 | 0.68 | −0.187 | −5,076 | no effect (params are no-ops) |
| GapGo cap 2.0% | 203 | 0.68 | −0.187 | −5,076 | no effect (gap trades rare) |
| Candle TA≥3 | 203 | 0.68 | −0.187 | −5,076 | no effect (TA already ≥2) |
| RVOL-first ranking | 203 | 0.68 | −0.185 | −5,005 | ≈0 (RVOL dispersion too low here) |
| ORB relaxed (vol 1.2, no VWAP) | 203 | 0.68 | −0.187 | −5,076 | ORB still starved |
| **NIFTY strict gate** | 176 | 0.79 | −0.121 | −2,801 | **+₹2,275** |
| **Block VWAP-RL + ST-shorts** | 149 | 0.76 | −0.134 | −2,756 | **+₹2,320** |
| **+ block candle-longs** | 130 | 0.82 | −0.098 | −1,691 | **+₹3,385** |
| **combo2 (all of the above)** | 108 | **0.88** | **−0.061** | **−988** | **−80% losses** |

## 4. Walk-forward validation (train 06-23→08-14, test 08-17→09-11)

See §4 table below (filled from the 4-run split on all 47 symbols).

<!-- WF_TABLE -->

## 5. What was changed in the repo

Code (all safe-by-default, env-flagged where regime-dependent):

- `backtest_harness.py` — exits now use the live per-strategy profiles;
  ranking keys extracted to `RANK_KEYS` for experimentation.
- `multi_strategy_live.py` —
  - `ORB_FIRST_CANDLE_DIR` (Concretum first-candle rule; default off);
  - `SIDE_BLOCK` env (block `(strategy,direction)` pairs; names normalized via
    `StrategyName`);
  - `GAPGO_MAX_PCT` 3.0→2.0 and entry window 09:45–11:00 (Vortex);
  - canonical `StrategyName` import.
- `research/ab_test.py`, `research/eval_split.py` — reusable A/B + walk-forward
  evaluators (dedup-state leak between runs fixed inside the harness loop).
- `docs/RESEARCH_2026-09.md` — this document.

Not changed (documented so nobody "fixes" them blind):

- **Side-block default is empty.** Blocking longs was regime-profitable in this
  window; it is a regime call, not a law. Set it via env when the ledger shows
  the same asymmetry live.
- **Strict NIFTY gate** is already env-driven (`NIFTY_GATE_ENABLED`,
  `NIFTY_STRICT`) — recommended ON, but it belongs to the live-config promotion
  path, not hard-coded.
- **`_supertrend_dir` approximation** left as-is; replacing it with
  `shared_indicators.supertrend` changes the strategy's behaviour and must be
  re-validated on its own before shipping.

## 6. Honest conclusions

1. **As configured, on this universe, none of the 8 strategies has positive
   expectancy after costs.** The best verified configuration still loses money
   (PF 0.88, expR −0.06R). The repo's own `PROFITABLE_ONLY` gate is doing
   exactly the right job by blocking live entries.
2. The two changes that actually moved the needle were **regime alignment**
   (strict NIFTY gate) and **cutting the losing sides** — both are
   risk-reduction, not new edge.
3. The published edge cases (Concretum ORB, Vortex gap band) live in
   **high-RVOL "in-play" names**, not fixed mega-cap lists. The highest-leverage
   next experiment is universe selection: rank the day's candidates by opening
   RVOL and trade only the top decile, instead of scanning a static NIFTY50
   list with turnover filters.
4. A 59-day single-regime window is too small to certify any of this. The
   weekly optimizer + ledger already in the repo are the right instruments;
   the side-block and gate flags above are the knobs it should be turning.

## 7. Additions (2026-09-13): two research-backed strategies + audit

Deep-research sources added this round:

| Topic | Source | What was taken |
|---|---|---|
| RSI-2 / Double-7 | Larry Connors & Cesar Alvarez, *Short Term Trading Strategies That Work*; QuantifiedStrategies 2024 re-test (SPY 1993→2026: 154 trades, 82.5% WR, PF 2.58) | Mean-reversion entry logic + high-WR/small-RR exit shape |
| NR7 / Stretch | Toby Crabel, *Day Trading with Short Term Price Patterns and ORB* (1990); EdgeLab 2026 re-test (1,850 trades, OOS Sharpe 0.87 SPY) | Volatility-contraction → expansion concept (feeds future ORB gating) |
| PDH/PDL | tradingstats.net (1,691 NQ sessions, 2015–2025): 80.8% of inside-open sessions break ≥1 level | Level definitions, inside-open filter, fresh-cross entry |

**New strategy 1 — MR_VWAP_FADE** (`MR_VWAP_FADE`): fade ≥2σ extensions from session
VWAP with RSI(2) ≤10 (long) / ≥90 (short), 10:00–14:30, volume-spike veto (>1.3× avg
= news continuation, skip). Exit profile: SL 1.2×ATR, RR 0.8, trail 0.5×ATR — the
book's first high-WR/small-RR profile. Rationale: the repo's own §2 finding that
NIFTY50 mega-caps are the most mean-reverting names, while all 8 existing strategies
are momentum. Gaps: `MR_FADE_*` env vars, default ON for detection, live-blocked by
the allowlist.

**New strategy 2 — PDHL_BREAK** (`PDHL_BREAK`): fresh-close break of previous-day
high/low, open-inside-prior-range filter, vol ≥1.2×, 09:45–14:30. Levels seeded daily
at 09:20 IST by `seed_today_gap.py` (new `seed_gap` cron job) into `pdhl_data.csv`.
Exit profile: SL 1.5×ATR, RR 1.5, trail 1.0×ATR. Gaps: `PDHL_*` env vars.

**Also fixed this round (see [FIXES_2026-09-13.md](FIXES_2026-09-13.md) for the full
audit):** live OCO sibling-cancel + real soft-exit orders (was phantom exits → net-short
risk), cold-start position wipe, OCO thread race, NIFTY_STRICT env wiring, ADX fail-open
on flat tape, honest Dhan cost model (brokerage ₹20/order now included — old backtests
undercharged ~11 bps round trip), Telegram webhook auth, ledger dedup + PARTIAL
exclusion, OB intraday revival (yesterday-zone retests), Kronos stale fail-closed.
Tests: 65 → **89**.
