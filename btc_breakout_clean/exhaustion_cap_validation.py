#!/usr/bin/env python3
"""
Exhaustion-cap sweep: does loosening `max_breakout_bps` add edge or just add trades?

The live cap (225 bps) rejects breakouts that gap > 2.25% above the prior high.
Empirically it is the single largest filter on crypto signals (~15-22/yr/sleeve),
far more than the regime filter. This script tests whether relaxing or removing it
improves the book — run on the full live 7-sleeve book (2018+, max 4 concurrent,
per-symbol crypto stops). A candidate is only worth adopting if it beats/ties the
baseline on return AND profit factor AND max drawdown.

Run:
  .venv/bin/python btc_breakout_clean/exhaustion_cap_validation.py
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
    LIVE_STRATEGY_PARAMS,
    LIVE_SYMBOLS,
    live_strategy_config,
)
from btc_breakout_paper_sim import StrategyConfig  # noqa: E402
from portfolio_param_sweep import beats_or_ties_baseline  # noqa: E402
from strategy_validation import portfolio_metrics, preload_raw, run_full_book_live  # noqa: E402

OUT_PATH = HERE / "exhaustion_cap_validation_results.json"

# None => cap off (accept any breakout size).
MODES: dict[str, float | None] = {
    "baseline_225": 225.0,
    "cap_300": 300.0,
    "cap_400": 400.0,
    "cap_600": 600.0,
    "cap_off": None,
}


def strat_for_cap(cap: float | None) -> dict[str, StrategyConfig]:
    return {s: replace(live_strategy_config(s), max_breakout_bps=cap) for s in LIVE_SYMBOLS}


def sleeve_trade_counts(trades: pd.DataFrame) -> dict[str, int]:
    if trades.empty or "symbol" not in trades.columns:
        return {}
    return {str(k): int(v) for k, v in trades["symbol"].value_counts().items()}


def run_mode(raw: dict[str, pd.DataFrame], cap: float | None) -> dict[str, Any]:
    symbols = tuple(LIVE_SYMBOLS)
    strats = strat_for_cap(cap)
    curves, _, trades, _ = run_full_book_live(raw, symbols, strats)
    initial = sum(float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in symbols)
    metrics = portfolio_metrics(curves, trades, initial)
    metrics["trades"] = int(len(trades))
    metrics["sleeve_trades"] = sleeve_trade_counts(trades)
    return metrics


def main() -> None:
    symbols = tuple(LIVE_SYMBOLS)
    print(f"Preloading OHLC for {symbols} ...", flush=True)
    raw = preload_raw(symbols)
    print("Preload done.\n", flush=True)

    results: dict[str, Any] = {}
    for mode, cap in MODES.items():
        m = run_mode(raw, cap)
        results[mode] = {"cap_bps": cap, "metrics": m}
        print(
            f"{mode:13} cap={'off' if cap is None else f'{cap:.0f}bps':>7}  "
            f"ret={m['return_pct']:7.1f}%  CAGR={m['cagr_pct']:5.1f}%  "
            f"bookDD={m['max_drawdown_pct']:7.2f}%  PF={m['profit_factor']:.2f}  "
            f"Sharpe={m['sharpe_ratio']:.2f}  trades={m['trades']}",
            flush=True,
        )

    baseline = results["baseline_225"]["metrics"]
    print(f"\nBaseline trades/sleeve: {baseline['sleeve_trades']}")
    for mode in MODES:
        if mode == "baseline_225":
            continue
        cand = results[mode]["metrics"]
        results[mode]["passes_baseline"] = beats_or_ties_baseline(baseline, cand)
        extra = cand["trades"] - baseline["trades"]
        dpf = cand["profit_factor"] - baseline["profit_factor"]
        dret = cand["return_pct"] - baseline["return_pct"]
        print(
            f"  {mode:10} +{extra:>3} trades | Δreturn {dret:+7.1f}pp | "
            f"ΔPF {dpf:+.2f} | passes={results[mode]['passes_baseline']}"
        )

    passing = [k for k in MODES if k != "baseline_225" and results[k].get("passes_baseline")]
    recommended = (
        max(passing, key=lambda k: results[k]["metrics"]["return_pct"])
        if passing
        else "baseline_225"
    )

    payload = {
        "modes": {k: ("off" if v is None else v) for k, v in MODES.items()},
        "baseline_metrics": baseline,
        "results": results,
        "recommended": recommended,
        "passing_modes": passing,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nPassing gate (beats/ties baseline on ret+PF+DD): {passing or ['(none)']}")
    print(f"Recommended: {recommended}")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
