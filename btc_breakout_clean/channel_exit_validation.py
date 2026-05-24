#!/usr/bin/env python3
"""
Experiment 2: Turtle 10-day low channel exit on crypto sleeves.

Modes (crypto only; metals/oil unchanged):
  - baseline: dynamic hold + fixed % stops (live)
  - channel_add_10: also exit if close < 10-day low channel (after hold_min)
  - channel_replace_10: channel exit instead of momentum_fade

Run from repo root:
  python3 btc_breakout_clean/channel_exit_validation.py
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

OUT_PATH = HERE / "channel_exit_validation_results.json"

MODES: dict[str, dict[str, Any]] = {
    "baseline": {
        "crypto": {"exit_channel_lookback": 0, "channel_exit_replaces_fade": False},
        "note": "Live: momentum fade + fixed % stops",
    },
    "channel_add_10": {
        "crypto": {"exit_channel_lookback": 10, "channel_exit_replaces_fade": False},
        "note": "Add 10d low channel exit after hold_min",
    },
    "channel_replace_10": {
        "crypto": {
            "exit_channel_lookback": 10,
            "channel_exit_replaces_fade": True,
        },
        "note": "10d channel exit instead of momentum fade",
    },
}


def strat_for_mode(symbol: str, mode: str):
    base = live_strategy_config(symbol)
    if symbol.upper() not in LIVE_CRYPTO_SYMBOLS:
        return base
    return replace(base, **MODES[mode]["crypto"])


def run_mode(raw: dict[str, pd.DataFrame], mode: str) -> dict[str, Any]:
    symbols = tuple(LIVE_SYMBOLS)
    strats = {s: strat_for_mode(s, mode) for s in symbols}
    curves, _, trades, _ = run_full_book_live(raw, symbols, strats)
    initial = sum(float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in symbols)
    metrics = portfolio_metrics(curves, trades, initial)
    sym_col = "sleeve" if "sleeve" in trades.columns else "symbol"
    if not trades.empty and "exit_reason" in trades.columns:
        metrics["exit_reasons"] = {
            str(k): int(v) for k, v in trades["exit_reason"].value_counts().items()
        }
        crypto = trades[trades[sym_col].isin(LIVE_CRYPTO_SYMBOLS)]
        ch = crypto[crypto["exit_reason"] == "channel_exit"] if not crypto.empty else crypto
        metrics["channel_exit_count"] = int(len(ch))
    else:
        metrics["exit_reasons"] = {}
        metrics["channel_exit_count"] = 0
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
            f"ch_exit={m.get('channel_exit_count', 0)}",
            flush=True,
        )

    baseline = results["baseline"]["metrics"]
    for mode in MODES:
        if mode == "baseline":
            continue
        results[mode]["passes_baseline"] = beats_or_ties_baseline(baseline, results[mode]["metrics"])

    passing = [k for k in MODES if k != "baseline" and results[k].get("passes_baseline")]
    # Prefer baseline when a "passing" mode is a no-op (identical metrics).
    noop = [
        k
        for k in passing
        if results[k]["metrics"]["return_pct"] == baseline["return_pct"]
        and results[k]["metrics"]["trades"] == baseline["trades"]
    ]
    passing = [k for k in passing if k not in noop]
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
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nRecommended: {recommended}")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
