# Cleanup report — 2026-09-23

Full-repo cleanup of `my_trader`: dead code removed, duplicated logic
consolidated, one config system, one genuine bug fixed, one opt-in
performance improvement. **Default runtime behavior is unchanged** except
where explicitly flagged below.

Test suite: **83 passed** (baseline before cleanup: 95 passed; the 12
removed tests covered only deleted dead modules).

## Feature — top-6 NSE intraday chart patterns (added 2026-09-23)

Deep-research-backed (report: `research/nse-intraday-chart-patterns-20260923.md`;
ranking = practitioner/educator consensus, NOT NSE-verified profitability evidence):

| Rank | Pattern | Implementation |
|---|---|---|
| 1 | Opening Range Breakout | existing detector kept; exits upgraded to pattern geometry via `chart_patterns.orb_exits()` (SL = opposite OR extreme ± 0.1× ATR; T1 = entry + 1.5× OR width) |
| 2 | Bull / Bear Flag | new `chart_patterns.detect_flag()` |
| 3 | VWAP Pullback / Reclaim | existing detector kept; exits upgraded via `chart_patterns.vwap_exits()` (pullback: SL past reversal candle ± 0.1× ATR; reclaim: SL past VWAP ± 0.15× ATR; target = prior swing, 1.5R floor) |
| 4 | Triangle (asc/desc/sym) | new `chart_patterns.detect_triangle()` |
| 5 | Double Top / Bottom | new `chart_patterns.detect_double()` |
| 6 | ABCD (continuation + fade-at-D) | new `chart_patterns.detect_abcd()` |

Rules enforced for every pattern entry: 5-min **close** beyond the level
(wick pierces rejected), breakout volume ≥ 1.5× 20-bar average, decisive
body ≥ 50% of range, opposing wick ≤ 40%, bar range ≤ 2× ATR, minimum
1.5R at entry (ORB uses the report's native ~1.3R geometry with a 1.0R
chase filter), no new entries after ~14:30 IST. `confirm_entry()` in
`chart_patterns.py` is the final pre-entry backstop applied by the scorer
before any paper/live entry. Pattern-native SL/target travel on the rows
as `struct_sl`/`struct_target`, which the scanner already prefers over the
ATR profile. New strategy tags registered: `FLAG`, `TRIANGLE`,
`DOUBLE_TOP_BOTTOM`, `ABCD` (registry + exit-profile fallbacks).

Tests: 25 new in `tests/test_chart_patterns.py` (synthetic 5-min sessions
per pattern variant, confirmation accept/reject cases, exit-math unit
tests, session-gate, scorer wiring). Full suite: **108 passed**.
These patterns are **unproven** — paper/unproven until backtested and
promoted through the existing profitability allowlist like every strategy.

## Deleted — dead modules (verified: zero live callers, zero cron/test references)

| File | Why |
|---|---|
| `ob_live.py` | Dead; worse, it monkey-patched scanner globals on import |
| `ports.py`, `historical_clock.py` | Unwired abstractions, never used |
| `order_state_machine.py`, `reconciler.py` | Unwired; startup reconciliation never ran |
| `simulated_broker.py` | Unwired paper-broker abstraction |
| `backtest_replay.py` | Dead duplicate of `backtest_harness.py` (with lookahead bias) |
| `parquet_store.py` | No callers |
| `exit_policy.py`, `exit_models.py`, `exit_validation.py` | Exit-policy V2 stack: never wired into the live path (flag default off, no callers) |
| `model_registry.py`, `model_manifest.py`, `promotion_policy.py` | Model-governance stack: no callers |
| `walk_forward.py` | No callers (`param_sweep.py` has its own local walk-forward) |
| `fill_model.py`, `correlation_manager.py` | No callers |
| `settings.py`, `config_loader.py`, `config_validator.py`, `feature_flags.py` | Legacy typed-settings config stack — **consolidated into `config_registry.py`** (see below) |
| `research/ob_data.csv`, `research/gap_data.csv` | Byte-identical duplicates of the live root CSVs |

## Consolidated

- **One config system.** `health_service.readiness()` (used by `/readyz` and
  `deployment_check.py`) now validates against `config_registry` instead of
  the deleted legacy stack. Same return shape `{status, problems,
  config_version}` + HTTP code. The two systems had divergent defaults
  (e.g. `max_open_positions`); now there is one source of truth.
- **One cost formula.** `cost_model.apply_costs()` is now the single
  implementation; `intraday_pattern_scanner_v2._apply_costs` and
  `backtest_harness._apply_costs` delegate to it (this also fixes
  `monte_carlo.py`, which imports the harness's copy). Verified
  **bit-identical** on 2000 randomized cases — note the old
  `CostModel.net_pnl` did *not* include slippage, so it was *not* used;
  the shared formula preserves the exact slippage-on-fills math both
  callers had.
- **One set of indicator primitives.** `indicators_ta._ema` and
  `_macd_hist` now delegate to `shared_indicators.ema` / `macd_hist`
  (identical math, verified identical outputs). Both `confluence_score`
  variants are kept — they are genuinely different scorers used by
  different live callers.
- **Dead event wiring removed.** `_wire_events()` subscribed
  `trade_closed` → allocator/ledger and `daily_pnl_ready` → allocator, but
  nothing ever emitted those events. Removed; the allocator and strategy
  ledger both run once from the EOD `/trigger/learn` cron (the single
  writer). Live `signal_found`/`trade_entered` → metrics subscriptions kept.
- **Allocator sizing made honest.** `place_bracket_orders` gated
  `adaptive_allocator.get_weight()` behind `_ALLOC_OK`, which could never
  become `True` — the call was a silent no-op. It now attempts the weight
  directly (guarded `try/except` → 1.0). In shadow mode (default)
  `get_weight` returns 1.0 by design, so default behavior is unchanged;
  active mode now actually works.

## Fixed (behavior change — flagged)

- **`shared_indicators.rsi` read the wrong window.** It averaged the
  *oldest* `period` deltas (`up[:period]`) instead of the trailing window,
  so the scanner's confluence RSI reflected momentum from hours ago. Now
  uses the most recent `period` bars. Docstring corrected (simple-average
  RSI, not Wilder smoothing). Existing test still passes.
- **Stale docs:** `vwap_reclaim_strategy.py` claimed `sl 1.4xATR / rr 2.2`;
  the live `strategy_exits.py` profile is `sl 2.0xATR / rr 1.5` — docstring
  corrected. README module table, wiring notes, and DEPLOYMENT.md updated
  for all removals (including `EXIT_POLICY_V2_ENABLED`, which no longer
  does anything).

## Faster (opt-in, default unchanged)

- `scan_once` gained **`SCAN_WORKERS`** (default `1` = today's sequential
  loop, unchanged). Set `SCAN_WORKERS=4` to parallelise the network fetch
  phase with a thread pool; pattern detection and scoring stay sequential
  because `score_signals()` uses a module-global scratch frame (`_CUR_DF`).
  The per-symbol `REQUEST_SLEEP_SEC` pacing is kept between submissions.

## How to adopt

This cleaned tree is a drop-in replacement for the repo contents: copy it
over, `pip install -r requirements.txt`, run `pytest tests/`, redeploy.
No env-var changes are required (only the new optional `SCAN_WORKERS`).
