# Regime-off research (Algo 2)

Standalone package — **does not modify** `btc_breakout_clean` breakout logic or live config.

## Goal

Find edge when **regime is off** (`close` below SMA200 or 0.95×SMA200), via solo backtests on metals/oil only.

## Mechanisms

| ID | Description |
|----|-------------|
| M0 | Breakout only when regime off (sanity / inverse regime) |
| M1 | Stretch MR: enter when stretched below regime floor |
| M2 | Prior-low tag: enter near N-day low while regime off |

## Run

Requires existing Dukascopy H1 cache (from breakout daily run):

```bash
python3 regime_off_mr/validation.py
```

Output: `regime_off_mr/validation_results.json` (gitignored).

## Discovery gates (solo)

- PF ≥ 1.15 full, ≥ 1.0 ex-2024
- ≥ 15 trades full, ≥ 3 ex-2024
- Max DD ≥ −15%
- Top trade &lt; 50% of total PnL

No combined book, no paper bot, no changes to breakout sleeves until a row passes on ≥ 2 symbols.
