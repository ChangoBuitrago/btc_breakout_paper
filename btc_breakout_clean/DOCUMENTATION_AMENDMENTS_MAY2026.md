# Simona 2.0 — Documentation Amendments (May 2026, final pass)

**Purpose:** Changes applied after the second external review of `SIMONA_COMPLETE_DOCUMENTATION.md`.  
**Full document:** [`SIMONA_COMPLETE_DOCUMENTATION.md`](SIMONA_COMPLETE_DOCUMENTATION.md)

---

## 1. Max utilization footnote (§14.2)

An earlier draft reported **75.7%** peak deployed / book equity. The corrected figure is **44.0%**.

| | Earlier draft | Corrected |
|--|---------------|-----------|
| Max utilization | 75.7% | **44.0%** |
| Mean utilization | 7.7% | **6.7%** |

**Cause:** Earlier calculation summed sleeve equity with `fill_value=0` on misaligned dates, **understating book equity** when sleeves had missing index rows → inflated `deployed / book`.

**Fix:** Use `portfolio_equity_series()` — forward-fill each sleeve to $12.5k initial equity, then sum — same method as book return and DD.

**Scope:** Utilization metrics only. **Returns, book DD, PF, trade count unchanged.**

---

## 2. Holdout Sharpe note (§14.5)

Holdout Sharpe **1.76** is higher than full-sample Sharpe **1.66**.

**Explanation:** Holdout starts **2022-01-01** at **$133,353** book equity — after 2018–2021 compounding and after the worst book-level drawdown for that equity path. The window is mostly bullish 2022–2025, not $100k inception through the 2022 crypto winter.

**This is window selection, not evidence the strategy improved post-2022.**

Holdout metrics (unchanged from prior fix):

| Metric | Value |
|--------|-------|
| Return (equity path) | +53.9% |
| Max DD (holdout window) | **−1.60%** |
| Trades entered | 146 |
| PF | 3.73 |

*(Prior −34% holdout DD was a bug; fixed in same pass.)*

---

## 3. Calendar-year net PnL (new §14.6)

Cumulative metrics hide losing calendar years. Added **realized net PnL by entry year** (USD, live replay, max 4 concurrent).

### Book by year

| Year | Trades | Book net PnL |
|------|--------|--------------|
| 2018 | 4 | **−$404** |
| 2019 | 20 | +$4,805 |
| 2020 | 42 | +$15,233 |
| 2021 | 38 | +$13,718 |
| 2022 | 15 | **+$1,551** |
| 2023 | 42 | +$18,499 |
| 2024 | 45 | +$29,913 |
| 2025 | 36 | +$15,276 |
| 2026 YTD | 8 | +$6,618 |

### Selected sleeves

| Year | BTCUSD | ETHUSDT | SOLUSDT | XAGUSD | DOGEUSDT |
|------|--------|---------|---------|--------|----------|
| 2022 | $0 | **−$733** | $0 | **−$1,148** | $0 |
| 2023 | +$3,382 | +$3,348 | +$6,534 | +$937 | +$1,401 |
| 2024 | **−$320** | +$10,290 | +$9,732 | +$722 | +$6,745 |
| 2025 | +$3,841 | +$30 | +$2,735 | +$4,712 | $0 |

**Notes:**

- PnL attributed to **entry year** (full trade PnL in entry year even if exit is next calendar year — minor at 5–15d holds; **2025 / 2026 YTD** may be slightly affected).
- **2022** barely positive at book level; **15 trades** — regime filter sat out the crypto bear (by design). **Forward risk:** next long sit-out while market recovers before re-entry is opportunity cost, not a backtest flaw.
- **XAG 2022 (−$1,148):** main metals loser; 13d min hold, `bull_only`, no hard stop — most bear-exposed sleeve.
- **BTC 2024** negative at sleeve level inside positive cumulative curve.
- Large share of total PnL from **2023–2025**.
- Does not replace a true flat/down regime holdout (insufficient history).

**Reproducibility:** `review_feedback_analysis.py` → `calendar_year_pnl` in JSON output.

---

## 4. Limitation #9 — wording (§22)

**Before:** “Returns driven by which sleeves worked, not portfolio construction.”

**After:** “Max-4 cap rarely binds (3.1% of days), but **book-level diversification benefit is real though modest** — it limits simultaneous correlated crypto exposure when it binds; **most return attribution remains at the sleeve level**.”

---

## 5. Third-pass additions (reviewer close-out)

| Item | Addition |
|------|----------|
| **Entry-year footnote** | Year-end straddles minor at 5–15d holds; 2025/2026 YTD may nudge slightly |
| **2022 sit-out** | Regime filter feature; forward opportunity-cost if flat 12+ months then miss recovery |
| **XAG 2022** | −$1,148; per-sleeve risk note (long hold, no stop, bull_only) |
| **Limitations §22** | Items 11–12 added in full doc |

---

## 6. No other metric changes

The following were confirmed unchanged in this pass:

- Book return 105.2%, DD −1.61%, PF 3.58, Sharpe 1.66 (full sample)
- Utilization: mean concurrent **1.72** sleeves, max-4 binds **3.1%** of days
- DOGE exhaustion preview: mean 5d +15.4%, **median −1.2%**
- Funding stress: ~2% of PnL at 0.01%/8h; **~10% at 0.05%/8h** (modal bull if perps)

---

## 7. Fourth-pass additions (next batch + fade ablation)

| Item | Finding |
|------|---------|
| **BTC Binance migration** | Rejected — return 103.9%, DD −2.58% vs baseline −1.61% |
| **Tiered sizing 1.5×** | Rejected — +29pp return but worst sleeve −17.2%, DD −2.14% |
| **9-sleeve LTC / AVAX** | Rejected — LTC solo −32%; book dilution |
| **SOL/DOGE max_alloc 50%** | Return-only fail — worst −12%, return −2pp |
| **Partial exit + max 5** | DD fail — Sharpe 1.84, return 110%, DD −1.96% |
| **Momentum-fade ablation** | Keep live — slope load-bearing; close&lt;SMA50 redundant |
| **Gate buckets** | `return_only` vs `dd_or_mixed` in next-batch JSON |

**Scripts:** `next_batch_experiments_validation.py`, `momentum_fade_ablation_validation.py`.

**Engine hooks (defaults off / unchanged live):** `tiered_sizing_by_breakout`, `momentum_fade_use_*` toggles.

---

## 8. Fifth-pass additions (hold window + bar frequency)

| Item | Finding |
|------|---------|
| **Hold-window sweep** | All fail gate; crypto longer (+2/+5d) closest near-miss (108.3%, DD −1.77%) |
| **Bar frequency full book** | **1d best** (edge score 5.96); 2d/3d/1w degrade |
| **Bar frequency crypto** | 1d Sharpe 1.42; 1h Sharpe 0.19 despite 1198% return |
| **Raw signal vs sim** | 1h +7% fwd 5d raw; daily +3.3% — daily rules convert signal to portfolio edge |
| **Live stance** | Daily bars only; no intraday/slower promotion |

**Scripts:** `hold_window_validation.py`, `timeframe_validation.py`, `bar_frequency_edge_validation.py`.

---

## 9. Slippage stress (sixth-pass)

| Extra bps/side | Return | DD | PF | Sharpe |
|----------------|--------|-----|-----|--------|
| 0 (live) | 105.2% | −1.61% | 3.58 | 1.66 |
| +5 | 101.7% | −1.69% | 3.46 | 1.62 |
| +10 | 98.2% | −1.77% | 3.35 | 1.59 |
| +20 | 91.5% | −1.92% | 3.13 | 1.51 |

**Script:** `slippage_stress_validation.py`. Edge survives +10 bps/side but return −7pp — slippage on live fills remains material risk.

---

## 10. Funding + paper trial (seventh-pass)

| Item | Finding |
|------|---------|
| **Funding flat 0.05%/8h** | −11.4% of net PnL (~12pp return); modal bull perp stress |
| **Funding skip &gt;3 bps** | No-op on history |
| **Paper partial50 + max5** | Sharpe 1.84, return 110%, DD −1.96% — paper only |

**Scripts:** `funding_stress_validation.py`, `paper_trial_validation.py`.

---

## 11. Architectural optimization review (May 2026)

See [`ARCHITECTURAL_REVIEW_RESPONSE_MAY2026.md`](ARCHITECTURAL_REVIEW_RESPONSE_MAY2026.md).

| Proposal | Verdict |
|----------|---------|
| Synthetic 21:00 UTC bars | **Done, reject live** — 72.5% / −2.74% DD (§19.10) |
| Synthetic + weekend dampen ×0.5 | **Done, reject** — 94.9% / −4.84% DD |
| Idle yield sweep (4.5% APR) | **Done, reporting** — +48.4pp overlay on baseline |
| Single-pass FCFS cap | **Regression PASS** — identical to two-pass |
| Breakout-priority cap | **Passes gate** in replay (108.7%) — paper only; live FCFS |
| TWAP slippage (crypto) | **Done, reject** — 87.0% / −2.34% DD |
| Sample SimonaEngineV2 | Reject — fade AND vs live OR |

---

*Amendments applied to `SIMONA_COMPLETE_DOCUMENTATION.md` — May 2026.*
