# Expert Review — Response & Empirical Follow-Up

> **Single file for sharing:** [`SIMONA_COMPLETE_DOCUMENTATION.md`](SIMONA_COMPLETE_DOCUMENTATION.md) — includes this response, full spec, and latest metrics.  
> **Architectural optimization feedback (May 2026):** [`ARCHITECTURAL_REVIEW_RESPONSE_MAY2026.md`](ARCHITECTURAL_REVIEW_RESPONSE_MAY2026.md)

**Date:** May 2026  
**Inputs:** External technical review + `review_feedback_analysis.py` → `review_feedback_analysis_results.json`  
**Related:** [`ALGORITHM_SPECIFICATION.md`](ALGORITHM_SPECIFICATION.md)

This document maps each reviewer critique to our position, new data, and planned actions.

---

## Executive summary

We **accept the review’s central thesis**: the system is well-specified and honestly backtested, but **sleeve-level inference is weak** (~31 trades/sleeve), **book-level headline DD understates deployed risk**, and **live realism gaps** (venue, slippage, funding if perps) need tightening before scaling capital.

**What we ran in response:**

| Analysis | Key finding |
|----------|-------------|
| PF bootstrap (250 trades) | Book PF 3.58, **95% CI [2.56, 5.00]** — pooled edge plausible |
| Exhaustion cap cohort (>225 bps) | Crypto “stretched” breakouts show **positive** avg 5d forward returns; cap may **exclude** momentum |
| Utilization | Mean **7.7%** of book equity deployed; **53%** of days have ≥1 position |
| BTC Dukascopy vs Binance | **61/69** exact signal-day match (~79%) |
| Funding stress (if perps) | ~**$2,127** drag at 0.01%/8h ≈ **2%** of net PnL — not fatal at low funding |
| 2022+ calendar holdout | **146 trades**, PF **3.73**; equity-path DD metric **needs fix** (see §6) |

**Our stance unchanged on live config:** keep **max 4 concurrent**; paper-trial **max 5** only; do **not** promote partial-exit stack without paper confirmation.

---

## 1. Sample size (reviewer: central weakness)

### Reviewer point

~250 trades / 8 sleeves ≈ **31 trades per sleeve** — insufficient to prove sleeve-level edge. High ex-2024 PF on ETH/BNB may be luck. Book-level 105% / Sharpe 1.66 is more credible but spans a bullish long-only era.

### Our response — **Agree**

| Level | Trades | PF | Bootstrap 95% CI |
|-------|--------|-----|------------------|
| **Book (full)** | 250 | 3.58 | **[2.56, 5.00]** |
| **Book (ex-2024)** | 89 | 4.18 | **[2.38, 7.23]** |
| BTC (ex-2024) | 14 | 1.79 | **[0.41, 7.37]** |
| ETH (ex-2024) | 9 | 5.51 | **[0.86, 30.96]** |
| BNB (ex-2024) | 13 | 1.81 | **[0.32, 7.22]** |

**Interpretation:** Book-level PF CI **excludes 1.0** at the low end → pooled edge is statistically plausible. **Sleeve ex-2024 CIs are wide and include ~1.0** → we will **not** claim sleeve-level OOS proof for BTC/BNB; ETH ex-2024 looks good but on **9 trades**.

**Actions:**

- Report **book metrics first**, sleeve metrics second with trade counts and CIs.
- Treat **ETH/BNB** as promising, **BTC** as unproven post-2024, **SOL/DOGE** as short history.
- Avoid further parameter tuning on ex-2024 windows.

---

## 2. Exhaustion cap (reviewer: may filter best trades)

### Reviewer point

225 bps cap may skip the strongest crypto continuations (3–8% breaks). Need cohort analysis: what happens when `breakout_bps > 225`?

### Our response — **Partially agree; data favors testing a higher cap**

Cohort: raw breakout + regime on, but **capped out** by `breakout_bps > 225` (forward returns from signal close — **not** full trade simulation with stops/holds):

| Symbol | n stretched | Median bps | Mean 5d fwd % | % 5d positive |
|--------|-------------|------------|---------------|---------------|
| BTCUSD | 116 | 421 | **+2.3** | 55% |
| ETHUSDT | 163 | 458 | **+3.1** | 57% |
| BNBUSDT | 152 | 454 | **+5.2** | 55% |
| SOLUSDT | 113 | 559 | **+5.5** | 63% |
| DOGEUSDT | 92 | 811 | **+15.4** | 49% |
| XAUUSD | 14 | 255 | **−1.1** | 50% |
| XAGUSD | 49 | 322 | ~0 | 59% |
| BRENT | 30 | 330 | +0.2 | 53% |

**Interpretation:**

- **Crypto:** stretched breakouts are **not** obvious losers on 5d forward close-to-close; cap may be **leaving momentum on the table**.
- **Gold:** tiny sample, slightly negative 5d — cap may help on XAU.
- **DOGE:** huge forward means, coin-flip win rate — tail-driven; cap may still be rational for risk.

**Actions:**

- Run proper backtest: `max_breakout_bps` ∈ {225, 300, 400, none} per asset class (crypto vs metals).
- Do **not** loosen cap book-wide without sleeve-level DD check.

---

## 3. Funding rates (reviewer: biggest unmodeled cost if perps)

### Reviewer point

If execution is on USDⓈ-M perps, funding can be 2–15%+ annualized — material vs 8.95% CAGR.

### Our response — **Clarify venue; stress test done for perp scenario**

| Fact | Detail |
|------|--------|
| **Current codebase** | Binance **spot** daily klines; `execution_venue: spot_daily_klines_paper_only_not_perps` |
| **Stress (IF perps)** | 0.01% per 8h on crypto `entry_notional` over hold days → **~$2,127** total drag |
| **As % of net PnL** | **~2.0%** |
| **As % of $100k** | **~2.1%** |

At **0.01%/8h**, funding is **noticeable but not strategy-breaking** on this replay. At **0.05%/8h** (hot bull), drag scales ~5× → **~10% of PnL** — material.

**Actions:**

- State explicitly in spec: **paper = spot signals**; live execution venue TBD.
- If live uses perps: model funding in sim or switch to spot.
- Re-run `funding_skip` OOB with real funding series (was no-op at 3 bps threshold).

---

## 4. BTC data source (reviewer: signal drift)

### Reviewer point

Dukascopy BTCUSD vs Binance execution → ±1 day signal mismatch.

### Our response — **Agree; fix is straightforward**

| Metric | Value |
|--------|-------|
| Dukascopy signal days | 69 |
| Binance BTCUSDT signal days | 69 |
| Exact same UTC day | **61 (79%)** |
| Only Dukascopy | 8 |
| Only Binance | 8 |

**Actions:**

- **Move BTC sleeve to Binance BTCUSDT** for signal generation if Binance is execution venue.
- Re-baseline book metrics after switch (expect small trade-count drift).
- Document remaining 8+8 day mismatches in validation appendix.

---

## 5. Book DD vs utilization (reviewer: flattering headline)

### Reviewer point

−1.61% book DD reflects **low capital utilization**, not safe per-position risk. Sleeve DD to −13% (SOL); Sharpe on ~3.6% ann. vol.

### Our response — **Agree — add utilization-adjusted reporting**

| Metric | Value |
|--------|-------|
| Mean deployed notional | **$9,581** (~9.6% of $100k) |
| Max deployed notional | **$70,297** (~70% of book) |
| Mean utilization (deployed / book equity) | **7.7%** |
| Max utilization | **75.7%** |
| Days with ≥1 open position | **53.3%** |
| Worst sleeve DD | **−13.0%** (SOLUSDT) |

**Interpretation:** Book DD is low because **most capital is idle most of the time**, not because positions are low-risk. Sharpe 1.66 is on **book equity volatility**, not on **risk capital deployed**.

**Actions:**

- Add to reports: **utilization**, **return on mean deployed**, **worst-sleeve DD** alongside book DD.
- Optional: report Sharpe on **deployed-capital** return series (future script).

---

## 6. Promotion gate & holdout (reviewer: overfitting risk)

### Reviewer point

Gate tuned on same history; use **frozen params from 2022-01-01** and evaluate forward only.

### Our response — **Agree on methodology**

**2022+ trade-based holdout (params unchanged, no re-tune):**

| Metric | Value |
|--------|-------|
| Trades | **146** |
| Profit factor | **3.73** |
| Per-sleeve PF CIs (2022+) | All wide; BTC [0.62, 7.38], ETH [1.14, 13.5], … |

**Fixed (May 2026):** Holdout equity path now uses aligned `portfolio_equity_series`. **Holdout DD −1.60%** (not −34%); return **+53.9%** from $133k at 2022-01-01. See **Second-pass addendum** below.

**Actions:**

- Adopt reviewer rule: **no parameter changes** without pre-registered holdout from 2022-01-01 forward.
- State clearly: calendar holdout **≠** regime-pure OOS (2022–present mostly bull).
- Treat promotion gate as **retrospective sanity check**, not OOS proof.

---

## 7. Two-pass max-4 cap (reviewer: backtest vs live)

### Reviewer point

Backtest uses full future path to block entries; live is greedy — small divergence.

### Our response — **Agree; impact is tiny**

Historical replay: **8 blocked entry days** in 7+ years (~250 trades). Live FCFS should match closely.

**Actions:**

- Document in spec: live uses same blocked-date list from prior-day portfolio replay (already how daily runner works).
- Log live blocked vs replay blocked monthly (H12 paper vs replay).

---

## 8. Momentum fade redundancy (reviewer: simplify exit)

### Reviewer point

`close < SMA50` and `sma50_slope20 < 0` are largely redundant; keep **3% giveback + close < SMA50**.

### Our response — **Agree to test**

**Action:** Ablation backtest — drop slope condition; if trade count/PnL unchanged, simplify live exit and update spec.

---

## Reviewer Q&A — consolidated

| # | Question | Our answer after data |
|---|----------|----------------------|
| 1 | Close vs high Donchian? | **Keep close-based**; buffer + cap approximate confirmation. |
| 2 | T+2 entry? | **Keep next-open**; no added lookahead benefit. |
| 4 | Asymmetric crypto stops? | **Keep**; metals smoother, crypto gap risk. |
| 5 | FCFS max-4? | **Keep**; marginal_risk did not help. |
| 6 | Vol sizing on crypto? | **Consider lower max_alloc (50%)** on SOL/DOGE — reviewer suggestion aligns with high sleeve DD. |
| 7 | Unify data? | **Move BTC to Binance**; keep Dukascopy for metals/oil. |
| 8 | OOS validity? | **Book pooled yes**; **sleeve-level mixed**; ETH/BNB hopeful, BTC weak ex-2024. |
| 9 | Max 5 concurrent? | **Paper trial OK** — frontier showed +5% return, same book DD on replay. |
| 10 | Main invalidator? | **Slippage on next-open** (esp. DOGE/SOL) if spot; **funding** if perps in hot regimes. |

---

## Priority action list

| Priority | Action | Owner |
|----------|--------|-------|
| P0 | Add utilization + PF CI to standard validation output | Engineering |
| ~~P0~~ | ~~Fix 2022+ holdout equity metrics~~ | **Done** |
| P1 | BTC → Binance BTCUSDT data; re-baseline | Engineering |
| P1 | Backtest `max_breakout_bps` sweep (crypto vs metals) | Research |
| P1 | Momentum-fade ablation (drop SMA50 slope) | Research |
| P2 | Paper trial max 5 concurrent | Ops |
| P2 | Slippage stress (+5–10 bps) on next-open entry | Research |
| P2 | If live = perps: funding series in sim | Engineering |

---

## Bottom line for the reviewer

We take the review seriously. The **pooled book edge** looks real (PF CI above 1), but **sleeve-level claims are overstated** in marketing language — we will correct that. The **225 bps cap** deserves a formal sweep; early forward-return data suggests we may be **excluding crypto winners**, not filtering losers. **Book DD must be reported with utilization and worst-sleeve DD.** Venue is **spot paper today**; perp funding at bull-market rates would matter at scale. **BTC on Dukascopy** is a documented drift risk — we will migrate to Binance for signal parity.

Thank you for the review — it sharpens what we can and cannot claim from this backtest.

---

## Second-pass addendum (reviewer follow-up, May 2026)

### Fixed: 2022+ holdout equity metrics

The prior **−34% holdout DD** was a **bug** (sleeve equity series summed without aligned `ffill` before aggregation). Recomputed with `portfolio_equity_series()`:

| Metric (2022-01-01 → end) | Value |
|---------------------------|-------|
| Equity at holdout start | **$133,353** |
| Equity at end | **$205,209** |
| Return (equity path) | **+53.9%** |
| **Max DD (holdout window)** | **−1.60%** |
| Max DD (full sample book) | −1.61% |
| Trades entered | 146 |
| PF (trades in window) | 3.73 |
| Sharpe (holdout equity) | 1.76 |

**No hidden −10% episode** in the holdout equity path at book level. Sleeve-level pain still exists (worst sleeve −13% full sample).

**Caveats we now state explicitly:**

- Calendar holdout **≠** regime-pure OOS: 2022–present is mostly bull for crypto/gold; we lack a long flat/down window in history.
- Holdout return is measured from **$133k** book equity at 2022-01-01, not from $100k inception.
- PF 3.73 on 146 trades is **suggestive**, not proof — same regime overlap concern.

### Exhaustion cap preview — stronger caveats + DOGE

Forward-return preview is **not** trade PnL (no stops, holds, fees, next-open). **Conclusion on cap change requires full simulation sweep.**

| Symbol | Mean 5d fwd | **Median 5d fwd** | Mean 5d (trim top 5%) |
|--------|-------------|-------------------|------------------------|
| DOGEUSDT | +15.4% | **−1.2%** | **+4.2%** |
| SOLUSDT | +5.5% | +3.3% | +3.2% |
| ETHUSDT | +3.1% | (see JSON) | — |

**DOGE:** Mean is **tail-driven** (811 bps median breakout); cap may correctly filter lottery spikes, not “missed momentum.” **Do not loosen DOGE cap** on preview data alone.

### Utilization / “serial trader” (corrected metric)

Prior **1.07 “open sleeves”** was wrong (counted entries per day, not concurrent positions). Corrected:

| Metric | Value |
|--------|-------|
| Mean utilization vs book | **6.7%** |
| Mean **concurrent** open sleeves (when in market) | **1.72** |
| Days with ≥1 position | 53.3% |
| Days at max-4 cap | **3.1%** |
| Blocked entries (full sample) | 8 |

**Acknowledged:** Functionally a **low-concurrency serial book**; diversification and max-4 cap **rarely bind**. Returns are driven by which sleeves worked, not active portfolio construction.

### Funding — modal bull scenario

| Rate per 8h | Drag vs net PnL |
|-------------|-----------------|
| 0.01% (low) | ~2% |
| **0.05% (modal bull, if perps)** | **~10%** |

Long-only wins when funding is elevated → **0.05% is the relevant stress**, not the tail case.

### Still to do before next reviewer send

1. **`max_breakout_bps` full backtest sweep** (crypto vs metals; DOGE separate).  
2. **BTC → Binance BTCUSDT** migration + re-baseline.  
3. Utilization now in **`strategy_validation.py` header** on every run.

---

*Re-run analyses:* `python3 btc_breakout_clean/review_feedback_analysis.py`
