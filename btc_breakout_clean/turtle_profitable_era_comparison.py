#!/usr/bin/env python3
"""
Compare Turtle S1 vs Simona breakout on the same markets, 2018–2026 only
(Dukascopy/Binance daily — the period we can replay reliably).

For each scenario we replay:
  1) Turtle System 1 (20/10, 2N stop, 1% risk) — the classic playbook
  2) Simona rules (live params on the 8-sleeve book; era template on classic-only names)

Run from repo root:
  .venv/bin/python btc_breakout_clean/turtle_profitable_era_comparison.py
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import (  # noqa: E402
    LIVE_MAX_CONCURRENT_ENTRIES,
    LIVE_STRATEGY_PARAMS,
    LIVE_SYMBOLS,
    fetch_binance_daily,
    live_strategy_config,
    live_symbol_source,
)
from btc_breakout_paper_sim import (  # noqa: E402
    SimConfig,
    StrategyConfig,
    add_indicators,
    default_skip_saturday_entry,
    dukascopy_cache_path,
    fetch_dukascopy_instrument,
    simulate_account,
)
from strategy_validation import blocked_entries_max_concurrent, portfolio_metrics  # noqa: E402
from turtle_reference_sim import TurtleConfig, add_turtle_indicators, simulate_turtle_account  # noqa: E402

OUT_PATH = HERE / "turtle_profitable_era_comparison_results.json"

SIM_START = "2018-01-01"
SIM_END = "2026-12-31"
DATA_START = "2017-01-01"  # indicator warmup

# Markets commonly cited for the original Turtle program (Dukascopy symbols).
TURTLE_CLASSIC_SYMBOLS: tuple[str, ...] = (
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "USDCAD",
    "AUDUSD",
    "XAUUSD",
    "XAGUSD",
    "XCUUSD",
    "BRENT",
    "GAS",
    "COFFEE",
    "SUGAR",
    "COCOA",
    "SOYBEAN",
    "COTTON",
    "US500",
)

# Asset-class defaults for symbols outside the live 8-sleeve book (trend + dynamic hold).
_ERA_CLASS: dict[str, str] = {
    "EURUSD": "fx",
    "GBPUSD": "fx",
    "USDJPY": "fx",
    "USDCHF": "fx",
    "USDCAD": "fx",
    "AUDUSD": "fx",
    "XAUUSD": "metal",
    "XAGUSD": "metal",
    "XCUUSD": "metal",
    "BRENT": "energy",
    "GAS": "energy",
    "COFFEE": "soft",
    "SUGAR": "soft",
    "COCOA": "soft",
    "SOYBEAN": "soft",
    "COTTON": "soft",
    "US500": "index",
}

_ERA_PARAMS: dict[str, dict[str, Any]] = {
    "fx": {
        "lookback": 20,
        "buffer_bps": 50.0,
        "max_breakout_bps": 400.0,
        "hold_min": 5,
        "hold_max": 20,
        "dynamic_hold": True,
        "hold_giveback_pct": 0.03,
        "trend_mode": "sma200_95",
        "fee_bps": 2.0,
    },
    "metal": {
        "lookback": 30,
        "buffer_bps": 100.0,
        "max_breakout_bps": 400.0,
        "hold_min": 9,
        "hold_max": 15,
        "dynamic_hold": True,
        "hold_giveback_pct": 0.03,
        "trend_mode": "sma200_95",
        "fee_bps": 2.0,
    },
    "energy": {
        "lookback": 30,
        "buffer_bps": 75.0,
        "max_breakout_bps": 400.0,
        "hold_min": 9,
        "hold_max": 15,
        "dynamic_hold": True,
        "hold_giveback_pct": 0.03,
        "trend_mode": "sma200_95",
        "fee_bps": 5.0,
    },
    "soft": {
        "lookback": 30,
        "buffer_bps": 100.0,
        "max_breakout_bps": 400.0,
        "hold_min": 9,
        "hold_max": 15,
        "dynamic_hold": True,
        "hold_giveback_pct": 0.03,
        "trend_mode": "sma200_95",
        "fee_bps": 5.0,
    },
    "index": {
        "lookback": 15,
        "buffer_bps": 100.0,
        "max_breakout_bps": 225.0,
        "hold_min": 9,
        "hold_max": 15,
        "dynamic_hold": True,
        "hold_giveback_pct": 0.03,
        "trend_mode": "sma200_95",
        "fee_bps": 2.0,
    },
}


@dataclass(frozen=True)
class EraScenario:
    id: str
    trader: str
    headline: str
    sim_start: str
    sim_end: str
    data_start: str
    symbols: tuple[str, ...]
    published_source: str
    published_note: str
    published_cagr_pct: float | None = None
    published_return_pct: float | None = None


SCENARIOS: tuple[EraScenario, ...] = (
    EraScenario(
        id="turtle_classic_book",
        trader="Turtle classic futures basket (2018–2026)",
        headline="17-market Turtle-style universe",
        sim_start=SIM_START,
        sim_end=SIM_END,
        data_start=DATA_START,
        symbols=TURTLE_CLASSIC_SYMBOLS,
        published_source="Literature (Parker/Chesapeake anecdotes — not this window)",
        published_note=(
            "Original Turtle riches were 1984–1990s; this replay is the same playbook "
            "on liquid Dukascopy markets where we have 2018+ daily data."
        ),
        published_cagr_pct=None,
    ),
    EraScenario(
        id="simona_live_book",
        trader="Simona live 8-sleeve book (2018–2026)",
        headline="Your production universe",
        sim_start=SIM_START,
        sim_end=SIM_END,
        data_start=DATA_START,
        symbols=tuple(LIVE_SYMBOLS),
        published_source="strategy_validation.py live baseline",
        published_note="Live book replay: per-symbol stops, max 4 concurrent, vol sizing.",
        published_cagr_pct=9.0,
    ),
)


def period_sim_cfg(
    symbol: str,
    equity: float,
    *,
    data_start: str,
    sim_start: str,
    sim_end: str,
    blocked: frozenset[pd.Timestamp] | None = None,
) -> SimConfig:
    sym = symbol.upper()
    src = live_symbol_source(sym) if sym in LIVE_STRATEGY_PARAMS else "dukascopy"
    return SimConfig(
        source=src,
        data_start=data_start,
        sim_start=pd.Timestamp(sim_start, tz="UTC"),
        end=sim_end,
        equity=equity,
        include_current=False,
        cache_path=Path(""),
        dukascopy_path=dukascopy_cache_path(sym) if src == "dukascopy" else Path(""),
        refresh_cache=False,
        show_trades=0,
        write_files=False,
        out_dir=Path("."),
        instrument=sym,
        skip_saturday_entry=default_skip_saturday_entry(src),
        blocked_entry_dates=blocked or frozenset(),
    )


def preload_scenario(
    scenario: EraScenario,
    *,
    refresh_cache: bool = False,
) -> tuple[dict[str, pd.DataFrame], dict[str, Any]]:
    raw: dict[str, pd.DataFrame] = {}
    coverage: dict[str, Any] = {}
    for sym in scenario.symbols:
        src = live_symbol_source(sym) if sym in LIVE_STRATEGY_PARAMS else "dukascopy"
        try:
            if src == "binance":
                df = fetch_binance_daily(sym, scenario.data_start, scenario.sim_end)
            else:
                df = fetch_dukascopy_instrument(
                    sym,
                    dukascopy_cache_path(sym),
                    scenario.data_start,
                    scenario.sim_end,
                    False,
                    refresh_cache,
                )
            raw[sym] = df
            if df.empty:
                coverage[sym] = {"ok": False, "reason": "empty"}
            else:
                in_window = df.loc[
                    (df.index >= pd.Timestamp(scenario.sim_start, tz="UTC"))
                    & (df.index <= pd.Timestamp(scenario.sim_end, tz="UTC"))
                ]
                coverage[sym] = {
                    "ok": len(in_window) >= 252,
                    "bars_total": int(len(df)),
                    "bars_in_sim": int(len(in_window)),
                    "data_first": df.index.min().isoformat(),
                    "data_last": df.index.max().isoformat(),
                }
        except Exception as exc:
            coverage[sym] = {"ok": False, "reason": str(exc)}
    return raw, coverage


def era_strategy_config(symbol: str) -> StrategyConfig:
    sym = symbol.upper()
    if sym in LIVE_STRATEGY_PARAMS:
        return live_strategy_config(sym)
    cls = _ERA_CLASS.get(sym, "metal")
    p = _ERA_PARAMS[cls]
    hold_min = int(p["hold_min"])
    hold_max = int(p["hold_max"])
    return StrategyConfig(
        lookback=int(p["lookback"]),
        buffer_bps=float(p["buffer_bps"]),
        max_breakout_bps=float(p["max_breakout_bps"]),
        trend_mode=str(p["trend_mode"]),
        hold_days=hold_min,
        trail_atr=0.0,
        fee_bps=float(p["fee_bps"]),
        vol_target=0.015,
        max_alloc=0.75,
        compound=True,
        hold_min=hold_min,
        hold_max=hold_max,
        dynamic_hold=bool(p["dynamic_hold"]),
        hold_giveback_pct=float(p["hold_giveback_pct"]),
    )


def run_turtle_sleeve_period(
    raw: pd.DataFrame,
    symbol: str,
    equity: float,
    scenario: EraScenario,
    *,
    blocked: frozenset[pd.Timestamp] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    fee = float(_ERA_PARAMS.get(_ERA_CLASS.get(symbol.upper(), "metal"), _ERA_PARAMS["metal"])["fee_bps"])
    if symbol.upper() in LIVE_STRATEGY_PARAMS:
        fee = float(LIVE_STRATEGY_PARAMS[symbol.upper()].get("fee_bps", fee))
    turtle_cfg = TurtleConfig(fee_bps=fee, compound=True, long_only=True)
    sim = period_sim_cfg(
        symbol,
        equity,
        data_start=scenario.data_start,
        sim_start=scenario.sim_start,
        sim_end=scenario.sim_end,
        blocked=blocked,
    )
    df = add_turtle_indicators(raw, turtle_cfg)
    return simulate_turtle_account(df, sim_cfg=sim, turtle_cfg=turtle_cfg)


def run_simona_sleeve_period(
    raw: pd.DataFrame,
    symbol: str,
    strat: StrategyConfig,
    equity: float,
    scenario: EraScenario,
    *,
    blocked: frozenset[pd.Timestamp] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    sim = period_sim_cfg(
        symbol,
        equity,
        data_start=scenario.data_start,
        sim_start=scenario.sim_start,
        sim_end=scenario.sim_end,
        blocked=blocked,
    )
    df = add_indicators(raw, strat)
    return simulate_account(df, sim_cfg=sim, strat_cfg=strat)


def run_book_period(
    raw: dict[str, pd.DataFrame],
    symbols: tuple[str, ...],
    strats: dict[str, StrategyConfig],
    scenario: EraScenario,
    *,
    turtle_mode: bool,
    max_concurrent: int = LIVE_MAX_CONCURRENT_ENTRIES,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    n = len(symbols)
    equity_each = 100_000.0 / n
    equities = {s: equity_each for s in symbols}

    def _run_once(blocked_map: dict[str, frozenset[pd.Timestamp]] | None) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
        curves: dict[str, pd.DataFrame] = {}
        parts: list[pd.DataFrame] = []
        for sym in symbols:
            if sym not in raw or raw[sym].empty:
                continue
            blocked = (blocked_map or {}).get(sym, frozenset())
            if turtle_mode:
                tr, cu, _ = run_turtle_sleeve_period(raw[sym], sym, equities[sym], scenario, blocked=blocked)
            else:
                tr, cu, _ = run_simona_sleeve_period(
                    raw[sym], sym, strats[sym], equities[sym], scenario, blocked=blocked
                )
            curves[sym] = cu
            if not tr.empty:
                part = tr.copy()
                part["sleeve"] = sym
                parts.append(part)
        trades = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
        return curves, trades

    curves, trades = _run_once(None)
    if max_concurrent > 0 and len(symbols) > 1 and not trades.empty:
        blocked = blocked_entries_max_concurrent(trades, max_concurrent)
        curves, trades = _run_once(blocked)
    return curves, trades


def effective_scenario_window(
    scenario: EraScenario, coverage: dict[str, Any], symbols: tuple[str, ...]
) -> dict[str, Any]:
    ok_syms = [s for s in symbols if coverage.get(s, {}).get("ok")]
    if not ok_syms:
        return {
            "requested": {"sim_start": scenario.sim_start, "sim_end": scenario.sim_end},
            "effective": None,
            "symbols_used": [],
            "symbols_dropped": list(symbols),
        }
    starts, ends = [], []
    for s in ok_syms:
        cov = coverage[s]
        starts.append(pd.Timestamp(cov["data_first"]))
        ends.append(pd.Timestamp(cov["data_last"]))
    eff_start = max(pd.Timestamp(scenario.sim_start, tz="UTC"), max(starts))
    eff_end = min(pd.Timestamp(scenario.sim_end, tz="UTC"), min(ends))
    return {
        "requested": {"sim_start": scenario.sim_start, "sim_end": scenario.sim_end},
        "effective": {
            "sim_start": eff_start.isoformat(),
            "sim_end": eff_end.isoformat(),
        },
        "symbols_used": ok_syms,
        "symbols_dropped": [s for s in symbols if s not in ok_syms],
    }


def run_scenario(
    scenario: EraScenario,
    *,
    refresh_cache: bool = False,
) -> dict[str, Any]:
    print(f"\n=== {scenario.id}: {scenario.trader} ({scenario.sim_start} → {scenario.sim_end}) ===", flush=True)
    raw_all, coverage = preload_scenario(scenario, refresh_cache=refresh_cache)
    window = effective_scenario_window(scenario, coverage, scenario.symbols)
    used = tuple(window["symbols_used"])
    if not used:
        print("  No symbols with sufficient history — skip.", flush=True)
        return {
            "scenario": scenario.__dict__,
            "data_coverage": coverage,
            "window": window,
            "skipped": True,
        }

    raw = {s: raw_all[s] for s in used}
    strats = {s: era_strategy_config(s) for s in used}
    n = len(used)
    initial = 100_000.0
    sleeve_equity = {s: initial / n for s in used}

    print(f"  Symbols: {n}/{len(scenario.symbols)}  (dropped {len(window['symbols_dropped'])})", flush=True)
    print("  Turtle S1 replay ...", flush=True)
    t_curves, t_trades = run_book_period(raw, used, strats, scenario, turtle_mode=True)
    turtle_m = portfolio_metrics(t_curves, t_trades, initial, initial_equity_by_sleeve=sleeve_equity)

    print("  Simona replay ...", flush=True)
    s_curves, s_trades = run_book_period(raw, used, strats, scenario, turtle_mode=False)
    simona_m = portfolio_metrics(s_curves, s_trades, initial, initial_equity_by_sleeve=sleeve_equity)

    delta_ret = simona_m["return_pct"] - turtle_m["return_pct"]
    delta_sharpe = simona_m["sharpe_ratio"] - turtle_m["sharpe_ratio"]

    row = {
        "scenario": scenario.__dict__,
        "data_coverage": coverage,
        "window": window,
        "skipped": False,
        "initial_equity": initial,
        "turtle_s1_replay": turtle_m,
        "simona_replay": simona_m,
        "simona_vs_turtle": {
            "return_pct_delta": delta_ret,
            "cagr_pct_delta": simona_m["cagr_pct"] - turtle_m["cagr_pct"],
            "book_dd_delta": simona_m["max_drawdown_pct"] - turtle_m["max_drawdown_pct"],
            "sharpe_delta": delta_sharpe,
            "simona_wins_return": delta_ret > 0,
            "simona_wins_sharpe": delta_sharpe > 0,
        },
        "published_reference": {
            "source": scenario.published_source,
            "note": scenario.published_note,
            "cagr_pct": scenario.published_cagr_pct,
            "return_pct": scenario.published_return_pct,
        },
    }
    print(
        f"  Turtle replay: CAGR {turtle_m['cagr_pct']:.1f}%  ret {turtle_m['return_pct']:.1f}%  "
        f"DD {turtle_m['max_drawdown_pct']:.2f}%  Sharpe {turtle_m['sharpe_ratio']:.2f}",
        flush=True,
    )
    print(
        f"  Simona replay: CAGR {simona_m['cagr_pct']:.1f}%  ret {simona_m['return_pct']:.1f}%  "
        f"DD {simona_m['max_drawdown_pct']:.2f}%  Sharpe {simona_m['sharpe_ratio']:.2f}",
        flush=True,
    )
    if scenario.published_cagr_pct is not None:
        print(f"  Published anecdote CAGR: ~{scenario.published_cagr_pct:.0f}% (not directly comparable)", flush=True)
    return row


def print_summary_table(results: list[dict[str, Any]]) -> None:
    print(f"\n{'Scenario':<28} {'Period':<12} {'Turtle':>8} {'Simona':>8} {'Δ ret':>8} {'TurtleDD':>9} {'SimDD':>9} {'Pub CAGR':>9}")
    print("-" * 100)
    for r in results:
        if r.get("skipped"):
            continue
        sc = r["scenario"]
        t = r["turtle_s1_replay"]
        s = r["simona_replay"]
        pub = r["published_reference"].get("cagr_pct")
        pub_s = f"~{pub:.0f}%" if pub is not None else "n/a"
        period = f"{sc['sim_start'][:4]}-{sc['sim_end'][:4]}"
        d = r["simona_vs_turtle"]["return_pct_delta"]
        print(
            f"{sc['id']:<28} {period:<12} {t['return_pct']:>7.1f}% {s['return_pct']:>7.1f}% {d:>+7.1f}% "
            f"{t['max_drawdown_pct']:>8.2f}% {s['max_drawdown_pct']:>8.2f}% {pub_s:>9}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--refresh-cache",
        action="store_true",
        help="Re-download Dukascopy H1 caches",
    )
    args = parser.parse_args()
    scenarios = SCENARIOS

    print(
        f"Turtle S1 vs Simona — {SIM_START} → {SIM_END} (same markets per scenario, max 4 concurrent)",
        flush=True,
    )
    print("Parker-era % returns are not comparable to this window (no 1980s data here).\n", flush=True)

    results: list[dict[str, Any]] = []
    for sc in scenarios:
        results.append(run_scenario(sc, refresh_cache=args.refresh_cache))

    print_summary_table(results)
    payload = {
        "period": {"sim_start": SIM_START, "sim_end": SIM_END, "data_start": DATA_START},
        "scenarios": results,
        "turtle_classic_universe": list(TURTLE_CLASSIC_SYMBOLS),
        "live_symbols": list(LIVE_SYMBOLS),
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
