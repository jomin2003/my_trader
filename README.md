# my_trader — Phases 0→10 Bundle (COMPLETE, TESTED)

**One folder, everything in it.** All standalone modules for every phase, plus
tests (108 passing) and `DEPLOYMENT.md`. Drop these into your repo root.

> 📊 **Strategy research & profitability review (2026-09):** see
> [`docs/RESEARCH_2026-09.md`](docs/RESEARCH_2026-09.md) — published evidence
> per strategy, A/B results on 59 days of real NIFTY50 data, and the honest
> verdict on what currently makes money (spoiler: nothing yet, and why).

## ✅ Verify in 2 minutes
```bash
pip install -r requirements.txt pytest pyyaml
python -m compileall -q .          # all files compile
pytest -q tests                    # 108 passed
python config_registry.py          # prints config version + missing env vars (no secrets)
```

## 📁 What's here

The repo ships the live service plus its research/optimization stack. The
modules below are the ones actually present; several paths that older
"Phase 0→10" notes referenced (`configs/`, `scripts/`, `src/`,
`migrations/`, `scheduler.py`) were never committed — configuration
lives in `config_registry.py` + env vars, and scheduling is external cron.

| Area | Files |
|---|---|
| Live service | `app.py` · `intraday_pattern_scanner_v2.py` · `multi_strategy_live.py` · `vwap_reclaim_strategy.py` · `chart_patterns.py` |
| Config | `config_registry.py` · `config_report.py` · `live_config.py` |
| Indicators | `shared_indicators.py` · `indicators_ta.py` · `structure_levels.py` |
| Exits | `strategy_exits.py` · `kronos_exits.py` · `reason_codes.py` |
| ML gates | `rr_predictor.py` · `rr_features.py` · `kronos_gate.py` · `vol_gate.py` · `vol_trainer.py` · `adaptive_allocator.py` · `decision_audit.py` |
| Research | `backtest_harness.py` · `param_sweep.py` · `vectorbt_sweep.py` · `monte_carlo.py` · `quantstats_report.py` · `promote_profitable.py` |
| Data | `csv_downloader.py` · `precompute_order_blocks.py` · `precompute_gapfill.py` · `seed_today_gap.py` · `ob_data.csv` · `gap_data.csv` |
| Persistence | `gist_storage.py` · `state_store.py` · `strategy_ledger.py` · `dhan_token_manager.py` · `event_bus.py` · `metrics.py` |
| Risk | `portfolio_risk.py` · `exposure_tracker.py` |
| Deploy | `Dockerfile` · `Procfile` · `render.yaml` · `runtime.txt` · `deployment_check.py` · `health_service.py` · `logging_config.py` |
| Tests | `tests/` — bundle, fixes, new-strategies, chart-patterns (108 tests) |

> **2026-09-23 cleanup:** 21 dead/duplicate modules were removed (dead exit-policy
> V2 stack, dead model-governance stack, dead order-state-machine/reconciler/
> simulated-broker, the `ob_live` monkey-patcher, the legacy typed-settings
> config stack now consolidated into `config_registry`, and byte-duplicate CSVs
> under `research/`). The round-trip cost formula now lives in exactly one place
> (`cost_model.apply_costs`); `indicators_ta` reuses `shared_indicators`
> primitives instead of re-implementing them.

## ⚠️ Wiring notes
The scanner imports the strategy engine (`multi_strategy_live`) directly, so
no copy-paste wiring is required. Exit authorization is layered on the live
path: `compute_sl_target` → `kronos_exits` → optional `rr_predictor`
(RR gate off by default). The dead event-bus subscriptions (`trade_closed` →
allocator/ledger, `daily_pnl_ready` → allocator) were removed — nothing ever
emitted those events; the allocator and strategy ledger both run once from the
EOD `/trigger/learn` cron. `SCAN_WORKERS` (default 1) parallelises only the
network fetch phase of `scan_once`; detection/scoring stay sequential. See
**`DEPLOYMENT.md`** for the flag-by-flag rollout.


## 🚀 Deploy
Follow **`DEPLOYMENT.md`** — one phase at a time, flag OFF → verify → flag ON in
paper → watch → promote. Every phase has a rollback line.
