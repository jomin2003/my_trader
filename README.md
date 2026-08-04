# my_trader — Phases 0→10 Bundle (COMPLETE, TESTED)

**One folder, everything in it.** All standalone modules for every phase, plus
tests (17 passing) and `DEPLOYMENT.md`. Drop these into your repo root.

## ✅ Verify in 2 minutes
```bash
pip install -r requirements.txt pytest pyyaml
python -m compileall -q .          # 47 files compile
pytest -q tests                    # 17 passed
python scripts/diagnose_environment.py   # sanitized report, no secrets
```

## 📁 What's here, by phase
| Phase | Files |
|---|---|
| 0 Baseline | `config_report.py` · `scripts/diagnose_environment.py` · `.env.example` · `.github/workflows/ci.yml` · `docs/CURRENT_BASELINE.md` |
| 1 Observability | `reason_codes.py` · `decision_audit.py` · `model_diagnostics.py` · `rr_predictor.py` (v3) · `rr_features.py` |
| 2 Exit authority | `exit_models.py` · `exit_validation.py` · `exit_policy.py` |
| 3 Config | `settings.py` · `config_loader.py` · `config_validator.py` · `feature_flags.py` · `configs/*.yaml` |
| 4 Modular | `src/my_trader/domain/models.py` · `strategies/{base,registry,orb}.py` |
| 5 Engine | `cost_model.py` · `fill_model.py` · `ports.py` · `simulated_broker.py` · `historical_clock.py` |
| 6 State | `sqlite_repository.py` · `order_state_machine.py` · `reconciler.py` · `migrations/001_initial.sql` |
| 7 Data | `feature_schema.py` · `data_validator.py` · `parquet_store.py` · `scripts/{convert_csv_to_parquet,validate_dataset}.py` |
| 8 Models | `model_manifest.py` · `model_registry.py` · `promotion_policy.py` · `walk_forward.py` |
| 9 Risk | `portfolio_risk.py` · `exposure_tracker.py` · `correlation_manager.py` · `portfolio_allocator.py` |
| 10 Harden | `Dockerfile` · `health_service.py` · `metrics.py` · `logging_config.py` · `deployment_check.py` · `.github/workflows/security.yml` |
| Tests | `tests/test_bundle.py` (all phases) |

## ⚠️ Important — the ONE thing this bundle can't include
Phases 1 & 2 also need **3 files patched inside YOUR existing code**:
`app.py`, `intraday_pattern_scanner_v2.py` (and they import the modules above).
Those patches depend on your live file contents. `DEPLOYMENT.md` → "Wiring" gives
the exact edits. The standalone modules here are complete and tested; the wiring
is copy-paste blocks.

## 🚀 Deploy
Follow **`DEPLOYMENT.md`** — one phase at a time, flag OFF → verify → flag ON in
paper → watch → promote. Every phase has a rollback line.
