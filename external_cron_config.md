# External cron configuration (cron-job.org)

The Render free tier has no built-in scheduler, so all timing comes from
**cron-job.org** (free) hitting the app's HTTP endpoints. This file is the
exact job list — recreate these 8 jobs if you ever rebuild the cron account.

Base URL: `https://YOUR_RENDER_URL` (e.g. `https://trading-bot.onrender.com`)

## Global settings (every job)

- **Method:** GET (POST also works)
- **Timezone:** Asia/Kolkata
- **Custom HTTP header:** `X-Cron-Secret: <your CRON_SECRET>`
  (Advanced → HTTP Headers → Add. The app rejects requests without it —
  never put the secret in the URL query string.)
- **Notifications:** on failure only

## Jobs

| # | Title | URL | Schedule |
|---|---|---|---|
| 1 | `keepalive` | `https://YOUR_RENDER_URL/healthz` | every 5 min: `*/5 * * * *` |
| 2 | `scan` | `https://YOUR_RENDER_URL/trigger/scan` | every 5 min Mon–Fri 09:15–15:30: `*/5 9-15 * * MON-FRI` |
| 3 | `oco` | `https://YOUR_RENDER_URL/trigger/oco` | every 5 min Mon–Fri 09:15–15:30, offset ~2 min from `scan` |
| 4 | `refresh_token` | `https://YOUR_RENDER_URL/trigger/refresh` | daily 08:30: `30 8 * * *` |
| 5 | `seed_gap` | `https://YOUR_RENDER_URL/trigger/seed-gap` | Mon–Fri 09:20: `20 9 * * MON-FRI` |
| 6 | `download_data` | `https://YOUR_RENDER_URL/trigger/download` | Mon–Fri 15:35: `35 15 * * MON-FRI` |
| 7 | `report` | `https://YOUR_RENDER_URL/trigger/report` | Mon–Fri 15:40: `40 15 * * MON-FRI` |
| 8 | `learn` | `https://YOUR_RENDER_URL/trigger/learn` | Mon–Fri 15:45: `45 15 * * MON-FRI` |

## What each job does

1. **keepalive** — dumb 200 ping; keeps Render's free tier from spinning down.
2. **scan** — runs `scan_once` + order placement (entry window only) + OCO check.
3. **oco** — standalone OCO/SL-target management between scans.
4. **refresh_token** — Dhan TOTP token refresh before market open.
5. **seed_gap** — writes today's gap rows so GapFill/GapGo can trade (skipped after 11:30 IST).
6. **download_data** — post-market: downloads bars, rebuilds `ob_data.csv` + `gap_data.csv`.
7. **report** — pushes the day's P&L report to Telegram.
8. **learn** — appends today's trades to the Gist ledger, runs the adaptive allocator.

## Deliberately NOT cron jobs

- **Weekly optimization** — runs as the GitHub Actions workflow
  `.github/workflows/weekly_opt.yml` (Sunday 19:00 IST). There is no
  `/trigger/weekly` endpoint; a cron job pointing at it would 404.
- **`/trigger/promote`** — manual-only. Promotes the latest local
  `sweep_*.csv`; Render's ephemeral disk never holds sweep files, so no
  cron job can use it. Call it by hand after a local sweep.

## GitHub Actions (also scheduled, not cron-job.org)

| Workflow | Schedule (IST) | Purpose |
|---|---|---|
| `.github/workflows/weekly_opt.yml` | Sun 19:00 (`30 13 * * SUN` UTC) | sweep → backtest → monte-carlo → conditional promote → Gist backup → Telegram |
| `.github/workflows/vol_trainer.yml` | Mon–Fri 16:15 (`45 10 * * 1-5` UTC) | trains vol model → pushes `vol_forecast.json` to Gist |

Neither workflow deploys to Render — the Gist is the promotion channel and
Render pulls it at boot.
