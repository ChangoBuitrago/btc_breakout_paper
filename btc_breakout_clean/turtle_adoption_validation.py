#!/usr/bin/env python3
"""
Experiments 4–6: Turtle sizing, System-2 backup entry, pyramiding (crypto sleeves).

Modes (crypto only; metals/oil unchanged):
  - baseline: live vol sizing, no backup, no pyramid
  - atr_risk_*: 1% (etc.) risk per 2N unit sizing
  - backup_55: OR signal on 55-day high (same buffer/regime)
  - pyramid_4x*: up to 4 units, add every 0.5N / 1.0N
  - turtle_full: all three combined

Run from repo root:
  .venv/bin/python btc_breakout_clean/turtle_adoption_validation.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import (  # noqa: E402
    LIVE_CRYPTO_SYMBOLS,
    LIVE_STRATEGY_PARAMS,
    LIVE_SYMBOLS,
    live_strategy_config,
)
from portfolio_param_sweep import beats_or_ties_baseline  # noqa: E402
from strategy_validation import portfolio_metrics, preload_raw, run_full_book_live  # noqa: E402

OUT_PATH = HERE / "turtle_adoption_validation_results.json"

MODES: dict[str, dict[str, Any]] = {
    "baseline": {
        "crypto": {},
        "note": "Live vol_target sizing",
    },
    "atr_risk_005": {
        "crypto": {"sizing_mode": "atr_risk", "atr_risk_pct": 0.005},
        "note": "0.5% equity risk per 2N unit",
    },
    "atr_risk_01": {
        "crypto": {"sizing_mode": "atr_risk", "atr_risk_pct": 0.01},
        "note": "1% equity risk per 2N unit (classic Turtle)",
    },
    "atr_risk_015": {
        "crypto": {"sizing_mode": "atr_risk", "atr_risk_pct": 0.015},
        "note": "1.5% equity risk per 2N unit",
    },
    "backup_55": {
        "crypto": {"backup_entry_lookback": 55},
        "note": "System-2: also enter on 55d high breakout",
    },
    "pyramid_4x05": {
        "crypto": {"max_pyramid_units": 4, "pyramid_n_step": 0.5},
        "note": "Up to 4 units, add every 0.5N",
    },
    "pyramid_4x10": {
        "crypto": {"max_pyramid_units": 4, "pyramid_n_step": 1.0},
        "note": "Up to 4 units, add every 1.0N",
    },
    "turtle_full": {
        "crypto": {
            "sizing_mode": "atr_risk",
            "atr_risk_pct": 0.01,
            "backup_entry_lookback": 55,
            "max_pyramid_units": 4,
            "pyramid_n_step": 0.5,
        },
        "note": "ATR 1% risk + 55d backup + pyramid 4×0.5N",
    },
}


def strat_for_mode(symbol: str, mode: str):
    base = live_strategy_config(symbol)
    if symbol.upper() not in LIVE_CRYPTO_SYMBOLS:
        return base
    overrides = MODES[mode]["crypto"]
    return replace(base, **overrides) if overrides else base


def run_mode(raw: dict[str, pd.DataFrame], mode: str) -> dict[str, Any]:
    symbols = tuple(LIVE_SYMBOLS)
    strats = {s: strat_for_mode(s, mode) for s in symbols}
    curves, _, trades, _ = run_full_book_live(raw, symbols, strats)
    initial = sum(float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in symbols)
    metrics = portfolio_metrics(curves, trades, initial)
    sym_col = "sleeve" if "sleeve" in trades.columns else "symbol"
    if not trades.empty:
        crypto = trades[trades[sym_col].isin(LIVE_CRYPTO_SYMBOLS)]
        metrics["trades"] = int(len(trades))
        metrics["crypto_trades"] = int(len(crypto))
        if "pyramid_units" in crypto.columns:
            metrics["avg_pyramid_units"] = float(pd.to_numeric(crypto["pyramid_units"], errors="coerce").mean())
            metrics["max_pyramid_units"] = int(
                pd.to_numeric(crypto["pyramid_units"], errors="coerce").max() or 1
            )
        else:
            metrics["avg_pyramid_units"] = 1.0
            metrics["max_pyramid_units"] = 1
    return metrics


def main() -> None:
    symbols = tuple(LIVE_SYMBOLS)
    print(f"Preloading OHLC for {symbols} ...", flush=True)
    raw = preload_raw(symbols)
    print("Preload done.\n", flush=True)

    results: dict[str, Any] = {}
    for mode, meta in MODES.items():
        metrics = run_mode(raw, mode)
        results[mode] = {"note": meta["note"], "metrics": metrics}
        m = metrics
        print(
            f"{mode:18} ret={m['return_pct']:6.1f}%  CAGR={m['cagr_pct']:5.1f}%  "
            f"bookDD={m['max_drawdown_pct']:6.2f}%  sleeveDD={m['worst_sleeve_max_drawdown_pct']:6.2f}% "
            f"({m['worst_sleeve']})  PF={m['profit_factor']:.2f}  Sharpe={m['sharpe_ratio']:.2f}  "
            f"trades={m.get('trades', m.get('trades', 0))}  "
            f"pyr_max={m.get('max_pyramid_units', 1)}",
            flush=True,
        )

    baseline = results["baseline"]["metrics"]
    for mode in MODES:
        if mode == "baseline":
            continue
        results[mode]["passes_baseline"] = beats_or_ties_baseline(baseline, results[mode]["metrics"])

    passing = [k for k in MODES if k != "baseline" and results[k].get("passes_baseline")]
    noop = [
        k
        for k in passing
        if results[k]["metrics"]["return_pct"] == baseline["return_pct"]
        and results[k]["metrics"].get("trades", 0) == baseline.get("trades", 0)
    ]
    passing = [k for k in passing if k not in noop]
    recommended = (
        max(passing, key=lambda k: results[k]["metrics"]["return_pct"])
        if passing
        else "baseline"
    )

    by_category = {
        "sizing": [m for m in ("atr_risk_005", "atr_risk_01", "atr_risk_015") if m in MODES],
        "backup": ["backup_55"],
        "pyramid": ["pyramid_4x05", "pyramid_4x10"],
        "combined": ["turtle_full"],
    }

    payload = {
        "modes": MODES,
        "baseline_metrics": baseline,
        "results": results,
        "recommended": recommended,
        "passing_modes": passing,
        "by_category": by_category,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nPassing gate: {passing or ['(none)']}")
    print(f"Recommended: {recommended}")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
