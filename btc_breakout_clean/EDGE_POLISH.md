# Algo 1 edge polish (May 2026)

Re-run validation on the **live 7-sleeve** book:

`BTCUSD`, `ETHUSDT`, `BNBUSDT`, `DOGEUSDT`, `XAUUSD`, `XAGUSD`, `BRENT`

## Baseline (current live params)

| Metric | Value |
|--------|--------|
| Return (2018+) | **99.8%** on ~$65k |
| Profit factor | **3.41** |
| Max DD (book) | **−2.26%** |
| Trades | 229 (~33/yr) |

**ex-2024:** PF **4.19**, 79 trades — book is not “all pre-2024 luck.”

### Per-sleeve (full / ex-2024 PF)

| Sleeve | Trades/yr | Full PF | ex-2024 PF | Note |
|--------|-----------|---------|------------|------|
| ETHUSDT | 5.7 | 4.26 | **5.26** | Strongest OOS crypto |
| BRENT | 4.5 | 3.27 | **4.79** | Strong OOS |
| DOGEUSDT | 3.4 | 10.06 | 80.67 | Only **4** ex trades — ignore PF |
| XAUUSD | 4.8 | 3.09 | 5.06 | Solid |
| BNBUSDT | 5.0 | 3.11 | 3.59 | Solid |
| XAGUSD | 4.7 | 2.44 | 3.18 | OK |
| **BTCUSD** | 6.0 | 2.49 | **1.35** | **Weakest OOS sleeve** |

## What we tested (`edge_polish.py`)

Promotion gate (same as historical research): portfolio return / PF / DD within tolerance of baseline **and** ex-2024 PF not worse by >0.05.

| Variant | Promoted? | Comment |
|---------|-----------|---------|
| **Baseline** | ✓ | Keep current params |
| Drop DOGE | — | Higher return, **worse** ex-2024 PF |
| BTC `sma200_95` | — | Slightly better BTC OOS; book ex-PF −0.04 |
| BTC cap 175 bps | — | Similar |
| Crypto cap 175 | — | Lower return |
| Crypto `sma50_slope_up` | — | Much lower return |
| Max 4 concurrent | — | Already **live** in `run_binance_paper_daily.py` |
| **BTC HWM pause 12%** | — | **Best ex-2024 PF (4.98)** but return −4% → fails gate |

**Conclusion:** The live parameter set is already near a local optimum under this gate. There is an edge; polishing **parameters** further mostly trades return vs OOS on the margin.

## Optional live risk (not promoted as param change)

**BTC sleeve 12% high-water-mark entry pause** — after sleeve equity drops 12% from peak, skip new entries until recovered. Backtest: ex-PF **4.98** vs **4.19**, fewer trades. Portfolio return **95.8%** vs **99.8%**.

Worth considering as a **risk policy** (not a signal change) if BTC OOS weakness shows up in paper. Not enabled in live config by default.

## Commands

```bash
python3 btc_breakout_clean/strategy_validation.py   # per-sleeve windows
python3 btc_breakout_clean/edge_polish.py           # polish variants
python3 btc_breakout_clean/hypothesis_validation.py  # H1–H13 queue
```

## Takeaway

- **Yes, there is an edge** — strong book PF with low reported DD and good ex-2024.
- **Not under-trading** — ~33 round-trips/year across 7 sleeves.
- **Polish** = monitor **BTC ex-2024**, keep **max-4 concurrent** (already on), avoid over-tuning DOGE/metals.
- Next lever is **paper-forward tracking** and optional **BTC HWM pause**, not another parameter grid.
