# DEPLOYMENT.md — Deploy Every Phase (0 → 10) Safely

Runbook for your `jomin2003/my_trader` bot (Render + Dhan + Telegram + Gist).
Every phase is **flag-gated and paper-preserving** — nothing changes trading
until you flip a flag, and each has a one-line rollback.

> **Golden rule:** deploy a phase with its flag **OFF**, confirm the app behaves
> exactly as before, *then* flip it **ON in paper**, watch a few sessions, then
> move on. Never merge a phase just because the app starts — it must pass the gate.

---

## Step 0 — Freeze the baseline (once)
```bash
git checkout -b phase-rollout
git tag -a baseline-v1 -m "frozen baseline (paper)"
git push origin baseline-v1
```

## Step 1 — Copy all files into your repo root
Every file in this bundle goes at the matching path (create `configs/`,
`src/my_trader/...`, `migrations/`, `docs/`, `scripts/`, `tests/`,
`.github/workflows/` as needed).

## Step 2 — Verify locally (must be green)
```bash
pip install -r requirements.txt pytest pyyaml
python -m compileall -q .          # 47 files compile
pytest -q tests                    # 17 passed
python scripts/diagnose_environment.py   # sanitized report, no secrets
```

## Step 3 — Render env vars (safe defaults = NO behaviour change)
```
AUDIT_ENABLED=1
EXIT_POLICY_V2_ENABLED=0
APP_PROFILE=paper
CONFIG_DIR=./configs
PERSISTENCE_BACKEND=gist
PORTFOLIO_RISK_ENABLED=0
```
Leave ALL existing vars as-is (`RR_GATE_ENABLED=0`, `ALLOC_MODE=shadow`,
`KRONOS_*`, secrets). Deploy. **Trading is unchanged.**

---

## Wiring the 2 existing files (Phase 1 & 2)

These modules must be called from YOUR `app.py` + `intraday_pattern_scanner_v2.py`.
Copy-paste these blocks.

### `app.py` — boot config report (Phase 0/1)
In `boot_restore()`, right after the BOOT banner:
```python
try:
    import config_report
    log.info("\n" + config_report.format_report(base_dir=BASE_DIR))
except Exception as e:
    log.warning(f"config report skipped: {e}")
```

### `app.py` — Telegram diagnostics (Phase 1)
In `telegram_webhook()`, add to the command chain before `elif cmd == "help"`:
```python
elif cmd == "mlstatus":
    import model_diagnostics as md; tg_send_to(chat_id, md.mlstatus_text())
elif cmd == "models":
    import model_diagnostics as md; tg_send_to(chat_id, md.models_text())
elif cmd == "config":
    import model_diagnostics as md; tg_send_to(chat_id, md.config_text(str(BASE_DIR)))
elif cmd == "decision":
    import model_diagnostics as md
    arg = text.split(maxsplit=1); sym = arg[1].strip().upper() if len(arg) > 1 else ""
    tg_send_to(chat_id, md.decision_text(sym) if sym else "Usage: /decision SYMBOL")
```
Add these to the `/help` text too.

### `app.py` — Phase 3 load-once config + validate at boot
Near the top of `boot_restore()`:
```python
from config_loader import load_settings
import config_validator as V
_settings = load_settings()
V.validate(_settings, base_dir=str(BASE_DIR), persistence_available=True)  # raises on bad config
log.info(f"config_version={_settings.version()}")
```

### `app.py` — Phase 10 health routes
```python
from health_service import liveness, readiness
@app.route("/livez")
def livez(): return liveness(), 200
@app.route("/readyz")
def readyz():
    rep, code = readiness(str(BASE_DIR)); return rep, code
```
Then install log redaction once at boot: `import logging_config; logging_config.install()`.

### `intraday_pattern_scanner_v2.py` — imports (Phase 1/2)
After your existing optional-import block:
```python
try:
    import decision_audit as _AUDIT, model_diagnostics as _MDIAG, reason_codes as _RC, config_report as _CFG
    _AUDIT_OK = True
except Exception as _e:
    _AUDIT_OK = False
```

### `intraday_pattern_scanner_v2.py` — observable RR + decision footer
In `place_bracket_orders`, replace the `rr_predictor.best_rr(...)` call with:
```python
_rec, _rr_status, _rr_reasons = rr_predictor.best_rr_ex(_feat, dirn)
if _rec:
    rr_override = (_rec["sl_mult"], _rec["tgt_mult"])
    if _AUDIT_OK: _MDIAG.COVERAGE.on_accept(symbol)
```
Just before the PAPER-entry `tg_send(...)`, build + attach the footer:
```python
_footer = ""
if _AUDIT_OK:
    _dec = _AUDIT.DecisionRecord(
        decision_id=_AUDIT.make_decision_id(strat, symbol, side),
        symbol=symbol, strategy=strat, side=side, signal_time=now_ist().isoformat(),
        baseline_exit_source=("structure" if (struct_sl and struct_tgt) else "atr"),
        final_exit_source=("rr_model" if rr_override else "atr"),
        direction_gate="passed", rr_model_status=_rr_status,
        kronos_status=("bypassed_rr_owns_exit" if rr_override else "na"),
        allocator_mode=(adaptive_allocator.ALLOC_MODE if _ALLOC_OK else "shadow"),
        risk_weight=float(alloc_w),
        model_version=(rr_predictor.diagnostics().get("model_version") if _RRGATE_OK else None),
        config_version=_CFG.config_version(), stop_price=sl, target_price=tgt,
        expected_rr=RISK_REWARD_RATIO, qty=qty)
    _AUDIT.record(_dec); _footer = "\n" + _dec.telegram_line()
tg_send(f"🧪 <b>PAPER ENTRY</b> {symbol} {side} [{strat}]\n"
        f"₹{entry_px} | SL ₹{sl} | TGT ₹{tgt} | Qty {qty}" + _footer, silent=True)
```

### `intraday_pattern_scanner_v2.py` — Phase 2 exit authority (optional, flag-gated)
Wrap the legacy exit block; when `EXIT_POLICY_V2_ENABLED=1`, call `exit_policy.resolve(...)`
instead (see `exit_policy.py` docstring). Add `kronos_exits.constraint_target()` so
Kronos only constrains, never rewrites.

---

## Phase-by-phase enable order (paper only)

| Order | Flip ON | Watch for | Rollback |
|---|---|---|---|
| 1 | `AUDIT_ENABLED=1` (already) | paper alerts show decision footer; `/mlstatus` works | `AUDIT_ENABLED=0` |
| 2 | `EXIT_POLICY_V2_ENABLED=1` | each trade logs exactly one exit source | `=0` |
| 3 | Phase 3 loader wired | boot prints `config_version`; bad config fails at boot | keep legacy constants |
| 4 | Phase 4 adapter | signals identical to baseline | adapter -> old module |
| 5 | `USE_SHARED_ENGINE=1` | backtest ≈ paper decisions | `=0` |
| 6 | `PERSISTENCE_BACKEND=sqlite` | restart keeps positions; no dup orders | `=gist` |
| 7 | (offline) `validate_dataset.py` | dataset quality report | keep CSV |
| 8 | (training) require full manifest | model refused if incomplete | don't register |
| 9 | `PORTFOLIO_RISK_ENABLED=1` | heat checked before each entry | `=0` |
| 10 | add `/readyz` + Docker | readiness fails on missing deps | additive |

---

## Render deployment changes summary
| Setting | Baseline | After all phases |
|---|---|---|
| Start command | `gunicorn app:app …` | `python deployment_check.py --preflight && gunicorn …` |
| Health check | `/healthz` | `/readyz` |
| New env vars | — | `AUDIT_ENABLED, EXIT_POLICY_V2_ENABLED, APP_PROFILE, CONFIG_DIR, PERSISTENCE_BACKEND, PORTFOLIO_RISK_ENABLED, USE_SHARED_ENGINE` |

## New Telegram commands
`/mlstatus` · `/models` · `/decision SYMBOL` · `/config` — add to `/help`.

## Rollback quick-reference
```
AUDIT_ENABLED=0
EXIT_POLICY_V2_ENABLED=0
APP_PROFILE=paper
PERSISTENCE_BACKEND=gist
PORTFOLIO_RISK_ENABLED=0
USE_SHARED_ENGINE=0
git checkout baseline-v1   # nuclear option
```

## Definition of Done (every phase)
complete files · behaviour preserved or versioned · tests · CI green · env changes
documented · Render changes listed · new Telegram commands documented · migration
steps · rollback steps · no secrets · paper validated · model+config versions in
diagnostics · runbook updated.
