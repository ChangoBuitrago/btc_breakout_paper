#!/usr/bin/env python3
"""
False-breakout filters: close-in-range, range expansion, weekly trend confirm.

Modes on full live 8-sleeve book (2018+, max 4 concurrent, per-symbol crypto stops):
  - baseline: live
  - close_top_65 / close_top_70: strong close within breakout day
  - range_expansion: day range >= 20d average
  - weekly_trend: close > weekly SMA(40w)
  - quality_combo: all three

Run:
  .venv/bin/python btc_breakout_clean/breakout_quality_validation.py
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

from btc_breakout_binance_paper_bot import LIVE_STRATEGY_PARAMS, LIVE_SYMBOLS, live_strategy_config  # noqa: E402
from portfolio_param_sweep import beats_or_ties_baseline  # noqa: E402
from strategy_validation import portfolio_metrics, preload_raw, run_full_book_live  # noqa: E402

OUT_PATH = HERE / "breakout_quality_validation_results.json"

MODES: dict[str, dict[str, Any]] = {
    "baseline": {"overrides": {}, "note": "Live (no quality filters)"},
    "close_top_65": {
        "overrides": {"breakout_min_close_position": 0.65},
        "note": "Close in top 35% of day range on signal bar",
    },
    "close_top_70": {
        "overrides": {"breakout_min_close_position": 0.70},
        "note": "Close in top 30% of day range",
    },
    "range_expansion": {
        "overrides": {"breakout_min_range_expansion": 1.0},
        "note": "Day range >= 20d average range",
    },
    "range_expansion_1p2": {
        "overrides": {"breakout_min_range_expansion": 1.2},
        "note": "Day range >= 1.2× 20d average",
    },
    "weekly_trend": {
        "overrides": {"require_weekly_trend": True},
        "note": "Weekly close > 40-week SMA",
    },
    "quality_combo": {
        "overrides": {
            "breakout_min_close_position": 0.65,
            "breakout_min_range_expansion": 1.0,
            "require_weekly_trend": True,
        },
        "note": "Close top 35% + range expansion + weekly trend",
    },
}


def strat_for_mode(mode: str) -> dict[str, Any]:
    ov = MODES[mode]["overrides"]
    return {s: replace(live_strategy_config(s), **ov) for s in LIVE_SYMBOLS}


def run_mode(raw: dict[str, pd.DataFrame], mode: str) -> dict[str, Any]:
    symbols = tuple(LIVE_SYMBOLS)
    strats = strat_for_mode(mode)
    curves, _, trades, _ = run_full_book_live(raw, symbols, strats)
    initial = sum(float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in symbols)
    metrics = portfolio_metrics(curves, trades, initial)
    metrics["trades"] = int(len(trades))
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
            f"{mode:22} ret={m['return_pct']:6.1f}%  CAGR={m['cagr_pct']:5.1f}%  "
            f"bookDD={m['max_drawdown_pct']:6.2f}%  sleeveDD={m['worst_sleeve_max_drawdown_pct']:6.2f}% "
            f"({m['worst_sleeve']})  PF={m['profit_factor']:.2f}  Sharpe={m['sharpe_ratio']:.2f}  "
            f"trades={m['trades']}",
            flush=True,
        )

    baseline = results["baseline"]["metrics"]
    for mode in MODES:
        if mode == "baseline":
            continue
        results[mode]["passes_baseline"] = beats_or_ties_baseline(baseline, results[mode]["metrics"])

    passing = [k for k in MODES if k != "baseline" and results[k].get("passes_baseline")]
    recommended = (
        max(passing, key=lambda k: results[k]["metrics"]["return_pct"])
        if passing
        else "baseline"
    )

    payload = {
        "modes": MODES,
        "baseline_metrics": baseline,
        "results": results,
        "recommended": recommended,
        "passing_modes": passing,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nPassing gate: {passing or ['(none)']}")
    print(f"Recommended: {recommended}")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
