# Architectural Review — Response (May 2026)

**Subject:** Response to *Simona 2.0 — Expert Review & Architectural Optimization Feedback*  
**Primary doc:** [`SIMONA_COMPLETE_DOCUMENTATION.md`](SIMONA_COMPLETE_DOCUMENTATION.md)  
**Prior review:** [`EXPERT_REVIEW_RESPONSE.md`](EXPERT_REVIEW_RESPONSE.md)

This document maps **Part I (assessment)** and **Part II (playbook)** to empirical work already run, flags risks in the proposed refactor code, and assigns priority without changing live configuration.

---

## Executive summary

| Theme | Our position |
|-------|--------------|
| Reviewer's diagnosis | **Mostly agree** — venue mismatch, gap/slippage, idle capital, and regime lag are real |
| Reviewer's playbook | **Research queue, not live promotion** — none of the four proposals has passed the existing promotion gate |
| Sample `SimonaEngineV2` | **Do not merge** — simplified logic diverges from live (fade OR vs AND, cap sort, sizing, metals path) |
| Highest-value next step | **Synthetic institutional bars** for BTC (+ execution parity test) — addresses phantom-market critique |
| Lowest-risk ops win | **Idle-cash yield sweep** — orthogonal to signal edge; model as APR overlay, not strategy change |

**Live stance unchanged:** 8 sleeves, daily bars, Dukascopy BTC signals, Binance crypto execution, max 4 FCFS concurrent.

---

## Part I — Assessment vs our data

### Strengths cited — confirmed

| Reviewer point | Empirical backing |
|----------------|-------------------|
| Utilization transparency (6.7% mean deployed) | `review_feedback_analysis.py`; corrected `portfolio_equity_series()` |
| Momentum-fade ablation | `momentum_fade_ablation_validation.py` — slope load-bearing; close&lt;SMA50 redundant |
| DOGE cap / tail artifact | Exhaustion cohort: DOGE mean +15.4% vs **median −1.2%** at 5d; keep 225 bps |
| Validation gate discipline | 17 improvement variants, next batch, bar frequency, hold sweep — **all reject** except policy tradeoffs |

### Fragilities cited — mapped

#### 1. Signal / execution venue mismatch — **Agree; #1 structural live risk**

- **Fact:** ~**21%** BTC signal-day mismatch Dukascopy vs Binance daily (`review_feedback_analysis.py`).
- **What we did:** Full book replay with Binance BTCUSDT bars → **103.9% return, −2.58% DD** vs baseline **105.2%, −1.61%** (`next_batch_experiments_validation.py`).
- **Reviewer is right:** Rejecting migration on backtest DD **does not fix** live phantom-market risk — it **documents** a tradeoff.
- **Our position:** Accept mismatch as **known live limitation** until **synthetic bars** or **Binance-native BTC** pass gate *with* TWAP/slippage model.

#### 2. Gap risk on T+1 open — **Agree; partially quantified**

- **Fact:** Signal at T−1 close, fill at T open (no lookahead).
- **What we did:** `slippage_stress_validation.py` — +10 bps/side → **98.2% return** (−7pp); +5 bps → 101.7%.
- **Not yet done:** Open-gap filter at scale (gap_skip 2.5% **failed** post Binance BTC migration); **TWAP** execution model; 15m σ slippage.
- **Our position:** Slippage stress supports reviewer concern; **TWAP proposal is valid P1 research**, not live.

#### 3. Opportunity cost / regime lag — **Agree**

- Documented: limitation §22 item 11 (2022 sit-out, recovery lag).
- `sma200_95` / `bull_only` **protected** 2022 book DD; cost is **months flat** in V-recovery — not captured as backtest loss.
- World Cup / event screen: `regime_off` adds trades but **−18% to −24%** solo tails — regime is doing its job.

---

## Part II — Playbook item by item

### 1. Synthetic institutional bars (21:00 UTC close, weekend dampening)

| | |
|--|--|
| **Problem** | Valid — aligns crypto session to macro 5pm EST; may cut BTC signal drift |
| **Proposal risk** | Weekend return ×0.5 is **ad-hoc** — reshapes SMA200 path; must be ablated (with/without dampening) |
| **Empirical status** | **Done** — `synthetic_pipeline.py` + `architectural_experiments_validation.py`: 21:00 UTC **72.5%** / −2.74% DD; dampen ×0.5 **94.9%** / −4.84% — **both fail gate** |
| **Verdict** | **P1 research complete** — do not promote; venue alignment alone insufficient |
| **Promotion rule** | Same gate as all variants; must beat baseline on return, PF, DD with slippage overlay |

**Note:** Dukascopy H1 → daily already exists for metals; crypto path would use Binance H1 → 21:00 UTC aggregate. Do **not** mix dampened crypto with undamped metals without book-level test.

---

### 2. Automated yield on idle cash (~93% idle)

| | |
|--|--|
| **Problem** | Valid — 6.7% utilization ⇒ ~\$93k idle on \$100k book |
| **Rough math** | 4.5% APR on 93% idle ≈ **+4.2%** book CAGR overlay (if instantly redeemable T+0) |
| **Empirical status** | **Done** — `yield_overlay.py`: +48.4pp on baseline at 4.5% APR (reporting only) |
| **Verdict** | **P2 ops / treasury** — implement outside strategy engine; paper-track idle yield separately |
| **Caution** | Does not fix venue mismatch; can **mask** underperformance in headline CAGR if reported combined |

---

### 3. Single-pass event-driven replay (priority queue by `breakout_bps`)

| | |
|--|--|
| **Engineering benefit** | Real — removes two-pass blocked-date precompute; enables streaming |
| **Behavior change** | **Yes** — reviewer fills highest `breakout_bps` first when cap binds; live/OOS replay is **FCFS by entry date** |
| **Empirical status** | FCFS single-pass **regression PASS** (105.2% identical). Breakout-priority cap **108.7%** / −1.61% DD — **passes gate** in replay (`single_pass_cap.py`) |
| **Verdict** | **P2 refactor** safe for FCFS parity; breakout-priority is optional **paper** cap policy, not live FCFS |

**Recommendation:** Refactor to single-pass ** preserving FCFS timestamp order** first; only then A/B test breakout-priority as a *strategy variant* with gate.

---

### 4. TWAP + dynamic slippage on entry

| | |
|--|--|
| **Formula proposed** | `slippage = max(5 bps, 0.1 × σ_15m)` on 15-min window around daily open |
| **Empirical status** | **Done** — `entry_slippage_*` hooks in `SimConfig` (defaults off); crypto TWAP proxy **87.0%** / −2.34% DD — **fail gate** |
| **Verdict** | **P1 research complete** — confirms fill sensitivity; live unchanged |
| **Live tie-in** | More important than yield sweep for **memecoin / satellite** sleeves |

---

## Part III — Sample code review (`SimonaEngineV2`)

**Do not promote the provided implementation.** Gaps vs live `btc_breakout_paper_sim.py`:

| Issue | Sample code | Live engine |
|-------|-------------|-------------|
| **Momentum fade** | Exit requires giveback **AND** close&lt;SMA50 **AND** slope&lt;0 | **OR** across three triggers (`momentum_faded()`) |
| **Concurrent cap** | Priority by `breakout_bps` desc | FCFS by entry timestamp; pass-1 block list |
| **Universe** | 2 crypto sleeves in example | 8 sleeves, Dukascopy metals/oil, per-sleeve params |
| **Exits** | No partial exit, channel, trail, stop_use_low nuances | Full exit stack |
| **Entries** | No Saturday skip, gap skip, exhaustion cap per asset, tiered sizing | All in `StrategyConfig` |
| **Sizing** | `portfolio_capital / n_sleeves` | Fixed sleeve equity \$12.5k, vol_target/vol20, max_alloc |
| **Indicators** | Computed inside synthetic pipeline | `add_indicators()` with weekly trend, backup entry, etc. |
| **Yield sweep** | Embeds 4.5% APR in equity path | Not in baseline sim (would confound gate) |

The fade logic bug alone would **invalidate** any metrics from the sample engine vs live baseline.

---

## Part IV — Prioritized action list (architectural)

| Pri | Action | Type | Gate? |
|-----|--------|------|-------|
| **P1** | Synthetic 21:00 UTC bars (BTC + crypto); with/without weekend dampening | Research | Yes |
| **P1** | TWAP / σ-based entry slippage model on crypto | Research | Yes |
| **P1** | Document live **venue mismatch** in daily ops checklist (signal source vs fill venue) | Ops | — |
| **P2** | Single-pass replay, **FCFS-preserving** | Engineering | Regression = baseline metrics |
| **P2** | Idle-cash yield as **reporting overlay** (not in strategy gate) | Treasury | — |
| **P3** | Breakout-priority cap sort (new variant) | Research | Yes |
| **Defer** | Merge `SimonaEngineV2` as replacement | — | Until parity test vs `simulate_account()` |

---

## Part V — What we will not do (without new evidence)

- Replace live engine with reviewer sample code
- Adopt breakout-priority cap as live default (FCFS validated; marginal_risk failed)
- Add weekend return dampening to live without ablation
- Count yield sweep toward strategy promotion gate
- Relax regime filters for World Cup / memecoins on main book (see §19.9 complete doc)

---

## Cross-reference — validation scripts

```bash
python3 btc_breakout_clean/review_feedback_analysis.py      # BTC drift, utilization, funding preview
python3 btc_breakout_clean/next_batch_experiments_validation.py  # Binance BTC migration
python3 btc_breakout_clean/slippage_stress_validation.py
python3 btc_breakout_clean/bar_frequency_edge_validation.py
python3 btc_breakout_clean/momentum_fade_ablation_validation.py
python3 btc_breakout_clean/oob_experiments_validation.py    # marginal_risk cap
python3 btc_breakout_clean/world_cup_candidate_validation.py
```

---

*Response drafted May 2026. Live configuration unchanged.*
