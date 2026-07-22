# Deploying the Trading Bot to Render (24/7)

This guide takes you from **local files → GitHub → Render** in about 30 minutes.

---

## What runs on Render

A single **Background Worker** process (`scheduler.py`) that stays alive 24/7 and cron-schedules everything:

| Time (IST) | What runs | File |
|---|---|---|
| Mon–Fri 09:14 | Launch live scanner subprocess | `intraday_pattern_scanner_v2.py` |
| Mon–Fri 15:25 | Force-kill scanner (safety) | — |
| Mon–Fri 15:35 | Download today's bars via Dhan | `csv_downloader.py` |
| Sun 19:00 | Weekly re-optimization pipeline | `csv_downloader → param_sweep → backtest → monte_carlo → live_config promote` |
| Every 15 min | Health check + scanner watchdog | (internal) |
| Daily 18:00 | Telegram summary | (internal) |

The scheduler restarts the scanner automatically if it dies during market hours and sends Telegram alerts for every failure.

---

## Cost (honest)

| Item | Plan | Monthly |
|---|---|---|
| Background Worker | Starter | **$7** |
| Persistent Disk (1 GB) | — | **$0.25** |
| **Total** | | **~$7.25** |

**Free tier is NOT an option** — free workers were removed and free web services spin down after 15 min idle. Your bot would stop trading mid-day.

---

## Prerequisites (5 min)

1. **GitHub account** (free) — https://github.com/signup
2. **Render account** (free signup) — https://render.com — you'll upgrade a single service later
3. **Dhan API credentials** — from https://dhan.co → Developer Portal → generate access token
4. **Telegram bot** — talk to `@BotFather`, run `/newbot`, save the token
5. **Telegram chat ID** — send a message to your new bot, then visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` — copy the `chat.id`

---

## Step 1 — Assemble the repo (5 min)

Create a folder locally and place **all 12 files** in it:

```
trading_bot/
├── .gitignore
├── Procfile
├── render.yaml
├── requirements.txt
├── README_DEPLOYMENT.md
├── scheduler.py                       ← orchestrator
├── intraday_pattern_scanner_v2.py    ← live scanner
├── backtest_harness.py                ← backtester
├── param_sweep.py                     ← grid optimizer
├── monte_carlo.py                     ← statistical test
├── csv_downloader.py                  ← data fetcher
└── live_config.py                     ← config bridge
```

**Do NOT include:** `.env`, `data/`, `logs/`, `live_config.json`, or `state.json` — the `.gitignore` blocks them, but double-check.

---

## Step 2 — Push to GitHub (5 min)

```bash
cd trading_bot
git init
git add .
git commit -m "Initial trading bot"
git branch -M main

# Create a NEW PRIVATE repo on GitHub (don't add README, .gitignore, or license)
# Then:
git remote add origin git@github.com:YOUR_USERNAME/trading_bot.git
git push -u origin main
```

**Make the repo PRIVATE.** Even without secrets in code, don't publish your strategy.

---

## Step 3 — Deploy on Render (10 min)

### 3a. Create the service via Blueprint

1. In Render dashboard → **New +** → **Blueprint**
2. Connect your GitHub, select the `trading_bot` repo
3. Render reads `render.yaml` and shows:
   - Service: `trading-orchestrator` (Background Worker, Singapore, Starter, 1 GB disk)
4. Click **Apply**

### 3b. Set your secrets

After the service is created, go to **trading-orchestrator → Environment** and click **Add Environment Variable** for each:

| Key | Value |
|---|---|
| `DHAN_CLIENT_ID` | your Dhan client id |
| `DHAN_ACCESS_TOKEN` | your Dhan token |
| `TG_BOT_TOKEN` | your Telegram bot token |
| `TG_CHAT_ID` | your Telegram chat id |

**Save.** Render will redeploy automatically.

### 3c. First-run bootstrap

Because the persistent disk starts empty, the very first sweep needs data. Trigger it manually:

1. In Render → **trading-orchestrator → Shell** (opens a terminal in the container)
2. Run:
   ```bash
   python csv_downloader.py --mode yfinance --preset nifty50 --out ./data
   python param_sweep.py --mode csv --csv-dir ./data \
       --nifty-csv ./data/NIFTY.csv --preset default \
       --walk-forward 0.7 --out ./sweep_out
   ```
3. If sweep produced results, promote them:
   ```bash
   python live_config.py promote --sweep ./sweep_out/sweep_LATEST.csv
   ```

You should get a `🟢 Orchestrator ONLINE` Telegram message within 30 seconds of the deploy.

---

## Step 4 — Verify it's working (5 min)

### Check Render logs
Render dashboard → **trading-orchestrator → Logs** — you should see:

```
YYYY-MM-DD HH:MM:SS | INFO | orchestrator | Orchestrator boot at ...
YYYY-MM-DD HH:MM:SS | INFO | orchestrator | Scheduler started. Jobs registered:
YYYY-MM-DD HH:MM:SS | INFO | orchestrator |   launch_scanner: next run = 2026-... 09:14:00+05:30
YYYY-MM-DD HH:MM:SS | INFO | orchestrator |   eod_kill: next run = ...
```

### Check Telegram
You should receive:
- `🟢 Orchestrator ONLINE` at boot
- `📊 Daily summary` at 18:00 IST
- `🚀 Live scanner started` at 09:14 IST on weekdays
- Any failure = an `⚠️` or `🚨` alert with details

---

## What the scheduler does when things break

| Problem | Auto-remediation | You get alerted? |
|---|---|---|
| Scanner crashes during market hours | Watchdog restarts after 60 sec cooldown | ✅ Yes |
| Data download fails | Job re-runs next weekday, keeps old CSVs | ✅ Yes |
| Sweep produces no results | Doesn't promote; live config unchanged | ✅ Yes |
| Monte Carlo p ≥ 0.05 | Doesn't promote; live config unchanged | ✅ (informational) |
| `live_config.json` > 30 days old | Scanner rejects it, falls back to hardcoded defaults | ✅ Yes |
| `live_config.json` corrupted | Scanner ignores it | ✅ Yes |
| Whole orchestrator crashes | Render auto-restarts the worker | ✅ Yes (via global except hook before crash) |
| Dhan API returns errors | Backoff + skip; keeps running | Only if persistent |

**The scheduler never edits your source code.** All auto-fixes are safe fallbacks. If a real code change is needed, the alert tells you what and where — you push a fix to GitHub, Render auto-redeploys.

---

## The kill switch

To stop trading immediately:

```bash
# Option 1: Pause the worker in Render dashboard (instant)
# Option 2: SSH via Render Shell
python live_config.py clear    # reverts scanner to conservative defaults
```

Or set a **big red environment variable**:

```
KILL_SWITCH=1
```

Then add this near the top of `scheduler.py`:

```python
if os.getenv("KILL_SWITCH") == "1":
    tg_send("🛑 KILL_SWITCH active — scheduler NOT starting jobs")
    while True:
        time.sleep(3600)
```

---

## Ongoing maintenance (mostly hands-off)

### Every Sunday evening (automatic)
The scheduler runs the full re-optimization. You receive a Telegram summary at ~20:30 IST. No action needed unless it fails.

### After every code change
Push to GitHub → Render auto-redeploys → you get a `🟢 Orchestrator ONLINE` Telegram. Done.

### Every 24 hours
The Dhan access token expires (SEBI rule). You'll get a `⚠️ Auth failed` alert. Update `DHAN_ACCESS_TOKEN` in Render env vars. Render will redeploy.

**Automate token refresh** by using Dhan's API-key based login and adding a token-refresh job — I can add that as a follow-up if you want.

---

## Common gotchas

1. **Wrong region = higher latency.** Deploy to **Singapore** (closest to India). Order execution roundtrip: ~150–200 ms vs 400 ms from US.
2. **Persistent disk isn't backed up.** If it fails, you lose your CSVs. That's fine — the weekly Sunday yfinance job rebuilds them from scratch.
3. **Free-tier Render CPU is throttled.** Sweeps take longer than on your laptop. Budget 60 min for the weekly job.
4. **Render workers can restart at any time** for platform maintenance. That's why every job has retry logic and state persistence (`state.json`).
5. **`live_config.json` lives on the persistent disk**, so it survives redeploys but not disk failure. The 30-day freshness gate protects you if it's lost.

---

## Health monitoring in one glance

Telegram becomes your dashboard. You get:
- **🟢 boot** = orchestrator (re)started
- **🚀 scanner start** = 09:14 IST weekdays
- **🎯 order attempt** = every signal (silent)
- **✅ order placed** = confirmed fills
- **🛑 SL hit** / **🎉 target hit** = OCO resolution
- **⚠️ warning** = something failed but recovered
- **🚨 fatal** = something failed and stopped
- **📊 daily summary** = 18:00 IST every day
- **🔧 weekly optimization** = Sunday 20:30 IST

If Telegram goes quiet for 24+ hours, check Render logs — the worker might have been paused.

---

## Support checklist when something breaks

1. Render dashboard → Logs (last 500 lines usually explain it)
2. `state.json` on the persistent disk shows last-run status per job
3. Telegram message history shows exact timestamps
4. Local test: pull the repo down and run `python scheduler.py` locally with the same env vars

---

## Files summary

| File | Committed to git? | Auto-generated? |
|---|---|---|
| `.gitignore` | ✅ | ❌ |
| `Procfile` | ✅ | ❌ |
| `render.yaml` | ✅ | ❌ |
| `requirements.txt` | ✅ | ❌ |
| `README_DEPLOYMENT.md` | ✅ | ❌ |
| `scheduler.py` | ✅ | ❌ |
| 6 pipeline modules (scanner, backtest, sweep, MC, downloader, live_config) | ✅ | ❌ |
| `.env` | ❌ (env vars in Render dashboard) | ❌ |
| `data/*.csv` | ❌ (persistent disk) | ✅ |
| `live_config.json` | ❌ (persistent disk) | ✅ |
| `state.json` | ❌ (persistent disk) | ✅ |
| `logs/*.log` | ❌ (persistent disk) | ✅ |
| `backtest_out/`, `sweep_out/`, `mc_out/` | ❌ (persistent disk) | ✅ |
