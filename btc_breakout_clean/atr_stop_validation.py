#!/usr/bin/env python3
"""
Experiment 1: Turtle-style 2×N ATR stops vs fixed-% stops (crypto sleeves).

Modes on crypto only (metals/oil unchanged):
  - baseline: per-symbol stop_loss_pct (live)
  - atr_2n: stop_atr_mult=2.0, stop_loss_pct=0
  - hybrid: both; tighter stop wins (max of pct floor and ATR floor)

Run from repo root:
  python3 btc_breakout_clean/atr_stop_validation.py
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
from strategy_validation import (  # noqa: E402
    portfolio_metrics,
    preload_raw,
    run_full_book_live,
)

OUT_PATH = HERE / "atr_stop_validation_results.json"

MODES: dict[str, dict[str, Any]] = {
    "baseline_fixed_pct": {
        "crypto": {"stop_atr_mult": 0.0},
        "note": "Live per-symbol stop_loss_pct",
    },
    "atr_2n_only": {
        "crypto": {"stop_atr_mult": 2.0, "stop_loss_pct": 0.0},
        "note": "Turtle 2×N (20-day TR), no fixed %",
    },
    "hybrid_tighter": {
        "crypto": {"stop_atr_mult": 2.0},
        "note": "Fixed % + 2×N; tighter (higher) floor wins",
    },
}


def strat_for_mode(symbol: str, mode: str) -> Any:
    base = live_strategy_config(symbol)
    if symbol.upper() not in LIVE_CRYPTO_SYMBOLS:
        return base
    overrides = MODES[mode]["crypto"]
    return replace(base, **overrides)


def run_mode(
    raw: dict[str, pd.DataFrame],
    mode: str,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame, dict[str, Any]]:
    symbols = tuple(LIVE_SYMBOLS)
    strats = {s: strat_for_mode(s, mode) for s in symbols}
    curves, _, trades, _ = run_full_book_live(raw, symbols, strats)
    initial = sum(float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in symbols)
    metrics = portfolio_metrics(curves, trades, initial)
    stops = trades[trades["exit_reason"].isin(("stop_loss", "stop_atr"))] if not trades.empty else trades
    stop_breakdown = (
        {str(k): int(v) for k, v in stops["exit_reason"].value_counts().items()} if not stops.empty else {}
    )
    sym_col = "symbol" if "symbol" in trades.columns else "sleeve"
    crypto = trades[trades[sym_col].isin(LIVE_CRYPTO_SYMBOLS)] if not trades.empty else trades
    if not crypto.empty and "open_to_exit_pct" in crypto.columns:
        worst = float(crypto["open_to_exit_pct"].min())
    else:
        worst = float("nan")
    return curves, trades, {**metrics, "stop_exits": stop_breakdown, "crypto_worst_pct": worst}


def main() -> None:
    symbols = tuple(LIVE_SYMBOLS)
    print(f"Preloading OHLC for {symbols} ...", flush=True)
    raw = preload_raw(symbols)
    print("Preload done.\n", flush=True)

    results: dict[str, Any] = {}
    for mode, meta in MODES.items():
        _, trades, metrics = run_mode(raw, mode)
        results[mode] = {
            "note": meta["note"],
            "metrics": metrics,
            "stop_exits": metrics.get("stop_exits", {}),
            "crypto_worst_trade_pct": metrics.get("crypto_worst_pct"),
        }
        m = metrics
        print(
            f"{mode:22} ret={m['return_pct']:6.1f}%  CAGR={m['cagr_pct']:5.1f}%  "
            f"bookDD={m['max_drawdown_pct']:6.2f}%  sleeveDD={m['worst_sleeve_max_drawdown_pct']:6.2f}% "
            f"({m['worst_sleeve']})  PF={m['profit_factor']:.2f}  Sharpe={m['sharpe_ratio']:.2f}  "
            f"stops={results[mode]['stop_exits']}",
            flush=True,
        )

    baseline = results["baseline_fixed_pct"]["metrics"]
    for mode in MODES:
        if mode == "baseline_fixed_pct":
            continue
        results[mode]["passes_baseline"] = beats_or_ties_baseline(baseline, results[mode]["metrics"])

    best = max(
        (m for m in MODES if m != "baseline_fixed_pct"),
        key=lambda k: (
            results[k]["passes_baseline"],
            results[k]["metrics"]["return_pct"],
            results[k]["metrics"]["max_drawdown_pct"],
        ),
    )
    payload = {
        "modes": MODES,
        "crypto_symbols": sorted(LIVE_CRYPTO_SYMBOLS),
        "baseline_metrics": baseline,
        "results": results,
        "recommended": best if results[best].get("passes_baseline") else "baseline_fixed_pct",
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nRecommended: {payload['recommended']}")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
