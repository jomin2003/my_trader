# Free-Tier Deployment: Render + External Cron

This adapts the trading bot to run on **Render's free web service** (₹0/month) using an external cron for scheduling. Total cost: **₹0**.

---

## The architecture change

**Before (paid, $7/mo):** One Background Worker running `scheduler.py` forever, with APScheduler + subprocess for the live scanner.

**Now (free, $0/mo):** One Flask web service (`app.py`) exposing HTTP endpoints. External cron (`cron-job.org`) hits those endpoints at market-timed intervals. A keep-alive ping every 5 min prevents Render from spinning it down.

```
cron-job.org (free)          Render free web service         Dhan / Telegram
     │                              │                              │
     │  every 5 min GET /healthz    │                              │
     │─────────────────────────────>│  (returns 200, stays awake)  │
     │                              │                              │
     │  Mon-Fri 9:15-15:30 IST      │                              │
     │  every 5 min POST /trigger/scan                              │
     │─────────────────────────────>│  scan_once + act + OCO       │
     │                              │─────────────────────────────>│
     │                              │                              │
     │  Mon-Fri 08:30 IST           │                              │
     │  POST /trigger/refresh       │                              │
     │─────────────────────────────>│  Dhan token via TOTP         │
     │                              │                              │
     │  Sun 19:00 IST               │                              │
     │  POST /trigger/weekly        │                              │
     │─────────────────────────────>│  sweep + backtest + promote  │
     │                              │  Backs up config to Gist  ────┴──> GitHub Gist
```

---

## Why persistence works despite ephemeral disk

Render's free tier wipes the filesystem on every restart. Solution:

| What | Where | Why |
|---|---|---|
| **`live_config.json`** | GitHub Gist (private, free) | Small, must survive cold starts |
| **Dhan token** | Refreshed on every cold start via TOTP | No need to persist |
| **CSVs (data)** | Re-downloaded from yfinance on cold start | Too large for Gist; free from yfinance anyway |
| **Trade blotter** | Telegram alerts (each order → message) | You get real-time record; Render logs also retain 7 days |
| **State counters** | In-memory (reset OK) | Non-critical |

---

## Cost breakdown

| Item | Price | Notes |
|---|---|---|
| Render Free Web Service | ₹0 | 512 MB RAM, 0.1 CPU, 750 hrs/mo |
| cron-job.org | ₹0 | Unlimited jobs, 1-min minimum |
| GitHub Gist | ₹0 | For config storage |
| Dhan API | ₹0 | Historical data + trading |
| Telegram | ₹0 | Alerts |
| **TOTAL** | **₹0/month** | |

---

## Hard tradeoffs vs the paid version

| Feature | Paid ($7/mo) | Free (₹0) | Impact |
|---|---|---|---|
| Always-on | Yes | Kept warm via cron | Cold start possible if cron misses |
| Persistent disk | 1 GB | None (Gist for config) | Weekly opt re-downloads data |
| CPU | Full core | 0.1 CPU (shared) | Sweep takes 3-5× longer |
| RAM | 512 MB dedicated | 512 MB (may swap) | Use `--preset quick` for sweep |
| Weekly Monte Carlo | Full 500 perms | Skipped by default | Add manually if RAM allows |
| Scanner as long-running loop | Yes | Per-request stateless | Slightly higher signal latency |

**If your first live month proves profitable, upgrade to paid** — it removes all these tradeoffs.

---

## Setup — 30 minutes total

### Prerequisites (same as paid deploy)
- GitHub account
- Render account (no credit card needed)
- Dhan API creds + TOTP enabled + PIN
- Telegram bot token + chat ID
- cron-job.org account (free)

---

### Step 1 — Assemble repo (5 min)

Your GitHub repo needs **13 files total**:

```
trading_bot/
├── app.py                           ← NEW: Flask web service
├── gist_storage.py                  ← NEW: config persistence
├── render.yaml                      ← NEW: free-tier config
├── requirements.txt                 ← includes Flask now
├── Procfile                         ← gunicorn startup
├── README_FREE_DEPLOY.md            ← this file
├── external_cron_config.md          ← cron-job.org schedules
├── .gitignore
│
├── intraday_pattern_scanner_v2.py   ← unchanged
├── backtest_harness.py              ← unchanged
├── param_sweep.py                   ← unchanged
├── monte_carlo.py                   ← unchanged
├── csv_downloader.py                ← unchanged
├── live_config.py                   ← unchanged
└── dhan_token_manager.py            ← unchanged
```

Push to a **private** GitHub repo.

---

### Step 2 — Create the GitHub Gist for config storage (2 min)

1. Go to **https://gist.github.com** → **New gist**
2. Filename: `live_config.json`
3. Content: `{}`
4. Click **Create secret gist** (NOT public!)
5. From the URL `https://gist.github.com/YOUR_USER/abc123def456...`, copy the long hex string → this is your `GITHUB_GIST_ID`
6. Go to https://github.com/settings/tokens → **Generate new token (fine-grained)**
   - Repository access: none
   - Account permissions: **Gists → Read and write**
   - Save token → this is your `GITHUB_TOKEN`

---

### Step 3 — Deploy on Render (5 min)

1. Render dashboard → **New** → **Blueprint** → connect GitHub repo
2. Render reads `render.yaml`, creates a **Free web service**
3. Apply → wait for first build to complete

### Step 4 — Set environment variables (3 min)

In Render dashboard → **trading-bot** → **Environment**, add:

| Key | Value |
|---|---|
| `DHAN_CLIENT_ID` | your Dhan client id |
| `DHAN_ACCESS_TOKEN` | your Dhan token (bootstrap) |
| `DHAN_PIN` | your 6-digit PIN |
| `DHAN_TOTP_SECRET` | base32 from Dhan TOTP QR |
| `TG_BOT_TOKEN` | Telegram bot token |
| `TG_CHAT_ID` | Telegram chat id |
| `CRON_SECRET` | any random string (e.g. `openssl rand -hex 24`) — **save this** |
| `GITHUB_TOKEN` | from Step 2 |
| `GITHUB_GIST_ID` | from Step 2 |

Render redeploys. Wait for the `🟢 App booted on Render` Telegram message.

### Step 5 — Note your Render URL

Something like `https://trading-bot-xyz.onrender.com`. **Save it.**

---

### Step 6 — Set up cron-job.org (10 min)

Sign up at **https://cron-job.org**. Create **6 jobs** as follows.

For every job:
- **Method:** GET (works fine; POST also OK)
- **Timezone:** Asia/Kolkata
- **Notification:** enable "on failure only"
- **Custom HTTP header:** add `X-Cron-Secret: <your_CRON_SECRET>`

| # | Title | URL | Schedule (IST) |
|---|---|---|---|
| 1 | `keepalive` | `https://YOUR_URL/healthz` | every **5 minutes** (`*/5 * * * *`) |
| 2 | `scan` | `https://YOUR_URL/trigger/scan` | every 5 min, **Mon-Fri 09:15-15:30** (`*/5 9-15 * * MON-FRI`) |
| 3 | `oco` | `https://YOUR_URL/trigger/oco` | every 5 min, **Mon-Fri 09:15-15:30** (offset by 2 min if possible) |
| 4 | `refresh_token` | `https://YOUR_URL/trigger/refresh` | daily **08:30** (`30 8 * * *`) |
| 5 | `download_data` | `https://YOUR_URL/trigger/download` | **Mon-Fri 15:35** (`35 15 * * MON-FRI`) |
| 6 | `weekly_opt` | `https://YOUR_URL/trigger/weekly` | **Sunday 19:00** (`0 19 * * SUN`) |

**How to add the header on cron-job.org:**
Advanced → HTTP Headers → Add → Name: `X-Cron-Secret`, Value: `<your CRON_SECRET>`.

### Step 7 — First-run bootstrap (5 min)

Because Render just booted with an empty filesystem, you need to seed data + config once.

Option A (simplest) — hit the weekly endpoint manually via browser:
```
https://YOUR_URL/trigger/weekly?secret=<your_CRON_SECRET>
```
Wait for `🔧 Weekly optimization started` on Telegram. Watch progress; ~15-25 min on free tier.

Option B (faster if you have data locally) — upload a `live_config.json` you already have to the Gist manually. Next cold start restores it.

---

## Verifying it works

### Test 1 — Health check
```
curl https://YOUR_URL/healthz
```
Expected: `ok` (200 OK)

### Test 2 — Status
Open in browser: `https://YOUR_URL/status`
Expected: JSON showing boot time, in-market status, state counters.

### Test 3 — Manual scan (during market hours)
```
curl -X POST https://YOUR_URL/trigger/scan \
     -H "X-Cron-Secret: YOUR_CRON_SECRET"
```
Expected: `{"ok": true, "signals": N, "scans_today": M}`. Check Telegram for signals.

### Test 4 — cron-job.org
In cron-job.org dashboard → click any job → **History** → verify recent runs show 200.

---

## Monitoring

Your dashboards are:
1. **Telegram** — all trading events + errors
2. **cron-job.org history** — proves cron is firing
3. **Render logs** — technical detail if something breaks

---

## Failure modes on free tier (and fixes)

| Symptom | Cause | Fix |
|---|---|---|
| Cold start at 09:15 misses first scan | Keep-alive failed for >15 min | Verify cron-job.org keepalive is running |
| `500 Internal Server Error` from `/trigger/scan` | Token expired mid-session | Check `/status` → look at `last_token_refresh`; hit `/trigger/refresh` |
| Weekly opt times out or OOMs | 0.1 CPU can't finish param_sweep | Switch preset to `quick` (already default); or run weekly opt via GitHub Actions (see below) |
| `live_config.json` missing after cold start | Gist not configured / token expired | Verify `GITHUB_TOKEN` scope is `gist:read/write`, `GITHUB_GIST_ID` is correct |
| Scanner running but no orders | `AUTO_TRADE_ENABLED = False` | That's the default. Set to True after paper-testing |
| Random 401 on cron endpoints | Header not being sent | Re-check cron-job.org Advanced → HTTP Headers |

---

## Bonus: run heavy weekly opt on GitHub Actions (free)

If Render's 0.1 CPU is too slow for your weekly sweep, run it on GitHub Actions instead (2000 min/month free). Save this as `.github/workflows/weekly_opt.yml`:

```yaml
name: Weekly Optimization
on:
  schedule:
    - cron: '30 13 * * SUN'   # 19:00 IST = 13:30 UTC
  workflow_dispatch:
jobs:
  optimize:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.11'}
      - run: pip install -r requirements.txt
      - run: |
          python csv_downloader.py --mode yfinance --preset nifty50 --days 59 --out ./data
          python param_sweep.py --mode csv --csv-dir ./data \
              --nifty-csv ./data/NIFTY.csv --preset default \
              --walk-forward 0.7 --out ./sweep_out
          python live_config.py promote --sweep ./sweep_out/sweep_*.csv --force
      - name: Push config to Gist
        env:
          GITHUB_TOKEN: ${{ secrets.GH_GIST_TOKEN }}
          GITHUB_GIST_ID: ${{ secrets.GH_GIST_ID }}
        run: python gist_storage.py backup live_config.json
```

Then delete the cron-job.org "weekly_opt" job — GitHub Actions handles it with a full CPU.

---

## Rules for going live

Same as the paid version:
1. Paper-trade for **≥ 10 trading days** with `AUTO_TRADE_ENABLED = False`
2. Only flip to `True` after paper results are within 30% of backtest
3. Keep `MAX_RISK_PER_TRADE = 500` for the first month
4. **Kill switch:** hit `/status` on browser → if things look wrong, in Render dashboard → **Suspend service**

---

## Migration path to paid

When you outgrow free tier (weekly opt too slow, or you want zero cold start risk):

1. Render dashboard → change instance type from Free → Starter ($7/mo)
2. Add a 1 GB persistent disk ($0.25/mo)
3. Set `mountPath: /opt/render/project/src/data`
4. Delete the cron-job.org keepalive job (no longer needed)
5. Optionally switch to Background Worker + `scheduler.py` from the paid deploy

Everything else — Dhan integration, live_config, Gist backup — keeps working unchanged.

---

## Files summary

| File | Committed to git? | Notes |
|---|---|---|
| `app.py`, `gist_storage.py`, `render.yaml`, `Procfile`, `requirements.txt` | ✅ | New for free-tier |
| 7 pipeline modules | ✅ | Unchanged from paid version |
| `README_FREE_DEPLOY.md`, `.gitignore` | ✅ | Docs |
| `.env`, `data/`, `live_config.json`, `sweep_out/`, `backtest_out/` | ❌ | Ephemeral / on Gist |
