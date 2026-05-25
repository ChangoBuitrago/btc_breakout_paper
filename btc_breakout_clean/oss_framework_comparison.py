#!/usr/bin/env python3
"""
Compare Simona live book vs Turtle S1 vs open-source strategy templates
(Freqtrade / QuantConnect LEAN proxies) on the same 8 sleeves, 2018+, fees, max-4.

Run:
  .venv/bin/python btc_breakout_clean/oss_framework_comparison.py
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
from oss_reference_sim import (  # noqa: E402
    OSS_STRATEGIES,
    OSSStrategyConfig,
    add_oss_indicators,
    simulate_oss_account,
)
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

OUT_PATH = HERE / "oss_framework_comparison_results.json"


def run_oss_sleeve(
    raw: pd.DataFrame,
    symbol: str,
    equity: float,
    oss_id: str,
    *,
    blocked: frozenset[pd.Timestamp] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    cfg = OSS_STRATEGIES[oss_id]
    fee = float(LIVE_STRATEGY_PARAMS[symbol]["fee_bps"])
    oss_cfg = OSSStrategyConfig(
        strategy_id=cfg.strategy_id,
        label=cfg.label,
        source=cfg.source,
        fee_bps=fee,
        vol_target=cfg.vol_target,
        max_alloc=cfg.max_alloc,
        max_hold=cfg.max_hold,
        compound=cfg.compound,
    )
    sim = _sim_cfg(symbol, equity, blocked_entry_dates=blocked or frozenset())
    df = add_oss_indicators(raw, oss_cfg)
    return simulate_oss_account(df, sim_cfg=sim, oss_cfg=oss_cfg)


def run_oss_book(
    raw: dict[str, pd.DataFrame],
    oss_id: str,
    *,
    max_concurrent: int = LIVE_MAX_CONCURRENT_ENTRIES,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    symbols = tuple(LIVE_SYMBOLS)
    equities = {s: float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in symbols}
    curves: dict[str, pd.DataFrame] = {}
    parts: list[pd.DataFrame] = []

    for sym in symbols:
        tr, cu, _ = run_oss_sleeve(raw[sym], sym, equities[sym], oss_id)
        curves[sym] = cu
        if not tr.empty:
            p = tr.copy()
            p["sleeve"] = sym
            parts.append(p)

    all_trades = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()

    if max_concurrent > 0 and len(symbols) > 1 and not all_trades.empty:
        blocked = blocked_entries_max_concurrent(all_trades, max_concurrent)
        curves = {}
        parts = []
        for sym in symbols:
            tr, cu, _ = run_oss_sleeve(
                raw[sym], sym, equities[sym], oss_id, blocked=blocked.get(sym, frozenset())
            )
            curves[sym] = cu
            if not tr.empty:
                p = tr.copy()
                p["sleeve"] = sym
                parts.append(p)
        all_trades = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()

    return curves, all_trades


def run_turtle_sleeve(
    raw: pd.DataFrame,
    symbol: str,
    equity: float,
    *,
    blocked: frozenset[pd.Timestamp] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    fee = float(LIVE_STRATEGY_PARAMS[symbol]["fee_bps"])
    turtle_cfg = TurtleConfig(fee_bps=fee, compound=True, long_only=True)
    sim = _sim_cfg(symbol, equity, blocked_entry_dates=blocked or frozenset())
    df = add_turtle_indicators(raw, turtle_cfg)
    return simulate_turtle_account(df, sim_cfg=sim, turtle_cfg=turtle_cfg)


def run_turtle_book(
    raw: dict[str, pd.DataFrame],
    *,
    max_concurrent: int = LIVE_MAX_CONCURRENT_ENTRIES,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    symbols = tuple(LIVE_SYMBOLS)
    equities = {s: float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in symbols}
    curves: dict[str, pd.DataFrame] = {}
    parts: list[pd.DataFrame] = []
    for sym in symbols:
        tr, cu, _ = run_turtle_sleeve(raw[sym], sym, equities[sym])
        curves[sym] = cu
        if not tr.empty:
            p = tr.copy()
            p["sleeve"] = sym
            parts.append(p)
    all_trades = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if max_concurrent > 0 and len(symbols) > 1 and not all_trades.empty:
        blocked = blocked_entries_max_concurrent(all_trades, max_concurrent)
        curves = {}
        parts = []
        for sym in symbols:
            tr, cu, _ = run_turtle_sleeve(
                raw[sym], sym, equities[sym], blocked=blocked.get(sym, frozenset())
            )
            curves[sym] = cu
            if not tr.empty:
                p = tr.copy()
                p["sleeve"] = sym
                parts.append(p)
        all_trades = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    return curves, all_trades


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


def _fmt(val: Any, width: int, prec: int = 1, suffix: str = "") -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return f"{'n/a':>{width}}"
    if isinstance(val, (int, float)):
        return f"{val:>{width}.{prec}f}{suffix}"
    return f"{str(val):>{width}}"


def print_table(rows: list[dict[str, Any]]) -> None:
    print(f"\n{'Strategy':<48} {'CAGR':>7} {'Return':>8} {'BookDD':>7} {'PF':>6} {'Shrp':>6} {'Trd':>5}")
    print("-" * 95)
    for r in rows:
        print(
            f"{r['label']:<48} "
            f"{_fmt(r.get('cagr_pct'), 7)} "
            f"{_fmt(r.get('return_pct'), 7, 1, '%')} "
            f"{_fmt(r.get('max_drawdown_pct'), 7, 2)} "
            f"{_fmt(r.get('profit_factor'), 6, 2)} "
            f"{_fmt(r.get('sharpe_ratio'), 6, 2)} "
            f"{int(r.get('trades') or 0):5d}"
        )


def main() -> None:
    symbols = tuple(LIVE_SYMBOLS)
    print(f"Preloading OHLC ({DATA_START}+) for {symbols} ...", flush=True)
    raw = preload_raw(symbols)
    print("Preload done.\n", flush=True)

    initial = sum(float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in symbols)
    sleeve_eq = {s: float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in symbols}
    rows: list[dict[str, Any]] = []

    print("Simona live book ...", flush=True)
    live_curves, _, live_trades, _ = run_full_book_live(
        raw, symbols, {s: live_strategy_config(s) for s in symbols}
    )
    live_m = portfolio_metrics(live_curves, live_trades, initial, initial_equity_by_sleeve=sleeve_eq)
    rows.append(metrics_row("Simona live breakout (this repo)", live_m, extra={"family": "simona"}))

    print("Turtle S1 ...", flush=True)
    t_curves, t_trades = run_turtle_book(raw)
    t_m = portfolio_metrics(t_curves, t_trades, initial, initial_equity_by_sleeve=sleeve_eq)
    rows.append(metrics_row("Turtle S1 (open trend template)", t_m, extra={"family": "turtle"}))

    results: dict[str, Any] = {"simona": live_m, "turtle_s1": t_m, "oss": {}}
    for oss_id, cfg in OSS_STRATEGIES.items():
        print(f"{cfg.label} ...", flush=True)
        curves, trades = run_oss_book(raw, oss_id)
        m = portfolio_metrics(curves, trades, initial, initial_equity_by_sleeve=sleeve_eq)
        results["oss"][oss_id] = m
        rows.append(
            metrics_row(
                f"[{cfg.source}] {cfg.label}",
                m,
                extra={"family": cfg.source, "strategy_id": oss_id},
            )
        )

    print_table(rows)

    vs = []
    for r in rows[2:]:
        vs.append(
            {
                "label": r["label"],
                "return_vs_simona": (r["return_pct"] or 0) - live_m["return_pct"],
                "sharpe_vs_simona": (r["sharpe_ratio"] or 0) - live_m["sharpe_ratio"],
                "dd_vs_simona": (r["max_drawdown_pct"] or 0) - live_m["max_drawdown_pct"],
            }
        )

    payload = {
        "period": {"data_start": DATA_START, "sim_start": SIM_START},
        "symbols": list(symbols),
        "initial_equity": initial,
        "comparison_table": rows,
        "vs_simona": vs,
        "results": results,
        "note": (
            "OSS rows are long-only template proxies on identical daily data/fees/sizing "
            "(vol_target 1.5%, max_alloc 75%), not live Freqtrade/LEAN installs."
        ),
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
