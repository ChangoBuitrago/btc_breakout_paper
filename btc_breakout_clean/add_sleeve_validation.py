#!/usr/bin/env python3
"""
Sweep US500 sleeve params and test 7-sleeve book vs 6-sleeve baseline.

Run from repo root:
  python3 btc_breakout_clean/add_sleeve_validation.py

Does not remove any existing sleeves — only evaluates adding US500.
"""

from __future__ import annotations

import itertools
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import (  # noqa: E402
    LIVE_SLEEVE_EQUITY,
    LIVE_STRATEGY_PARAMS,
    LIVE_SYMBOLS,
    live_strategy_config,
)
from btc_breakout_paper_sim import (  # noqa: E402
    StrategyConfig,
    add_indicators,
    default_skip_saturday_entry,
    dukascopy_cache_path,
    fetch_source_data,
    simulate_account,
)
from portfolio_param_sweep import (  # noqa: E402
    baseline_strategies,
    beats_or_ties_baseline,
    run_portfolio,
)
from strategy_validation import (  # noqa: E402
    _sim_cfg,
    beats_baseline,
    portfolio_metrics,
    preload_raw,
    run_sleeve,
    sleeve_window_metrics,
)

OUT_PATH = HERE / "add_sleeve_validation_results.json"
US500 = "US500"


def candidate_strategy_config(symbol: str = US500) -> StrategyConfig:
    params = CANDIDATE_SLEEVE_PARAMS[symbol.upper()]
    hold_min = int(params.get("hold_min", params["hold_days"]))
    hold_max = int(params.get("hold_max", hold_min))
    dynamic_hold = bool(params.get("dynamic_hold", hold_max > hold_min))
    return StrategyConfig(
        lookback=int(params["lookback"]),
        buffer_bps=float(params["buffer_bps"]),
        max_breakout_bps=float(params["max_breakout_bps"]),
        trend_mode=str(params.get("trend_mode", "bull_only")),
        hold_days=hold_min,
        trail_atr=0.0,
        fee_bps=float(params.get("fee_bps", 2.0)),
        vol_target=0.015,
        max_alloc=0.75,
        compound=bool(params.get("compound", True)),
        hold_min=hold_min,
        hold_max=hold_max,
        dynamic_hold=dynamic_hold,
        hold_giveback_pct=float(params.get("hold_giveback_pct", 0.03)),
    )


def us500_strat(
    lookback: int,
    buffer_bps: float,
    max_breakout_bps: float,
    hold_min: int,
    hold_max: int,
    trend_mode: str = "bull_only",
) -> StrategyConfig:
    return StrategyConfig(
        lookback=lookback,
        buffer_bps=buffer_bps,
        max_breakout_bps=max_breakout_bps,
        trend_mode=trend_mode,
        hold_days=hold_min,
        trail_atr=0.0,
        fee_bps=2.0,
        vol_target=0.015,
        max_alloc=0.75,
        compound=True,
        hold_min=hold_min,
        hold_max=hold_max,
        dynamic_hold=hold_max > hold_min,
        hold_giveback_pct=0.03,
    )


def load_us500_raw() -> pd.DataFrame:
    sim = _sim_cfg(
        US500,
        10_000.0,
        source="dukascopy",
        dukascopy_path=dukascopy_cache_path(US500),
        skip_saturday_entry=default_skip_saturday_entry("dukascopy"),
    )
    return fetch_source_data(sim)


def sweep_us500(raw: pd.DataFrame) -> list[dict[str, Any]]:
    lookbacks = [20, 25, 30]
    buffers = [75.0, 100.0, 125.0]
    maxbps = [200.0, 225.0, 250.0]
    hold_pairs = [(7, 12), (9, 14), (9, 15), (10, 15), (10, 18)]
    trend_modes = ["bull_only", "sma200_95"]

    ind_cache: dict[tuple[int, float, float, str], pd.DataFrame] = {}
    rows: list[dict[str, Any]] = []
    sim = _sim_cfg(US500, 10_000.0, source="dukascopy", dukascopy_path=dukascopy_cache_path(US500))

    for lb, buf, mx in itertools.product(lookbacks, buffers, maxbps):
        for trend in trend_modes:
            key = (lb, buf, mx, trend)
            if key not in ind_cache:
                base = us500_strat(lb, buf, mx, 9, 15, trend_mode=trend)
                ind_cache[key] = add_indicators(raw, base)

    ex_start = pd.Timestamp("2024-01-01", tz="UTC")
    total = len(lookbacks) * len(buffers) * len(maxbps) * len(hold_pairs) * len(trend_modes)
    n = 0
    for lb, buf, mx, trend in itertools.product(lookbacks, buffers, maxbps, trend_modes):
        df = ind_cache[(lb, buf, mx, trend)]
        for hmin, hmax in hold_pairs:
            n += 1
            if hmax <= hmin:
                continue
            strat = us500_strat(lb, buf, mx, hmin, hmax, trend_mode=trend)
            trades, curve, summary = simulate_account(df, sim_cfg=sim, strat_cfg=strat)
            ex = sleeve_window_metrics(trades, 10_000.0, ex_start, None)
            rows.append(
                {
                    "lookback": lb,
                    "buffer_bps": buf,
                    "max_breakout_bps": mx,
                    "hold_min": hmin,
                    "hold_max": hmax,
                    "trend_mode": trend,
                    "full_return_pct": summary["return_pct"],
                    "full_pf": summary["profit_factor"],
                    "full_max_dd_pct": summary["max_drawdown_pct"],
                    "full_trades": summary["trades"],
                    "ex_2024_pf": ex["profit_factor"],
                    "ex_2024_pnl": ex["net_pnl"],
                    "ex_2024_trades": ex["trades"],
                }
            )
            if n % 20 == 0:
                print(f"  US500 sweep {n}/{total}…", flush=True)

    rows.sort(
        key=lambda r: (
            float(r["ex_2024_pf"]) if pd.notna(r["ex_2024_pf"]) else -1.0,
            float(r["full_pf"]) if pd.notna(r["full_pf"]) else -1.0,
            float(r["full_return_pct"]),
        ),
        reverse=True,
    )
    return rows


def strat_from_row(row: dict[str, Any]) -> StrategyConfig:
    return us500_strat(
        int(row["lookback"]),
        float(row["buffer_bps"]),
        float(row["max_breakout_bps"]),
        int(row["hold_min"]),
        int(row["hold_max"]),
        trend_mode=str(row["trend_mode"]),
    )


def run_book_with_us500(
    raw_cache: dict[str, pd.DataFrame],
    us500_strat_cfg: StrategyConfig,
) -> dict[str, Any]:
    symbols = tuple(LIVE_SYMBOLS) + (US500,)
    strats = baseline_strategies()
    strats[US500] = us500_strat_cfg
    eq = {s: float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in LIVE_SYMBOLS}
    eq[US500] = float(LIVE_STRATEGY_PARAMS.get(US500, {}).get("equity", LIVE_SLEEVE_EQUITY))
    return run_portfolio(raw_cache, symbols, strats, eq)


def main() -> None:
    print("Loading US500 (Dukascopy)…", flush=True)
    us500_raw = load_us500_raw()
    print(f"  US500 rows={len(us500_raw):,}", flush=True)

    print("Sweeping US500 parameters…", flush=True)
    sweep_rows = sweep_us500(us500_raw)
    top = sweep_rows[:8]

    print("Preloading 6-sleeve book + US500…", flush=True)
    symbols6 = tuple(LIVE_SYMBOLS)
    raw6 = preload_raw(symbols6)
    raw6[US500] = us500_raw

    base_strats = baseline_strategies()
    base_eq = {s: float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in symbols6}
    baseline6 = run_portfolio(raw6, symbols6, base_strats, base_eq)

    curves6: dict[str, pd.DataFrame] = {}
    trade_parts: list[pd.DataFrame] = []
    for sym in symbols6:
        tr, cu, _ = run_sleeve(raw6[sym], sym, base_strats[sym], base_eq[sym])
        curves6[sym] = cu
        if not tr.empty:
            t = tr.copy()
            t["sleeve"] = sym
            trade_parts.append(t)
    all_trades6 = pd.concat(trade_parts, ignore_index=True) if trade_parts else pd.DataFrame()
    baseline_metrics = portfolio_metrics(curves6, all_trades6, sum(base_eq.values()))

    book_tests: list[dict[str, Any]] = []
    for row in top:
        cfg = strat_from_row(row)
        cand = run_book_with_us500(raw6, cfg)
        ok = beats_or_ties_baseline(baseline6, cand) and beats_baseline(baseline_metrics, cand)
        book_tests.append(
            {
                "params": {
                    k: row[k]
                    for k in (
                        "lookback",
                        "buffer_bps",
                        "max_breakout_bps",
                        "hold_min",
                        "hold_max",
                        "trend_mode",
                    )
                },
                "us500_solo": {
                    "full_return_pct": row["full_return_pct"],
                    "full_pf": row["full_pf"],
                    "ex_2024_pf": row["ex_2024_pf"],
                    "ex_2024_pnl": row["ex_2024_pnl"],
                },
                "book_7_metrics": cand,
                "passes_promotion": ok,
            }
        )
        print(
            f"  7-book lb={row['lookback']} buf={row['buffer_bps']:.0f} "
            f"hold={row['hold_min']}-{row['hold_max']} {row['trend_mode']} → "
            f"ret={cand['return_pct']:.1f}% PF={cand['profit_factor']:.2f} "
            f"DD={cand['max_drawdown_pct']:.2f}% pass={ok}",
            flush=True,
        )

    promoted = [t for t in book_tests if t["passes_promotion"]]
    best = promoted[0] if promoted else (book_tests[0] if book_tests else None)

    payload: dict[str, Any] = {
        "candidate": US500,
        "baseline_6_sleeve": baseline6,
        "baseline_6_metrics": baseline_metrics,
        "sweep_top_solo": top[:15],
        "book_tests": book_tests,
        "promoted": best,
        "promotion_count": len(promoted),
        "enable_live": bool(promoted),
        "recommended_params": best["params"] if best else None,
        "note": (
            "Book promotion compares aggregate return on 7×$10k vs 6×$10k baseline. "
            "US500 may be added for paper diversification even when passes_promotion is false."
        ),
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {OUT_PATH}")
    if promoted:
        print("PROMOTION: add US500 to LIVE_SYMBOLS with promoted params.")
        print(json.dumps(best["params"], indent=2))
    else:
        print("No config passed book promotion gate; stub params unchanged.")


if __name__ == "__main__":
    main()
