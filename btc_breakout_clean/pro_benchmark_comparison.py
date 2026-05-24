#!/usr/bin/env python3
"""
Compare live breakout book vs Turtle S1 reference on the same symbols/data/fees.

Run from repo root:
  python3 btc_breakout_clean/pro_benchmark_comparison.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import (  # noqa: E402
    LIVE_MAX_CONCURRENT_ENTRIES,
    LIVE_STRATEGY_PARAMS,
    LIVE_SYMBOLS,
    live_strategy_config,
)
from btc_breakout_paper_sim import add_indicators  # noqa: E402
from strategy_validation import (  # noqa: E402
    DATA_START,
    SIM_START,
    _sim_cfg,
    blocked_entries_max_concurrent,
    portfolio_metrics,
    preload_raw,
    run_full_book_live,
)
from turtle_reference_sim import TurtleConfig, add_turtle_indicators, simulate_turtle_account  # noqa: E402

OUT_PATH = HERE / "pro_benchmark_comparison_results.json"

# Published benchmarks (not same period/methodology — reference only)
LITERATURE_BENCHMARKS: list[dict[str, Any]] = [
    {
        "label": "Turtle original (futures, literature)",
        "return_pct": None,
        "cagr_pct": 72.0,
        "max_drawdown_pct": -66.0,
        "profit_factor": None,
        "sharpe_ratio": None,
        "annualized_vol_pct": None,
        "calmar_ratio": 1.09,
        "note": "Trading with Rayner / Turtle rules summary",
    },
    {
        "label": "Turtle revised 1970-2009 (futures)",
        "return_pct": None,
        "cagr_pct": 35.3,
        "max_drawdown_pct": -15.6,
        "profit_factor": None,
        "sharpe_ratio": None,
        "annualized_vol_pct": None,
        "calmar_ratio": 2.26,
        "note": "Revised rules, 100+ markets",
    },
    {
        "label": "BTC Donchian 20/10 (Boring Edge blog)",
        "return_pct": 2786.0,
        "cagr_pct": 48.2,
        "max_drawdown_pct": -53.7,
        "profit_factor": None,
        "sharpe_ratio": None,
        "annualized_vol_pct": None,
        "calmar_ratio": 0.90,
        "note": "BTC only, Sep 2017-Mar 2026, 100% in/out",
    },
]


def run_turtle_sleeve(
    raw: pd.DataFrame,
    symbol: str,
    equity: float,
    *,
    blocked: frozenset[pd.Timestamp] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    fee = float(LIVE_STRATEGY_PARAMS[symbol]["fee_bps"])
    turtle_cfg = TurtleConfig(fee_bps=fee, compound=True, long_only=True)
    sim = _sim_cfg(
        symbol,
        equity,
        blocked_entry_dates=blocked or frozenset(),
    )
    df = add_turtle_indicators(raw, turtle_cfg)
    return simulate_turtle_account(df, sim_cfg=sim, turtle_cfg=turtle_cfg)


def run_turtle_book(
    raw: dict[str, pd.DataFrame],
    *,
    max_concurrent: int | None = LIVE_MAX_CONCURRENT_ENTRIES,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    symbols = tuple(LIVE_SYMBOLS)
    equities = {s: float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in symbols}

    trade_parts: list[pd.DataFrame] = []
    curves: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        tr, cu, _ = run_turtle_sleeve(raw[sym], sym, equities[sym])
        curves[sym] = cu
        if not tr.empty:
            part = tr.copy()
            part["sleeve"] = sym
            trade_parts.append(part)

    all_trades = pd.concat(trade_parts, ignore_index=True) if trade_parts else pd.DataFrame()

    if max_concurrent and max_concurrent > 0 and len(symbols) > 1:
        blocked = blocked_entries_max_concurrent(all_trades, max_concurrent)
        curves = {}
        trade_parts = []
        for sym in symbols:
            tr, cu, _ = run_turtle_sleeve(
                raw[sym], sym, equities[sym], blocked=blocked.get(sym, frozenset())
            )
            curves[sym] = cu
            if not tr.empty:
                part = tr.copy()
                part["sleeve"] = sym
                trade_parts.append(part)
        all_trades = pd.concat(trade_parts, ignore_index=True) if trade_parts else pd.DataFrame()

    return curves, all_trades


def run_live_book(raw: dict[str, pd.DataFrame]) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    symbols = tuple(LIVE_SYMBOLS)
    strats = {s: live_strategy_config(s) for s in symbols}
    curves, _, trades, _ = run_full_book_live(raw, symbols, strats)
    return curves, trades


def metrics_row(label: str, m: dict[str, Any], *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {
        "label": label,
        "return_pct": m.get("return_pct"),
        "cagr_pct": m.get("cagr_pct"),
        "max_drawdown_pct": m.get("max_drawdown_pct"),
        "worst_sleeve_max_drawdown_pct": m.get("worst_sleeve_max_drawdown_pct"),
        "worst_sleeve": m.get("worst_sleeve"),
        "profit_factor": m.get("profit_factor"),
        "sharpe_ratio": m.get("sharpe_ratio"),
        "annualized_vol_pct": m.get("annualized_vol_pct"),
        "calmar_ratio": m.get("calmar_ratio"),
        "trades": m.get("trades"),
    }
    if extra:
        row.update(extra)
    return row


def _fmt(val: Any, width: int, prec: int = 1) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return f"{'n/a':>{width}}"
    if isinstance(val, (int, float)):
        return f"{val:>{width}.{prec}f}" if prec else f"{val:>{width}}"
    return f"{str(val):>{width}}"


def print_comparison_table(rows: list[dict[str, Any]]) -> None:
    print(f"\n{'Strategy':<42} {'CAGR':>7} {'BookDD':>7} {'SlvDD':>7} {'PF':>6} {'Shrp':>6} {'Vol':>6} {'Cal':>6} {'Trd':>5}")
    print("-" * 100)
    for r in rows:
        print(
            f"{r['label']:<42} "
            f"{_fmt(r.get('cagr_pct'), 7)} "
            f"{_fmt(r.get('max_drawdown_pct'), 7)} "
            f"{_fmt(r.get('worst_sleeve_max_drawdown_pct'), 7)} "
            f"{_fmt(r.get('profit_factor'), 6, 2)} "
            f"{_fmt(r.get('sharpe_ratio'), 6, 2)} "
            f"{_fmt(r.get('annualized_vol_pct'), 6, 1)} "
            f"{_fmt(r.get('calmar_ratio'), 6, 2)} "
            f"{_fmt(r.get('trades'), 5, 0)}"
        )


def main() -> None:
    symbols = tuple(LIVE_SYMBOLS)
    print(f"Preloading OHLC ({DATA_START}+) for {symbols} ...", flush=True)
    raw = preload_raw(symbols)
    print("Preload done.\n", flush=True)

    initial_total = sum(float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in symbols)

    print("Running live book (per-symbol stops, max 4 concurrent) ...", flush=True)
    live_curves, live_trades = run_live_book(raw)
    live_m = portfolio_metrics(live_curves, live_trades, initial_total)

    print("Running Turtle S1 reference (20/10, 2N stop, 1% risk, long-only, max 4 concurrent) ...", flush=True)
    turtle_curves, turtle_trades = run_turtle_book(raw, max_concurrent=LIVE_MAX_CONCURRENT_ENTRIES)
    turtle_m = portfolio_metrics(turtle_curves, turtle_trades, initial_total)

    rows = [
        metrics_row("Live breakout book (Simona)", live_m),
        metrics_row("Turtle S1 replay (same data/fees)", turtle_m),
    ]
    for lit in LITERATURE_BENCHMARKS:
        rows.append({**lit})

    print_comparison_table(rows)

    payload = {
        "period": {"data_start": DATA_START, "sim_start": SIM_START},
        "initial_equity": initial_total,
        "live_book": live_m,
        "turtle_s1_replay": turtle_m,
        "comparison_table": rows,
        "live_per_sleeve_dd": live_m.get("sleeve_max_drawdown_pct", {}),
        "turtle_per_sleeve_dd": turtle_m.get("sleeve_max_drawdown_pct", {}),
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
