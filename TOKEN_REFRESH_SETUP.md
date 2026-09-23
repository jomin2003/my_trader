# Dhan Token Auto-Refresh — Setup Guide

This adds a **fully automated 24/7 Dhan token refresh** to your Render deployment. After setup, you'll never manually update `DHAN_ACCESS_TOKEN` again.

---

## What changed

| File | Change |
|---|---|
| **`dhan_token_manager.py`** | **NEW** — handles TOTP-based refresh + persistent caching |
| **`scheduler.py`** | **UPDATED** — daily 08:30 IST refresh job + boot-time refresh + auth-failure recovery |
| **`requirements.txt`** | **UPDATED** — added `pyotp>=2.9.0` |

Copy these 3 files into your repo (replace old versions), commit, push. Render will auto-redeploy.

---

## One-time Dhan setup (5 min)

### 1. Enable TOTP on your Dhan account
1. Log into **web.dhan.co** → **My Profile** → **Access DhanHQ APIs**
2. Enable **TOTP** (2FA)
3. Scan the QR with Google Authenticator / Authy — but **before you close the setup screen**, click **"Can't scan? Enter manually"** and **copy the base32 secret** (e.g., `JBSWY3DPEHPK3PXP...`).
4. Verify the 6-digit code the app generates matches Dhan's expected code.

### 2. Note your Dhan PIN
The 6-digit numeric PIN you use to log into Dhan Web. If you've forgotten it, reset it in the app.

### 3. Verify with a manual test locally (recommended)
```bash
pip install pyotp requests
export DHAN_CLIENT_ID="1000000001"
export DHAN_PIN="123456"
export DHAN_TOTP_SECRET="JBSWY3DPEHPK3PXP..."

python dhan_token_manager.py refresh
python dhan_token_manager.py show
```

Expected output:
```
========================================
  Token acquired.
  Expires: 2026-07-23 08:30:15+05:30  (24.0h left)
  Saved to: ./data/.dhan_token.json
========================================
```

If this works locally, Render will work too.

---

## Render deployment (3 min)

### 1. Add 3 new environment variables
Go to Render dashboard → **trading-orchestrator** → **Environment**:

| Key | Value | Source |
|---|---|---|
| `DHAN_CLIENT_ID` | your Dhan client ID | Dhan Web → Profile |
| `DHAN_PIN` | your 6-digit PIN | your Dhan login PIN |
| `DHAN_TOTP_SECRET` | the base32 secret | from TOTP setup step 1 |

**Keep** `DHAN_ACCESS_TOKEN` env var as-is for the first deploy (it'll be used as bootstrap). After the first successful auto-refresh, you can delete it — the scheduler will always use `.dhan_token.json` on the persistent disk.

### 2. Push updated files & redeploy
```bash
git add dhan_token_manager.py scheduler.py requirements.txt
git commit -m "Add Dhan token auto-refresh"
git push origin main
```

Render auto-redeploys. Within 60 sec you should see:
```
🟢 Orchestrator ONLINE
🔑 Dhan token refreshed for today
```

---

## What now runs automatically

| Time (IST) | Job | What it does |
|---|---|---|
| **At every scheduler boot** | `_refresh_dhan_token()` | Checks cache; refreshes if <4h left |
| **Daily 08:30** | `job_refresh_token` | Fresh TOTP-based token, 45 min before market |
| **Mon–Fri 09:14** | `job_launch_scanner` | Belt-and-braces: refreshes token again, then launches scanner |
| **When scanner dies** | `watchdog` | Refreshes token first (in case auth was the failure), then restarts |
| **Every 15 min** | `job_health_check` | Alerts if token has <2h left |
| **Daily 18:00** | `job_daily_summary` | Includes token expiry in the daily recap |

The refresh **updates `os.environ["DHAN_ACCESS_TOKEN"]` in the running scheduler process**, so every subprocess launched afterwards (scanner, downloader, etc.) inherits the fresh token — no code changes needed in the 6 pipeline modules.

---

## Two-tier fallback logic

The token manager tries the cheapest option first:

```
if cached token has >4h left  →  reuse it (skip refresh)
else if cached token is still active  →  call RenewToken (extends 24h, no TOTP)
else  →  call generateAccessToken with fresh TOTP (bulletproof)
```

If **all three fail**, you get a loud Telegram alert with the exact error, and the scheduler continues running the non-Dhan jobs (yfinance data, etc.) so nothing else breaks.

---

## Kill switch / manual override

**Force a refresh right now (from Render Shell):**
```bash
python dhan_token_manager.py refresh --force
```

**Check current token status:**
```bash
python dhan_token_manager.py show
```

**Disable auto-refresh temporarily** (revert to env var):
```bash
rm ./data/.dhan_token.json
# Set DHAN_ACCESS_TOKEN in Render dashboard manually
```

---

## Security notes

1. **Never commit `.dhan_token.json` to git.** It's already blocked by `.gitignore` (`data/` is excluded).
2. **Keep `DHAN_TOTP_SECRET` in Render env vars only.** Never paste it in code, logs, or Slack.
3. **The token file has 0600 permissions** where the filesystem allows it (Render's disk does).
4. **If the TOTP secret leaks**, immediately disable TOTP on Dhan Web and re-enroll. That invalidates the leaked secret.
5. **The scheduler process's `os.environ` is only visible to child processes it launches** — Render's log stream doesn't dump env vars.

---

## Common issues

| Symptom | Likely cause | Fix |
|---|---|---|
| `SystemExit: DHAN_TOTP_SECRET env var is not set` | Missing on Render | Add it in dashboard |
| `TOTP generation failed (check DHAN_TOTP_SECRET is valid base32)` | Wrong secret / typo | Re-enroll TOTP on Dhan, copy secret carefully |
| `generateAccessToken HTTP 401` | Wrong PIN or expired TOTP | Verify PIN; check server clock is IST |
| `generateAccessToken HTTP 400` | Wrong client_id format | Should be all digits |
| Token refreshes but scanner still fails | Old scanner using stale env | Force scanner restart: kill from Render Shell |

---

## What you gained

- **Zero manual token updates** — the entire pipeline now runs unattended for weeks/months
- **Auto-recovery from auth failures** — if the scanner dies from expired token, watchdog refreshes + restarts
- **Full audit trail** — every refresh is logged with expiry time
- **Graceful degradation** — if refresh fails, you're alerted immediately (not at 09:15 when trading starts failing)

This is the last piece of the fully autonomous pipeline. Your bot is now truly 24/7.
