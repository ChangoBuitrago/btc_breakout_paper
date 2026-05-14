#!/usr/bin/env python3
"""
Compare fixed min hold vs dynamic [min,max] momentum exit vs fixed max hold.

Run from repo root:
  python3 btc_breakout_clean/dynamic_hold_validation.py
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
    live_symbol_equity,
    live_strategy_config,
)
from btc_breakout_paper_sim import (  # noqa: E402
    StrategyConfig,
    add_indicators,
    default_skip_saturday_entry,
    dukascopy_cache_path,
    fetch_source_data,
)
from strategy_validation import (  # noqa: E402
    DATA_START,
    beats_baseline,
    portfolio_metrics,
    preload_raw,
    run_sleeve,
    _sim_cfg,
)

OUT_PATH = HERE / "dynamic_hold_validation_results.json"


def strat_variant(symbol: str, mode: str) -> StrategyConfig:
    base = live_strategy_config(symbol)
    hold_min = int(LIVE_STRATEGY_PARAMS[symbol.upper()].get("hold_min", base.hold_days))
    hold_max = int(LIVE_STRATEGY_PARAMS[symbol.upper()].get("hold_max", hold_min))
    if mode == "fixed_min":
        return replace(
            base,
            hold_days=hold_min,
            hold_min=hold_min,
            hold_max=hold_max,
            dynamic_hold=False,
        )
    if mode == "fixed_max":
        return replace(
            base,
            hold_days=hold_max,
            hold_min=hold_min,
            hold_max=hold_max,
            dynamic_hold=False,
        )
    if mode == "dynamic":
        return replace(
            base,
            hold_days=hold_min,
            hold_min=hold_min,
            hold_max=hold_max,
            dynamic_hold=True,
        )
    raise ValueError(mode)


def run_portfolio(mode: str, raw: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    curves: dict[str, pd.DataFrame] = {}
    trade_parts: list[pd.DataFrame] = []
    for sym in LIVE_SYMBOLS:
        equity = live_symbol_equity(sym, float(LIVE_STRATEGY_PARAMS[sym]["equity"]))
        strat = strat_variant(sym, mode)
        trades, curve, _ = run_sleeve(raw[sym], sym, strat, equity)
        curves[sym] = curve
        if not trades.empty:
            t = trades.copy()
            t["symbol"] = sym
            trade_parts.append(t)
    all_trades = pd.concat(trade_parts, ignore_index=True) if trade_parts else pd.DataFrame()
    return curves, all_trades


def exit_reason_breakdown(trades: pd.DataFrame) -> dict[str, int]:
    if trades.empty or "exit_reason" not in trades.columns:
        return {}
    return {str(k): int(v) for k, v in trades["exit_reason"].value_counts().items()}


def main() -> None:
    raw = preload_raw(tuple(LIVE_SYMBOLS))
    initial_total = sum(float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in LIVE_SYMBOLS)

    results: dict[str, Any] = {}
    for mode in ("fixed_min", "dynamic", "fixed_max"):
        curves, trades = run_portfolio(mode, raw)
        metrics = portfolio_metrics(curves, trades, initial_total)
        results[mode] = {
            "metrics": metrics,
            "exit_reasons": exit_reason_breakdown(trades),
        }

    baseline = results["fixed_min"]["metrics"]
    for mode in ("dynamic", "fixed_max"):
        results[mode]["passes_baseline"] = beats_baseline(baseline, results[mode]["metrics"])

    payload = {
        "baseline_mode": "fixed_min",
        "hold_ranges": {
            s: {
                "hold_min": LIVE_STRATEGY_PARAMS[s].get("hold_min", LIVE_STRATEGY_PARAMS[s]["hold_days"]),
                "hold_max": LIVE_STRATEGY_PARAMS[s].get("hold_max", LIVE_STRATEGY_PARAMS[s]["hold_days"]),
            }
            for s in LIVE_SYMBOLS
        },
        "results": results,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
