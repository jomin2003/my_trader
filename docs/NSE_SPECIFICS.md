# NSE India intraday — system specifics (what is India-specific where)

Every layer of this repo assumes **NSE equity intraday, IST market hours 09:15–15:30,
Dhan broker**. This page is the single reference for those assumptions.

## Timing

| Thing | Value | Where |
|---|---|---|
| Market window | 09:15–15:30 IST (`Asia/Kolkata`, ZoneInfo — not UTC) | `intraday_pattern_scanner_v2.py` MARKET_OPEN/MARKET_CLOSE |
| Bot flatten | **15:15 IST** — deliberately ahead of Dhan's intraday auto-square-off (~15:20–15:25) so the bot, not the broker, controls the exit | `FORCE_EXIT_TIME` |
| Gap seeding | 09:20 IST cron (`/trigger/seed-gap`) — today's first 5-min bar exists by then | `seed_today_gap.py` |
| Strategy windows | MR fade 10:00–14:30 · PDHL 09:45–14:30 · ORB/gap windows in `multi_strategy_live.py` | all IST |

## Costs (NSE equity intraday, Dhan) — see `config_registry.py`

| Component | Value | Side |
|---|---|---|
| STT | 0.025% | sell only (≈1.25 bps one-way effective) |
| NSE transaction | 0.297 bps | both sides |
| SEBI | 0.001 bps | both sides |
| Stamp duty | 0.3 bps | buy only |
| GST | 18% on (brokerage + txn + SEBI) | — |
| Dhan brokerage | ₹20 min per order → **₹23.6 with GST** | both sides |

Modeled as `TAXES_BPS_ONEWAY=2` + `BROKERAGE_PER_TRADE=23.6`. Round trip at the default
₹25k notional ≈ **29 bps** — any strategy whose average win is < ~0.6R at a 0.6% stop is
dead on arrival. All backtest verdicts dated before 2026-09-13 used the old 18 bps model
and are optimistic by ~11 bps round trip.

## Holidays

NSE holidays are **not** hard-coded. Behavior on a holiday is a graceful no-op:
the fetch returns no bars for today, the scan yields nothing, and `seed_today_gap`
writes no rows (its `_prev_trading_day` also skips weekends; bar-based prev-day
detection means a holiday-adjacent previous session is picked up from actual bars).
If you want the crons to skip entirely, add the NSE holiday list to cron-job.org's
schedule excludes — the bot itself does not need it.

## Universe & instrument details

- Universe gated to the **NSE F&O list** (`USE_FNO_UNIVERSE_ONLY`) — liquid, gappy names.
- Exchange segment `NSE_EQ`, instrument `EQUITY`, product `INTRADAY` (Dhan margin product,
  broker auto-square-off applies — see 15:15 note above).
- Tick size 0.05 (`OB_TICK_SIZE`) — standard NSE equity tick in the traded price band.
- Bar timestamps: Dhan returns epoch seconds; converted `unit="s", utc=True` → IST.
- Friday-skip flag (`FRIDAY_SKIP`) exists from the Angel One NSE weekday-distortion
  study — off by default, enable only after out-of-sample validation.
