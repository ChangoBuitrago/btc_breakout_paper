# Regime-off research (Algo 2) — v2

Standalone package. Does **not** modify `btc_breakout_clean`.

## v2 improvements (regime-off fit)

- **Stretch band** (min–max bps below floor) — avoid weak tags and crash chasing
- **Regime-off maturity** — require N consecutive days below floor
- **Bounce filter** — green day before entry (stabilization)
- **5d return floor** — skip free-fall entries
- **M3** — stretch + bounce + still below SMA50
- **Exits** — regime-on (back above floor), SMA50 touch, or min/max hold
- **Cooldown** after exit — fewer overlapping trades
- **Stricter gates** — cap trade count, sanity-check inflated ex-2024 PF

## Mechanisms

| ID | Role |
|----|------|
| M3 | Primary: stretch bounce below SMA50 |
| M1 | Stretch MR (no SMA50 cap) |
| M2 | Prior-low tag + bounce |

M0 (bear breakout) removed — failed sanity checks.

## Run

```bash
python3 regime_off_mr/validation.py
```

Requires `btc_breakout_clean/cache/*_dukascopy_h1.csv`.

## Gates

PF ≥ 1.15 full, ≥ 1.05 ex-2024 · 12–55 trades · DD ≥ −15% · ex-PF ≤ 8 if &lt; 8 ex trades
