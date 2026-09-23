# Best Intraday Chart Patterns for NSE Equity Stocks (5-Minute Timeframe)

> Researched 2026-09-23. Sources are index-based (`index` flag) unless marked `verified live`.
> Conventions used in all code-convertible rules: `ATR` = ATR(14) on 5-min candles; `VMA(n)` = mean volume of the last n 5-min bars;
> `vol_ratio` = bar_volume / VMA(20). Times are IST. "Buffer" = 0.1×ATR unless stated.

---

## Ranked Top-6 Shortlist

| Rank | Pattern | Direction | Core edge | Best NSE context |
|------|---------|-----------|-----------|------------------|
| 1 | Opening Range Breakout (15-min OR on 5-min candles) | Continuation | Institutional opening auction imbalance; self-fulfilling level | 9:30–11:30, high relative volume, NIFTY-aligned |
| 2 | Bull / Bear Flag (high-tight) | Continuation | Momentum + digestion, clean measured move | Trending days after 10:00; first pullback after morning drive |
| 3 | VWAP Pullback / VWAP Reclaim | Trend re-entry / regime change | Institutions defend VWAP as fair value | Post-10:00 trends; midday deep pullbacks; avoid choppy days |
| 4 | Ascending / Descending / Symmetrical Triangle | Continuation | Compression + supply absorption; volume contract→expand | Mid-session consolidation 10:30–14:00 |
| 5 | Double Top / Double Bottom | Reversal | Failed auction at day high/low, neckline confirms | After extended morning runs into overhead supply/resistance; 10:30–11:30 reversals |
| 6 | ABCD (AB=CD legs) | Continuation (legs 1–2) / reversal at D | Measurable legs, pullback depth filter, tight invalidation | First pullback of a trending day; 9:30–14:00 |

*Ranking basis: frequency of use + reported reliability among Indian intraday educators/practitioners and availability of backtested variants (notably the 2017–2026 Nifty ORB study, notes/source-01). Head & Shoulders is deliberately ranked out (see "Honorable mention") — on 5-min NSE candles it forms too rarely and too noisily intraday; practitioners report it more reliable on 15–30 min+.*

---

## Pattern 1 — Opening Range Breakout (ORB)

**1. Identification rules (5-min candles).**
- Opening range = high/low of the first three 5-min candles: 9:15, 9:20, 9:25 (i.e., bars with start_time in [09:15, 09:30)).
  - `OR_high = max(high)` over those 3 bars; `OR_low = min(low)`; `OR_width = OR_high − OR_low`.
- Minimum width filter: skip if `OR_width < 0.4 × ATR(14)` (equivalently the intradaylab backtest's "skip tiny ranges" rule: < 40 Nifty points on the index). Tiny ranges → false breakouts.
- No pattern exists before 9:30; entries only after the third bar closes. (30-min variant: 9:15–9:45, six bars — lower false-breakout rate, fewer opportunities.)

**2. Entry trigger.**
- LONG: first 5-min candle that **closes above** `OR_high`. SHORT: closes below `OR_low`.
- Practitioner variants: (a) enter on the breakout close; (b) enter on the *next* bar's open if breakout bar was decisive; (c) retest entry — wait for pullback to `OR_high` holding, then enter (lower fill rate, better R:R). Never enter while price is still inside the range.

**3. Candle confirmation.**
- Breakout bar volume: `vol_ratio ≥ 1.5` (Sharenox: >1.5× average; palify: 50%+ above average). Same-time-of-day average is stricter and preferred.
- Close must be beyond the level, not just a wick piercing it.
- Decisive body: `abs(close−open) ≥ 0.5 × (high−low)` of the breakout bar.
- Range filter: skip if breakout bar range > 2×ATR(14) — extended bars are exhaustion/chase entries and fail often.
- Skip entirely in the first 15 minutes and when a scheduled news event (RBI policy, results) is inside the window.

**4. Stop-loss (code rule).**
- Standard (structure-based): LONG `SL = OR_low − 0.1×ATR`; SHORT `SL = OR_high + 0.1×ATR` (intradaylab backtest uses the full-range SL; palify/Sharenox use just inside the range).
- Tight variant: `SL = breakout_bar_low − 0.1×ATR` (long). Better R:R but lower win rate.

**5. Target.**
- `T1 = breakout_price + 1.5 × OR_width` (long); full measured target `T2 = breakout_price + 2.0 × OR_width` (intradaylab's 2:1 rule).
- Hard time exit: square off by 14:30–15:15 regardless (brokers auto-square intraday ~15:15–3:20; IIFL advises exiting before 15:15).

**6. Best market context.**
- 09:30–11:30 IST primary window (best signal quality; also the 10:00–10:30 second-highest-volume block).
- Days with high relative volume (first-hour volume ≥1.5× its 10-day same-window average).
- Direction aligned with NIFTY/BANKNIFTY trend and with the gap direction; avoid ORB shorts on strong gap-up trend days.
- Works on NIFTY/BANKNIFTY futures and liquid large-caps (RELIANCE, HDFCBANK cited) on high-volume days.

**7. Failure modes / filters.**
- False-breakout rate is the known killer: ~40% on 5-min-defined ORB vs ~25% on 15-min ORB (palify). Prefer the 15-min definition for stocks.
- Wick-only pierces that close back inside the range within 1–2 bars → trap; require the close.
- Low-volume breakouts fail; news/event-driven opens produce whipsaws — stand aside.
- Breakouts late in the day (>14:30) have poor follow-through vs. risk; midday (12:00–13:30) breakouts frequently revert to the range.

---

## Pattern 2 — Bull / Bear Flag (high-tight flag)

**1. Identification rules.**
- Flagpole: an impulse leg of ≥4 consecutive same-direction 5-min candles with total rise (bull) `pole = pole_high − pole_low ≥ 1.0 × ATR(14)` and average body ≥ 60% of bar range (impulsive, not drifty). Pole typically completes in 3–10 bars.
- Flag: 3–10 consolidation bars after the pole, sloping slightly against the trend (down for bull flag) or horizontal, bounded by two roughly parallel lines (upper/lower). Tightness: `flag_height = max(flag highs) − min(flag lows) ≤ 0.5 × pole`.
- Validity: flag must not retrace more than 50% of the pole; volume must contract during the flag (each flag bar's volume < pole average).
- Mirror all rules for bear flags.

**2. Entry trigger.**
- LONG: 5-min candle closes above the flag's upper boundary line (or above `max(flag highs)` for a horizontal flag). Bear flag: close below lower boundary.

**3. Candle confirmation.**
- Breakout bar `vol_ratio ≥ 1.5` vs the flag bars' average volume (volume expansion after contraction is the core confirmation).
- Close beyond the boundary; no long opposing wick: `wick ≤ 0.4 × bar range`.
- Breakout bar range ≤ 2×ATR(14) (avoid chasing a vertical extension).
- Prefer the first clean breakout; a flag older than ~10 bars is stale — downgrade to watch-only.

**4. Stop-loss.**
- LONG: `SL = min(flag lows) − 0.1×ATR`. Alternative tighter: `SL = breakout_bar_low − 0.1×ATR`.
- SHORT: mirror: `SL = max(flag highs) + 0.1×ATR`.

**5. Target.**
- Measured move: `target = breakout_price + pole` (long); i.e., project the full flagpole height from the breakout point.
- Execution: book partial at 1R, trail remainder to measured target; minimum acceptable setup R:R at entry ≥ 1:1.5.

**6. Best market context.**
- Established intraday trend days, entries typically 10:00–13:00 on the *first* flag after the morning drive (later flags have lower follow-through).
- High relative-volume stocks (RVOL ≥ 1.5); mid-caps with news/catalyst trend better than mega-caps for flags.
- NIFTY trend aligned with the flag direction.

**7. Failure modes / filters.**
- Flag retraces >50% of pole → it's a reversal setup, not a flag; skip.
- Pole not impulsive (slow grind) → weak follow-through; require the ≥1.0×ATR, ≥4-bar impulse.
- Breakout into higher-timeframe resistance (day high, previous close, round number) → high failure; check overhead levels first.
- Breakout on contracting/flat volume → fakeout; require expansion.
- If you have to "squint" to see the flag, it isn't one (CTI pro-trader heuristic).

---

## Pattern 3 — VWAP Pullback / VWAP Reclaim

**1. Identification rules.**
- Session VWAP resets at 9:15 (compute from 9:15 cumulative typical-price × volume).
- Trend qualification (long): price above VWAP for ≥12 consecutive 5-min bars (≈60–90 min; the Kunal-Desai/MT5 rule) with higher highs and higher lows, VWAP itself rising. Short: mirror.
- Pullback: price drifts down to touch VWAP — `min(low) ≤ VWAP ≤ max(high)` or within `0.05×ATR` of VWAP — on declining volume (pullback bars' volume < impulse average).
- Reclaim variant: price below VWAP ≥12 bars, then a 5-min bar closes back above VWAP.

**2. Entry trigger.**
- Pullback long: enter on the close of the first 5-min reversal candle (hammer / bullish engulfing / strong close) that holds at/above VWAP, or on a break above that candle's high.
- Reclaim: enter on the 5-min close back above VWAP.

**3. Candle confirmation.**
- Hard filter: **no 5-min close below VWAP is allowed** on a long pullback entry — a close below VWAP signals a range/weak day, skip (dotnettutorials rule).
- Reversal candle volume expanding: `vol_ratio ≥ 1.25`.
- Reject entries where the pullback bar range > 2×ATR (panic flush, not an orderly pullback).
- First pullback of the day is highest probability; each subsequent touch is weaker.

**4. Stop-loss.**
- Pullback long: `SL = min(reversal_candle_low, VWAP − 0.15×ATR)`, i.e., below the reversal candle low with a small buffer; equivalently `SL = reversal_candle_low − 0.1×ATR`.
- Reclaim long: `SL = VWAP − 0.15×ATR` (below the reclaimed level).
- Short: mirror above.

**5. Target.**
- `T1 = prior swing high` (long) — the impulse high before the pullback; `T2 = day high`.
- Fallback rule: minimum 1.5R; take the nearer of (prior high, 1.5R) first (Sharenox: previous day's high or 1.5× risk, whichever first).

**6. Best market context.**
- Clean trend days after 10:00 AM; the deeper midday pullback (11:30–13:30) to VWAP is the classic entry.
- High relative volume; stock above both VWAP and previous-day VWAP for longs.
- Institutions benchmark to VWAP — this is the "institutional favorite" because funds buy dips to VWAP.

**7. Failure modes / filters.**
- Choppy/range days: price crossing VWAP ≥3 times by midday → VWAP has no directional authority; skip all VWAP trades that day.
- A 5-min close below VWAP on a long setup = invalidation, not a buying opportunity.
- Low-volume bounce at VWAP (no institutional participation) → fails.
- Late day: VWAP entries degrade after ~14:00 (algo-scalper research: valid only till ~14:00); square off by 15:15.
- News-driven parabolic stocks: VWAP pulls back too deep/fast; require the orderly-pullback filters.

---

## Pattern 4 — Triangle (Ascending / Descending / Symmetrical)

**1. Identification rules (5-min).**
- Ascending: horizontal resistance line touched ≥2 times (3+ ideal) with highs within `0.1×ATR` of each other; rising support line through ≥2 higher swing lows, converging toward resistance. Duration 6–24 bars (1–2 hours typical intraday; tradingsim notes up to 4–8 hrs on 5-min).
- Descending: mirror (flat support, descending resistance).
- Symmetrical: two converging lines, ≥2 touches each; direction = breakout direction (slight bias to prior trend).
- Volume must decline during formation vs. the preceding impulse.

**2. Entry trigger.**
- 5-min candle **closes** beyond the flat line (above resistance for ascending; below support for descending). Symmetrical: close beyond either boundary.
- Aggressive: half position on breakout close, add half on successful retest of the broken line. Conservative: wait for retest holding, enter on the bounce.

**3. Candle confirmation.**
- Breakout bar `vol_ratio ≥ 1.5` (ideally 2×) vs. the pattern's average bar volume.
- Decisive close beyond the line; reject wick-only pierces and long opposing wicks.
- Breakout bar range ≤ 2×ATR.
- Skip if the "flat" line's touches differ by >0.1×ATR (it's not a real level) or if the lower line isn't actually rising.

**4. Stop-loss.**
- Ascending long: `SL = most_recent_higher_low − 0.1×ATR` (below the rising trendline). If that distance makes R:R < 1:1.5, skip the trade rather than tightening.
- Symmetrical: `SL = breakout_bar extreme ∓ 0.1×ATR` (below breakout bar low for longs).
- Descending short: mirror.

**5. Target.**
- `target = breakout_price ± triangle_height`, where `triangle_height` = widest vertical distance between the two lines (measured at the pattern's start). This is the standard measured move.
- Time stop: if no follow-through within 3 bars of breakout, exit at breakeven (tradingsim rule).

**6. Best market context.**
- Mid-session consolidations (10:30–14:00) after a morning impulse — continuation into the afternoon.
- Ascending triangles in established uptrends; descending in downtrends. Best with RVOL ≥ 1.5 and NIFTY aligned.
- The pattern's apex timing matters: breakouts in the final third of the triangle (near apex) are cleanest.

**7. Failure modes / filters.**
- Seeing triangles everywhere: require the ≥2 clean touches and genuinely converging lines.
- Breakout without volume expansion → high fakeout rate; stand aside.
- Symmetrical triangles break either way — don't pre-position directionally; wait for the close.
- Breakout in the last 45 minutes of the session has poor measured-move completion; prefer earlier.
- Pattern too long/stale (apex passed, price drifting sideways) → downgrade to range, don't trade the "breakout."

---

## Pattern 5 — Double Top / Double Bottom

**1. Identification rules.**
- Double top: prior uptrend (higher highs over ≥10 bars), then two swing highs `H1, H2` with `|H2 − H1| / H1 ≤ 3%` (use `≤ 0.5×ATR` as the tighter intraday equivalent for large-caps), separated by 3–10 bars with a trough between them. The trough low = neckline.
- Double bottom: mirror (two lows within tolerance, peak between = neckline).
- Quality filters: second top on equal-or-lower volume than the first, or RSI(14) divergence (RSI makes a lower high at H2); the trough between peaks should be ≥0.5×ATR deep (shallow troughs = noise).

**2. Entry trigger.**
- 5-min candle **closes** below the neckline (double top) / above the neckline (double bottom). ~65% of twin peaks never confirm — never enter before the neckline close.

**3. Candle confirmation.**
- Volume expansion on the neckline-break bar: `vol_ratio ≥ 1.5`.
- Decisive close beyond neckline; reject wick pierces that snap back.
- Break bar range ≤ 2×ATR (avoid entering a vertical flush late).
- Retest entry (higher win rate, lower fill): wait for pullback to the neckline (occurs ~64% of the time per Bulkowski) and a rejection candle there; enter on rejection.

**4. Stop-loss.**
- Double top short: `SL = H2 + 0.1×ATR` (above the second peak — never at the neckline). Double bottom long: `SL = L2 − 0.1×ATR`.

**5. Target.**
- Measured move: double top `target = neckline − (H1 − neckline)`; double bottom `target = neckline + (neckline − L1)`.
- Expectation: ~64% of confirmed patterns reach the full measured move (Bulkowski, Adam & Adam); book partial at 1.5R regardless.

**6. Best market context.**
- After an extended morning run into a known ceiling: day high, previous-day high/close, round number — where supply is real.
- Classic intraday reversal windows 10:30–11:30 and 13:30–14:30 (documented reversal hotspots).
- Lower-RVOL, range-type days; mega-caps fading opening extremes.

**7. Failure modes / filters.**
- Entering before the neckline breaks (the 65%-unconfirmed trap) — the most common error.
- Strong trend days: double tops fail repeatedly when institutional flow is one-sided; require the RSI/volume-divergence filter and NIFTY not trending hard the same way.
- Peaks too far apart in tolerance (>3% / >0.5×ATR) → it's two separate swings, not a pattern.
- Neckline break on low volume → false breakdown; wait for expansion or the retest.
- Midday chop produces endless minor double-tops — require the ≥0.5×ATR trough depth and ≥10-bar preceding trend.

---

## Pattern 6 — ABCD (AB=CD legs)

**1. Identification rules (bullish-continuation ABCD on 5-min).**
- Leg AB: impulse of ≥5 bars, `|B − A| ≥ 1.5×ATR(14)`, in the trend direction (long: A→B up).
- Leg BC: pullback against the trend with retracement depth `0.382 ≤ (B − C)/(B − A) ≤ 0.786` (ideal 0.5–0.7; never shallower than ~0.3); 2–8 bars; volume contracts vs AB.
- Leg CD: resumption leg from C; projected completion zone `D ≈ C + (B − A)` (i.e., CD length 0.9–1.1× AB) for the continuation entry; for the reversal-fade variant, D is the measured completion point where you fade.
- Time symmetry: CD duration ≈ AB duration (±50%) adds reliability.
- Bearish ABCD: mirror.

**2. Entry trigger (two traded variants).**
- (a) Continuation — trade legs 1–2 only (BearBullTraders rule): enter long on the 5-min close above B (the AB extreme) as CD launches, i.e., the pullback-resumption breakout.
- (b) Reversal at D: at the projected D zone (`C ± 1.0×AB`), enter counter-trend on a 5-min reversal candle (hammer/engulfing) — fade the completed pattern.

**3. Candle confirmation.**
- Volume: strong on AB, contracting on BC, expanding on CD resumption.
- (a) The close above B must be decisive (body ≥ 50% of bar range), `vol_ratio ≥ 1.5`.
- (b) Require the reversal candle at D; never blind-fade the projected level.
- Skip if the stock is extended >2×ATR from its 9-EMA on 5-min (overextended — pullbacks cut deeper).
- Higher-timeframe gate: 15-min/60-min trend must agree with the ABCD direction (BearBullTraders filter).

**4. Stop-loss.**
- (a) Continuation long: `SL = C − 0.1×ATR` (below the pullback low — pattern invalidation).
- (b) Reversal fade: `SL = D ± 0.15×ATR` beyond the extreme (below D for bullish ABCD fade... define: fading a bearish ABCD at D → short with `SL = D + 0.15×ATR`).

**5. Target.**
- (a) Continuation: measured extension `target = entry + 1.0×AB` projected from C (i.e., the D point itself), or 1.5–2R minimum.
- (b) Reversal fade: minimal target 38.2% retracement of leg CD, ultimate 61.8% (Bramesh rule); move SL to breakeven once the 38.2% target prints.

**6. Best market context.**
- First pullback of a trending day, 09:30–14:00; strongest when the day already has a clear AB impulse (e.g., post-ORB drive).
- High relative volume with a catalyst; 60-min chart trend-aligned.
- Before 10:00, practitioners trade ABCD on 1-min with extra care (not inside a big second 5-min candle).

**7. Failure modes / filters.**
- BC retracement <30%: too shallow — the "pullback" is just a pause; continuation entries still work but D-fades fail (no real D).
- CD extends >1.27× AB: structure is an extension/impulse, not ABCD — don't fade it.
- Blind-fading projected D without a reversal candle — the top cause of losses on this pattern.
- Counter-trend ABCDs against the 60-min trend: skip (multi-timeframe disagreement kills win rate).
- Multiple overlapping ABCD projections (choppy market): no clean legs → no trade.

---

## Honorable mention — Head & Shoulders / Inverse H&S (excluded from top 6)

Well-documented (Bulkowski-based backtest on NSE large/mid-caps: 57% win rate, 1:1.8 avg R:R, daily timeframe; neckline-break entry, SL above right shoulder, measured-move target) but it forms too infrequently and too messily on 5-min intraday candles to make a 5-min top-6. On 15–30 min charts it remains a valid reversal pattern: neckline from the two reaction lows (tops), entry on close beyond neckline with volume, `SL = right_shoulder_extreme ± 0.1–0.2% buffer`, `target = breakout ∓ (head − neckline)`.

---

## NSE-Specific Intraday Structure Notes

**Session structure.**
- NSE equity normal market: **9:15 AM – 3:30 PM IST**; pre-open order collection 9:00–9:08 with equilibrium-price discovery (~9:08–9:12) before the 9:15 open.
- Brokers auto-square intraday (MIS) positions typically **3:15–3:20 PM**; plan all exits by ~15:15. Intraday equity effectively trades 9:15–~15:20.
- Thursday = weekly expiry: highest volume day, avg swings ±0.41%, direction a coin flip — reduce size or avoid pattern breakouts on expiry (10-yr Nifty study).

**Opening range significance.**
- 9:15–9:45 digests all overnight news/global cues; price formation here is high-volume but arguably less "efficient" (academic NSE finding). The range high/low becomes the day's reference auction bracket — hence ORB's edge: institutional opening imbalances resolve directionally ~78% continuation for 30–90 min (practitioner claim, palify).
- Practical rule used widely: don't trade the first 15 minutes; define the range, then trade its resolution.

**Volume windows (U-shaped day).**
- Academic NSE study: last half hour **15:00–15:30 carries 13–20% of total daily volume** (highest block); **10:00–10:30** is second highest (~8%+).
- Practitioner consensus: 9:15–10:15 = highest volatility + volume (opening spikes often the day's biggest swing, but "just as often faded as followed"); 12:00–13:30 = midday lull (lowest volatility, narrow candles — mean-reversion territory); 14:30–15:30 = closing frenzy (institutional/option-writer flows; ~57% of closing hours green — late-day rally bias).

**Gap behavior in NIFTY50 stocks.**
- Gaps vs previous close are routine (GIFT Nifty overnight cue). Practitioner observation: **small opening gaps frequently fill the same day or next**; gap-and-go trend days occur when the gap is large (>~1%) with strong volume and NIFTY-aligned breadth.
- Trading implication: gap-fade/mean-reversion logic is for small gaps in mega-caps; gap-continuation (ORB/flag) logic is for large gaps with volume confirmation. Never fade a large gap without a reversal candle.

**Why mean-reversion dominates mega-caps while momentum works in high-RVOL mid-caps.**
- Mega-caps (RELIANCE, HDFCBANK, INFY): deep institutional participation, tight spreads, heavy VWAP-benchmarked flow → price gravitates to VWAP; opening extremes get faded by arbitrageurs; breakouts need sustained institutional volume to follow through. Hence VWAP-pullback and double-top/bottom (fade-the-extreme) styles dominate here.
- High relative-volume mid-caps (2–3× RVOL with news/catalyst): thinner institutional anchoring, retail + momentum-algo participation → impulse legs extend, pullbacks are shallow, and continuation patterns (flags, triangles, ABCD, ORB) trend cleanly to measured moves.
- Coding translation: gate continuation-pattern entries on `RVOL ≥ 1.5` and prefer mean-reversion patterns (VWAP pullback, double tops/bottoms, gap fades) when `RVOL < 1.2` and the stock is a large-cap index constituent.

**Universal intraday risk rails (Indian practitioner consensus).**
- Risk 1–2% of capital per trade; minimum setup R:R 1:1.5 (intradaylab uses 2:1 on ORB).
- Volume confirmation threshold: breakout bars ≥1.5× average 5-min volume (≥2× ideal).
- No fresh entries after ~14:30–15:00; all positions flat by ~15:15.
- Align with NIFTY trend: don't take counter-index pattern trades; skip the first 15 minutes.

---

## Could not verify
- Exact statistical reliability rankings ("pattern X works Y% of the time on NSE 5-min") — no public NSE 5-min pattern-by-pattern backtest dataset was found; rankings above reflect practitioner/educator consensus plus the one long-horizon ORB backtest cited. Any reliability % quoted (e.g., Bulkowski's 64% measured-move, 65% unconfirmed) comes from US-market research, not NSE data.
- palify.io's "78% continuation" and "40%/25%/15% false-breakout" ORB figures are practitioner claims without published methodology — treat as directional, not precise.
- Whether extended trading hours (NSE proposal to 5 PM equity) are live: discussed by traders but not confirmed; current analysis assumes 9:15–15:30.

## Sources
- ORB backtest (Nifty, 2017–2026, 2,122 trades): https://intradaylab.com/blog/nifty-orb-breakout-strategy-backtest — `index`, notes/source-01
- ORB rules + false-breakout discussion: https://palify.io/articles/view-article/intraday-breakout-stocks — `index`
- ORB beginner guide (15/30-min variants): http://stockmarketforvaibhav.blogspot.com/2025/12/opening-range-breakout-orb-strategy.html — `index`
- NSE intraday system skill (ORB + VWAP continuation, trading windows 09:30–11:30 / 13:30–15:00): https://github.com/varuntandon1988-blip/indian-stock-trading/blob/HEAD/.claude/skills/nse-intraday-system/SKILL.md — `index`
- OpenAlgo intraday strategies (ORB code recipe, SBIN 5-min): http://openalgo.in/python/intraday-strategies — `index`
- VWAP pullback NSE playbook: sharenox.com intraday guide (full URL not returned by index; domain-level) — `index`, notes/source-02
- VWAP setups (pullback/short/reclaim rules): https://medium.com/@kunal00/vwap-trading-strategy-the-complete-guide-for-day-traders-2026-90c1cafe10c5 — `index`
- VWAP pullback framework (M5–M15): https://mt5toolbox.com/vwap-pullback-strategy/ — `index`
- VWAP time-correction/price-correction rules: https://dotnettutorials.net/lesson/vwap-trading/ — `index`
- VWAP + volume-profile confluence (valid till 14:00): https://github.com/shubhamtaywade82/algo_scalper_api/blob/HEAD/docs/OPTIONS_RESEARCH/implementation_plan.md — `index`
- Flag pattern rules (entry/SL/target/volume): https://trendspider.com/learning-center/chart-patterns-flags/ — `index`
- Bull flag structure (RIL NSE example, 15-min): https://joinfingrad.com/blog/bullish-flag-pattern-structure-and-trading/ — `index`
- Bull/bear flag playbook (breakout vs SMC entries, partials at 1:1): https://citytradersimperium.com/bull-flag-vs-bear-flag-patterns/ — `index`
- Bull flag blueprint (1–3% filter, 2:1 RR): https://www.visionfactory.org/post/mastering-the-bull-flag-pattern-a-trader-s-blueprint-for-spotting-high-potential-breakouts — `index`
- Ascending triangle tradability filters + time stop: https://www.tradingsim.com/blog/ascending-triangle?hs_amp=true — `index`, notes/source-03
- Double top/bottom rules (3% tolerance, volume): https://www.gate.com/crypto-wiki/article/what-is-the-double-top-and-bottom-pattern-how-to-identify-and-trade-20260116 — `index`
- Double top stats (65% unconfirmed, 64% measured-move, RSI-divergence filter): https://chartscout.io/double-top-pattern-crypto — `index` (Bulkowski-derived)
- Double top/bottom India guide (NIFM): https://www.onlinenifm.com/blog/post/6/technical-analysis/charting-pattern-double-top-and-double-bottom — `index`
- Double top/bottom (TradeBrains, India): https://tradebrains.in/understanding-double-top-and-double-bottom-chart-patterns/ — `index`
- ABCD pattern day-trading guide (5–15 min, Fib rules): https://www.tradingsim.com/blog/the-day-trading-abcd-pattern-explained?hs_amp=true — `index`
- ABCD rules (pullback depth, 9EMA, HTF gate): https://bearbulltraders.com/wp-content/uploads/2024/05/ABCD-Pattern-28th-of-may-2024.pdf — `index`, notes/source-06
- ABCD harmonic plan (NSE examples BPCL/Yes Bank, targets 38.2/50/61.8%): https://brameshtechanalysis.com/2018/08/15/how-to-trade-abcd-harmonic-pattern-2/ — `index`
- H&S backtest NSE large/mid-cap (57% win, 1:1.8 RR): https://medium.com/@strike.marketingteam/head-and-shoulders-pattern-meaning-4-types-trading-guide-and-our-backtest-result-c3df9221f183 — `index`
- H&S execution rules (SL buffer, measured move, volume): https://www.monetyra.com/trading-strategies/intraday-trading/head-and-shoulders-chart-pattern-explained — `index`
- H&S India guide (Rachana Ranade): https://www.rachanaranade.com/blog/head-and-shoulder-patterns-a-practical-guide-to-one-of-the-most-reliable-chart-formations — `index`
- NSE intraday volume U-shape (academic): http://amsterdam2018.econworld.org/papers/Gopalaswamy_Sampath_Intraday.pdf — `index`, notes/source-04
- 10-yr Nifty time-based patterns (hourly stats, reversal hotspots): https://medium.com/@stockdetails/unveiling-10-years-of-nifty-50-time-based-trading-patterns-what-the-data-really-tells-us-1c01d223875f — `index`, notes/source-05
- Intraday timing blocks India (IIFL): https://www.indiainfoline.com/knowledge-center/share-market/what-is-the-timing-of-intraday-trading?amp= — `index`
- Nifty gap-fill observations: https://in.tradingview.com/chart/NIFTY/G7uW85qS-Nifty-Gap-Theory-Study-Observation/ — `index`
- Nifty gap case study (failed breakdown/late reversal): https://medium.com/@vishnu.sivaprasadan/nifty-case-study-how-a-gap-down-open-turned-into-a-failed-breakdown-and-late-day-reversal-4ddb7bccd142 — `index`
