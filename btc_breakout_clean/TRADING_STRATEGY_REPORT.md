# Trading Strategy Report — Long-Only Daily Breakout

This document describes the **trading rules only**: what the system trades, why each piece exists, and how a single position moves from flat → signal → entry → exit. Infrastructure (Telegram, GitHub, file layout) is out of scope.

**Source of truth in code:** `btc_breakout_paper_sim.py` (engine), `btc_breakout_binance_paper_bot.py` (live per-sleeve parameters).

---

## 1. Strategy thesis

**Name (informal):** volatility-scaled **close breakout** with **trend regime** and **fixed holding period**.

**Core idea:** After price has consolidated below a rolling “ceiling” (prior N-day max **close**), a **confirmed** breakout above that ceiling—by more than a buffer, but not by “too much”—often continues in the direction of the break for a few days, especially when the broader trend filter is on.

**Design choices that define the edge:**

| Choice | Rationale |
|--------|-----------|
| **Long only** | Captures upside continuation; no short logic |
| **Daily bars** | Low turnover, avoids intraday noise |
| **Close-based breakout** | Signal on **settled** daily close, not intraday wicks |
| **Buffer above prior high** | Filters marginal pokes through resistance |
| **Max breakout cap** | Skips “exhausted” gaps that often mean-revert |
| **SMA200-style regime** | Only trade breaks aligned with medium-term trend (with gold exception) |
| **Fixed hold, no stop (live)** | Lets winners run a defined window; avoids whipsaw stops on volatile crypto/metals |
| **Vol-scaled size** | Smaller when recent daily vol is high |

The live book runs **six independent sleeves** (BTC, ETH, BNB, gold, silver, copper), each with its own lookback/buffer/hold/regime tuned for that instrument’s behavior—not one global parameter set.

---

## 2. Bar construction (what “a day” means)

All logic runs on **one row per session**:

- **Binance (ETH, BNB):** native daily klines; only **fully closed** candles are used.
- **Dukascopy (BTC, metals):** hourly bars resampled to daily: open=first, high=max, low=min, **close=last** of the UTC day.

Indicators and signals are computed on this daily series. Warm-up needs roughly **200+ bars** before SMA200 and rolling windows are valid.

---

## 3. Indicators (computed every bar)

From `add_indicators()` in `btc_breakout_paper_sim.py`:

| Field | Definition |
|-------|------------|
| `ret` | Daily close-to-close return |
| `vol20` | 20-day std of `ret` (sizing input) |
| `atr14` | 14-day mean of \|daily return\| (trail only; **live trail = 0**) |
| `sma50`, `sma100`, `sma200` | Simple moving averages of **close** |
| `sma50_slope20` | `sma50 - sma50[20]` |
| `sma200_slope20` | `sma200 - sma200[20]` |
| `prior_high` | `max(close)` over the prior `lookback` bars, **excluding today** (`shift(1)`) |
| `breakout_bps` | `10,000 × (close / prior_high − 1)` |
| `signal` | Boolean composite (see below) |

**Important:** Breakout level uses **closes only**, not highs. A day that spikes above the level intraday but **closes** below does **not** signal.

---

## 4. Signal definition (all conditions must pass)

On bar **t** (after the close is known):

### Step A — Raw breakout

```
close[t] > prior_high[t] × (1 + buffer_bps / 10,000)
```

`buffer_bps` is the minimum clearance above the rolling ceiling (e.g. 125 bps = 1.25%).

### Step B — Exhaustion cap

```
breakout_bps[t] ≤ max_breakout_bps
```

Default cap in live config: **225 bps (2.25%)** above `prior_high`. Breaks larger than that are treated as **too stretched** and skipped—reducing chase entries after vertical moves.

### Step C — Regime filter (`trend_mode`)

Live modes in use:

| Sleeve | `trend_mode` | Rule |
|--------|--------------|------|
| BTC, ETH, BNB, XAG, XCU | `bull_only` | `close > SMA200` |
| XAU | `sma200_95` | `close > 0.95 × SMA200` (slightly looser for gold) |

Other modes exist in code (`sma100`, `sma50_slope_up`, `all`, etc.) for research; they are **not** in the live book.

### Final signal

```
signal[t] = raw_breakout AND not_exhausted AND regime_on
```

If any leg fails, **no trade** that cycle.

---

## 5. Execution model (signal bar vs entry bar)

This is the most important timing detail.

```
Day t-1 (signal bar)          Day t (entry bar)
─────────────────────         ─────────────────
Close prints                  Enter at OPEN if
Signal evaluated              signal was true yesterday
                              Hold N sessions;
                              exit at CLOSE on day N
```

From `simulate_account()`:

- On day **t**, the engine reads **`signal[t−1]`** (yesterday’s close).
- If flat and yesterday signaled → **buy at today’s open** (`open[t]`).
- **No same-bar entry** on the signal close; you always get at least one session of gap risk overnight.

Recorded trade fields include:

- `next_open_gap_pct` — open vs signal close
- `open_to_exit_pct` — open entry to exit close

**While in a position:** new signals are **ignored** (`if not in_pos and todays_signal`). No pyramiding, no scale-in, no “refresh” of the trade.

---

## 6. Position sizing

On the **signal bar** (day before entry):

```
size_frac = min(max_alloc, vol_target / vol20)
```

Live defaults (all sleeves): **`vol_target = 1.5%`**, **`max_alloc = 75%`**, **`compound = true`**.

| Concept | Meaning |
|---------|---------|
| `vol20` high (volatile) | Smaller fraction of equity |
| `vol20` low | Size approaches cap (75%) |
| `compound` | Notional based on **current** sleeve equity, not fixed $10k forever |

Entry notional = `equity × size_frac`; fees charged on that notional at entry and again at exit.

If `vol20` is missing or zero → **size_frac = 0** → signal effectively blocked (no entry).

---

## 7. Exit rules

### Live configuration

- **Dynamic hold** (most sleeves): exit at **`hold_max`**, or after **`hold_min`** if momentum faded (3% giveback from peak close, close &lt; SMA50, or negative SMA50 slope).
- **`trail_atr = 0`:** no trailing stop in production.
- **Crypto hard stop** (per symbol, from `crypto_stop_validation.py` solo grid):

  | Symbol | Stop |
  |--------|------|
  | BTCUSD | 5% |
  | ETHUSDT | 6% |
  | BNBUSDT | 5% |
  | SOLUSDT | 12% |
  | DOGEUSDT | 12% |

  Trigger: daily **low** touches stop; fill at stop price. Can fire from day 1. Metals/oil have **no** hard stop.

### Portfolio entry cap (live)

- **`LIVE_MAX_CONCURRENT_ENTRIES = 4`:** at most four sleeves may have an open position at once.
- Daily runner does a **two-pass** replay: uncapped pass → compute blocked signal dates → capped pass (same as `run_full_book_live()` in validation).
- Historical replay blocks **8** entry days on the current 8-sleeve book (2018+).
- Default exit price: **close** of the exit bar (except stop fills at the stop limit).

### Optional (research only)

If `trail_atr > 0`, exit early when:

```
close < peak_close_since_entry × (1 − trail_atr × atr14)
```

Not used in live params. Grid: `crypto_stop_validation.py`.

### End of data

Open trades at the last bar are **force-closed** at that bar’s close.

### What is **not** an exit

- No take-profit
- No exit on opposite signal
- No exit on regime turning off while in trade (crypto stop is separate)

---

## 8. Costs

Per side: `fee_bps` on notional (entry and exit).

| Sleeve type | `fee_bps` |
|-------------|-----------|
| Crypto / copper | 10 |
| Gold / silver | 2 |

Rough drag: ~20 bps round-trip on crypto sleeves vs ~4 bps on XAU/XAG (plus gap and slippage not modeled).

---

## 9. Live parameters per instrument

Each sleeve is the **same strategy class** with different knobs (`LIVE_STRATEGY_PARAMS` in `btc_breakout_binance_paper_bot.py`):

| Sleeve | Lookback | Buffer | Cap | Regime | Hold | Interpretation |
|--------|----------|--------|-----|--------|------|----------------|
| **BTCUSD** | 15d | 125 | 225 | bull_only | **5d** | Shorter memory, moderate buffer; **shortest** crypto hold |
| **ETHUSDT** | 10d | 150 | 225 | bull_only | **10d** | Faster ceiling (10d), wider buffer; longer hold |
| **BNBUSDT** | 15d | 125 | 225 | bull_only | **6d** | Similar to BTC structure, slightly longer hold |
| **XAUUSD** | 30d | 100 | 225 | sma200_95 | **9d** | Slower ceiling; looser gold regime |
| **XAGUSD** | 30d | 100 | 225 | bull_only | **13d** | Longest hold in book; silver trends need more time |
| **XCUUSD** | 15d | 100 | 225 | bull_only | **4d** | Shortest hold overall; copper mean-reverts faster |

**Book size:** 6 sleeves × **$10,000** = **$60,000** notional starting equity (compounded per sleeve).

**Hold tuning philosophy:** shorten holds where volatility hurts (especially crypto), but only to the **minimum** days that still beat or match the old 4-sleeve baseline on return, profit factor, and drawdown. Aggressive longer holds + looser caps showed higher backtest return but worse DD and were **rejected**.

---

## 10. Full trade lifecycle (example)

**BTC, lookback=15, buffer=125, hold=5, bull_only**

1. Days 1–15: price chops below rolling 15-day max close.
2. **Day 16 close:** `close` clears `prior_high × 1.0125`, breakout ≤ 225 bps, `close > SMA200` → **`signal = true`**.
3. **Day 17 open:** enter long at open; size e.g. 45% of sleeve if `vol20` implies that.
4. **Days 17–21:** hold regardless of new signals or pullbacks.
5. **Day 21 close:** `hold_bars = 5` → exit at close; pay exit fee; sleeve flat.
6. If **Day 18** also had `signal = true`, it is **ignored** until flat again.

**Flat-state status messages** (conceptually): `no breakout +Xbps`, `regime off`, `too stretched`, `warming up` — these describe **which gate** blocked entry.

---

## 11. Portfolio-level behavior (strategy interaction)

Sleeves do **not** share risk logic:

- Each runs its own signal → entry → hold → exit loop on **$10,000** starting equity (compounded per sleeve).
- Correlation is implicit (e.g. crypto risk-on days may fire multiple sleeves); there is **no** book-level max exposure or veto.
- Combined backtest stats sum sleeve equity curves (`portfolio_param_sweep.py`).

**2018+ reference (current 7-sleeve live book, May 2026):** ~**99.8%** return, **PF ~3.41**, **max DD ~−2.26%** on ~$65k (`strategy_validation.py`). Sleeves: `BTCUSD`, `ETHUSDT`, `BNBUSDT`, `DOGEUSDT`, `XAUUSD`, `XAGUSD`, `BRENT`. See `EDGE_POLISH.md` for polish experiments.

That profile fits a **low-frequency, positive-skew, time-stop** system: many small losses/fees, fewer larger winners, modest time in market (`exposure_pct` typically well below 100% per sleeve).

Re-run anytime: `python3 btc_breakout_clean/strategy_validation.py` → `strategy_validation_results.json`.

---

## 12. Strategy boundaries and caveats

**Modeled explicitly**

- Close breakout + buffer + cap + regime
- Next-open entry
- Vol-scaled long-only size
- Fixed holding period
- Per-side fees

**Not modeled (paper vs real)**

- Slippage, spread, funding (perps), borrow
- Partial fills, halts, exchange downtime
- Exact Dukascopy vs Binance vs TradingView bar alignment

**Python vs Pine (calendar)**

- **Aligned (May 2026):** Python sets `skip_saturday_entry=True` for all Dukascopy sleeves (`default_skip_saturday_entry()`), matching Pine `skip_sat_entry`. Pending signals defer to the next non-Saturday open, same as the chart script.
- Binance sleeves (ETH, BNB) never skip Saturday — Binance daily bars only.

**Warm-up**

- Early history produces `warming up` / NaN regime until SMA200 and `prior_high` are defined.

**Signal uses yesterday only for entry**

- Today’s live `signal` on the last closed bar means **“eligible to enter next session”**, not “in position now.”

---

## 13. What was tried and rejected (strategy evolution)

| Change | Outcome |
|--------|---------|
| Shorter metal holds (e.g. 10d blanket) | **Hurt** stats → reverted |
| XAG `trend_mode` → `bull_only` | **Kept** — passed baseline |
| Add ETH + BNB with per-crypto optimization | **Kept** — diversified book with acceptable aggregate stats |
| Longer crypto holds + looser caps (“fine-tune” grid) | **Rejected** — ~181% ret / PF 3.19 but DD −3.74% vs tolerance |
| BTC hold 10d | Temporary; minimum holds adopted → **BTC 5d** |

**Promotion rule for parameter changes:** portfolio **return**, **profit factor**, and **max drawdown** vs baseline within tight tolerances—not raw trade count.

---

## 15. Validation & OOS (reproducible)

Script: `btc_breakout_clean/strategy_validation.py`  
Output: `btc_breakout_clean/strategy_validation_results.json` (gitignored)

**Portfolio baseline (2018+, live params, Saturday skip on Dukascopy):**

| Metric | Value |
|--------|-------|
| Return | **99.8%** |
| Max DD | **−2.26%** |
| PF | **3.41** |
| Trades (all sleeves) | **229** |

### Per-sleeve — full sample & signal frequency

| Sleeve | Trades/yr | Full return | Full PF | ex-2024 trades | ex-2024 PF | ex-2024 PnL |
|--------|-----------|-------------|---------|----------------|------------|-------------|
| BTCUSD | 6.9 | 93.6% | 2.30 | 17 | 1.14 | +$502 |
| ETHUSDT | 6.0 | 196.2% | 4.37 | 10 | 6.44 | +$10,249 |
| BNBUSDT | 5.1 | 89.1% | 3.93 | 15 | 4.57 | +$4,446 |
| XAUUSD | 5.9 | 32.1% | 2.13 | 20 | 5.81 | +$3,465 |
| XAGUSD | 5.1 | 94.6% | 2.72 | 12 | 5.76 | +$6,267 |
| XCUUSD | 8.1 | 13.8% | 1.29 | 26 | 0.90 | −$215 |

**OOS read-through:**

- **ETH / BNB** show strong ex-2024 PF and PnL — they are not merely correlated BTC noise in this backtest.
- **BTC** is weaker ex-2024 (PF 1.14); most full-sample edge is **pre-2024** (PF 3.42). Calendar 2024 was negative (−$1,195); 2025 recovered (PF 9.26, +$1,697).
- **XCUUSD** is the weakest sleeve ex-2024 (PF 0.90); highest trade frequency (~8/yr) but lowest full-sample PF.
- **XAG 13d hold** — not separately swept in `portfolio_param_sweep.py`; live value chosen via hold-tuning vs baseline (see §13).

### Gold regime: `sma200_95` vs `bull_only` (XAU only)

| Regime | Trades | Return | PF | Max DD |
|--------|--------|--------|-----|--------|
| **sma200_95** (live) | 43 | 32.1% | 2.13 | −11.4% |
| bull_only | 40 | 30.6% | 2.12 | −11.4% |

Live `sma200_95` is marginally better on return/PF with similar DD — reasonable to keep.

### Dukascopy Saturday-skip impact

| Sleeve | Trades (skip on/off) | Net PnL skip on | Net PnL skip off |
|--------|----------------------|-----------------|------------------|
| BTCUSD | 45 / 46 | $9,360 | $11,907 |
| XAU / XAG / XCU | unchanged | unchanged | unchanged |

Saturday alignment **reduces** BTC backtest PnL vs naive entry but matches Pine and live bot behavior.

### Drawdown recovery (sleeve max-DD trough → prior peak)

| Sleeve | Recovery days |
|--------|----------------|
| ETHUSDT | 9 |
| BNBUSDT | 14 |
| XAUUSD | 49 |
| XAGUSD | 56 |
| XCUUSD | 70 |
| BTCUSD | not recovered to prior peak in sample |

---

## 16. Risk overlays (simulated, not live)

Tested in `strategy_validation.py` against the same baseline. **Neither overlay passes** the standard promotion gate (return / PF / DD within tolerance of baseline).

| Overlay | Return | Max DD | PF | vs baseline |
|---------|--------|--------|-----|-------------|
| **Baseline** (per-symbol crypto stops + **max 4** concurrent) | **105.2%** | **−1.61%** | **3.59** | — |
| Max **3** concurrent sleeves | 97.9% | −1.60% | 3.54 | **Fails** (~26 blocked) |
| Per-sleeve **12% HWM pause** | 88.3% | −2.47% | 3.16 | **Fails** |

**Interpretation:** Book-level caps and HWM pauses trade return for modest PF/DD shifts in this historical replay. They remain **candidates for live risk policy** but are not promoted as backtest improvements. Any adoption should be a deliberate live risk choice, not a parameter tune.

**Live vs backtest:** Paper state under `btc_breakout_clean/paper_portfolio/` is not in git. When available, compare `trades.csv` entry dates and `next_open_gap_pct` to validation replay.

---

## 17. Summary

You are running a **daily, long-only breakout continuation** system: buy when **yesterday’s close** breaks above the prior **N-day max close** by at least **buffer_bps** but not more than **max_breakout_bps**, only when a **trend regime** is on; enter at **today’s open** with size **inversely proportional to 20-day volatility** (capped at 75% of sleeve equity), with **at most four concurrent sleeves**; exit on **dynamic hold**, **crypto hard stops**, or **max hold**. Eight instruments share the same engine but different **lookback / buffer / hold / regime / stop**; the book is optimized for **robust aggregate stats** rather than maximum backtest return.
