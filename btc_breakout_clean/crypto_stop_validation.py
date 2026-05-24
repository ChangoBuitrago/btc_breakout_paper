#!/usr/bin/env python3
"""
Grid hard stop-loss % on crypto sleeves (uniform book + per-symbol solo/book).

Run from repo root:
  python3 btc_breakout_clean/crypto_stop_validation.py
  python3 btc_breakout_clean/crypto_stop_validation.py --per-symbol-only
"""

from __future__ import annotations

import argparse
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
    live_symbol_equity,
    live_strategy_config,
)
from btc_breakout_paper_sim import StrategyConfig  # noqa: E402
from portfolio_param_sweep import beats_or_ties_baseline  # noqa: E402
from strategy_validation import (  # noqa: E402
    portfolio_metrics,
    preload_raw,
    run_full_book_live,
    run_sleeve,
)

OUT_PATH = HERE / "crypto_stop_validation_results.json"
STOP_GRID = (0.0, 0.04, 0.05, 0.06, 0.08, 0.10, 0.12)


def strat_with_stop(symbol: str, stop_pct: float) -> StrategyConfig:
    base = live_strategy_config(symbol)
    if symbol.upper() in LIVE_CRYPTO_SYMBOLS:
        return replace(base, stop_loss_pct=stop_pct, stop_use_low=True)
    return base


def strat_with_stop_map(stop_map: dict[str, float]) -> dict[str, StrategyConfig]:
    out: dict[str, StrategyConfig] = {}
    for sym in LIVE_SYMBOLS:
        base = live_strategy_config(sym)
        pct = stop_map.get(sym.upper(), 0.0)
        if sym.upper() in LIVE_CRYPTO_SYMBOLS and pct > 0:
            out[sym] = replace(base, stop_loss_pct=pct, stop_use_low=True)
        else:
            out[sym] = base
    return out


def run_book_stops(
    raw: dict[str, pd.DataFrame],
    stop_map: dict[str, float],
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    strats = strat_with_stop_map(stop_map)
    curves, _, all_trades, _ = run_full_book_live(raw, tuple(LIVE_SYMBOLS), strats)
    return curves, all_trades


def run_book_uniform(
    raw: dict[str, pd.DataFrame],
    stop_pct: float,
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    return run_book_stops(raw, {s: stop_pct for s in LIVE_CRYPTO_SYMBOLS})


def exit_reason_breakdown(trades: pd.DataFrame) -> dict[str, int]:
    if trades.empty or "exit_reason" not in trades.columns:
        return {}
    return {str(k): int(v) for k, v in trades["exit_reason"].value_counts().items()}


def crypto_stop_stats(trades: pd.DataFrame) -> dict[str, Any]:
    if trades.empty:
        return {}
    crypto = trades[trades["symbol"].isin(LIVE_CRYPTO_SYMBOLS)]
    if crypto.empty:
        return {}
    stops = crypto[crypto["exit_reason"] == "stop_loss"]
    return {
        "crypto_trades": int(len(crypto)),
        "crypto_stop_exits": int(len(stops)),
        "crypto_stop_pct_of_trades": round(100.0 * len(stops) / len(crypto), 1),
        "crypto_worst_open_to_exit_pct": round(float(crypto["open_to_exit_pct"].min()), 2),
    }


def solo_stop_metrics(
    raw: pd.DataFrame,
    symbol: str,
    stop_pct: float,
) -> dict[str, Any]:
    equity = live_symbol_equity(symbol, float(LIVE_STRATEGY_PARAMS[symbol]["equity"]))
    strat = strat_with_stop(symbol, stop_pct)
    trades, curve, _ = run_sleeve(raw, symbol, strat, equity)
    m = sleeve_window_metrics(trades, equity)
    worst = float("nan")
    stop_exits = 0
    if not trades.empty:
        worst = float(trades["open_to_exit_pct"].min())
        stop_exits = int((trades["exit_reason"] == "stop_loss").sum())
    return {
        "stop_loss_pct": stop_pct,
        "metrics": m,
        "worst_open_to_exit_pct": worst,
        "stop_exits": stop_exits,
    }


def pick_solo_stop(symbol: str, grid_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick stop for one symbol from solo grid (baseline = stop 0)."""
    baseline = next(r for r in grid_results if r["stop_loss_pct"] == 0.0)
    b_ret = baseline["metrics"]["return_pct_on_sleeve"]
    b_pf = baseline["metrics"]["profit_factor"]
    b_worst = baseline["worst_open_to_exit_pct"]

    candidates = [r for r in grid_results if r["stop_loss_pct"] > 0]
    if not candidates:
        return baseline

    def score(row: dict[str, Any]) -> float:
        pct = row["stop_loss_pct"]
        m = row["metrics"]
        worst = row["worst_open_to_exit_pct"]
        cap = -100.0 * pct - 0.5  # e.g. -5.5% for 5% stop
        capped = worst >= cap if pd.notna(worst) else False
        ret = m["return_pct_on_sleeve"]
        pf = m["profit_factor"]
        # Prefer stops that cap tail; then return; penalize PF collapse
        pf_pen = 0.0 if pd.isna(b_pf) or pd.isna(pf) else max(0.0, (b_pf - 0.15) - pf) * 20.0
        ret_pen = max(0.0, b_ret - ret - 8.0) * 2.0 if b_ret > 0 else 0.0
        tail_bonus = 30.0 if capped else -10.0
        return tail_bonus + ret - pf_pen - ret_pen

    best = max(candidates, key=score)
    best["baseline_worst_pct"] = b_worst
    best["baseline_return_pct"] = b_ret
    best["baseline_pf"] = b_pf
    return best


def grid_per_symbol(raw: dict[str, pd.DataFrame]) -> dict[str, Any]:
    per_sym: dict[str, Any] = {}
    stop_map: dict[str, float] = {}
    for sym in sorted(LIVE_CRYPTO_SYMBOLS):
        rows = [solo_stop_metrics(raw[sym], sym, pct) for pct in STOP_GRID]
        pick = pick_solo_stop(sym, rows)
        chosen = float(pick["stop_loss_pct"])
        stop_map[sym] = chosen
        per_sym[sym] = {
            "chosen_stop_pct": chosen,
            "solo_pick": pick,
            "grid": rows,
        }
        print(
            f"  {sym:10} stop={int(chosen * 100):2d}%  "
            f"solo_ret={pick['metrics']['return_pct_on_sleeve']:6.1f}%  "
            f"PF={pick['metrics']['profit_factor']:.2f}  "
            f"worst={pick['worst_open_to_exit_pct']:+.1f}%  "
            f"(base worst={pick.get('baseline_worst_pct', float('nan')):+.1f}%)",
            flush=True,
        )
    return {"per_symbol": per_sym, "stop_map": stop_map}


def run_uniform_grid(raw: dict[str, pd.DataFrame], initial_total: float) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for stop_pct in STOP_GRID:
        label = f"uniform_{int(stop_pct * 100)}pct" if stop_pct > 0 else "uniform_no_stop"
        curves, trades = run_book_uniform(raw, stop_pct)
        metrics = portfolio_metrics(curves, trades, initial_total)
        results[label] = {
            "stop_loss_pct_crypto": stop_pct,
            "metrics": metrics,
            "exit_reasons": exit_reason_breakdown(trades),
            "crypto": crypto_stop_stats(trades),
        }
        print(
            f"{label:22} ret={metrics['return_pct']:6.1f}% "
            f"PF={metrics['profit_factor']:.2f} DD={metrics['max_drawdown_pct']:.2f}%",
            flush=True,
        )

    baseline = results["uniform_no_stop"]["metrics"]
    best_label = "uniform_no_stop"
    best_score = -1e9
    for label, row in results.items():
        if label == "uniform_no_stop":
            continue
        m = row["metrics"]
        passes = beats_or_ties_baseline(baseline, m)
        row["passes_baseline"] = passes
        score = m["max_drawdown_pct"] if passes else m["return_pct"] - 50.0
        if score > best_score:
            best_score = score
            best_label = label
    results["_uniform_recommended"] = best_label
    return results


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--per-symbol-only", action="store_true")
    args = p.parse_args()

    raw = preload_raw(tuple(LIVE_SYMBOLS))
    initial_total = sum(float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in LIVE_SYMBOLS)

    payload: dict[str, Any] = {"stop_grid": list(STOP_GRID), "crypto_symbols": sorted(LIVE_CRYPTO_SYMBOLS)}

    if not args.per_symbol_only:
        print("Uniform crypto stop (full book):", flush=True)
        payload["uniform"] = run_uniform_grid(raw, initial_total)
        payload["uniform_recommended"] = payload["uniform"].get("_uniform_recommended")

    print("\nPer-symbol solo grid:", flush=True)
    per = grid_per_symbol(raw)
    stop_map = per["stop_map"]
    print("\nPer-symbol stops full book:", flush=True)
    curves, trades = run_book_stops(raw, stop_map)
    book_metrics = portfolio_metrics(curves, trades, initial_total)
    print(
        f"  per_symbol_book      ret={book_metrics['return_pct']:6.1f}% "
        f"PF={book_metrics['profit_factor']:.2f} DD={book_metrics['max_drawdown_pct']:.2f}%",
        flush=True,
    )
    payload["per_symbol"] = per["per_symbol"]
    payload["per_symbol_stop_map"] = stop_map
    payload["per_symbol_book"] = {
        "metrics": book_metrics,
        "exit_reasons": exit_reason_breakdown(trades),
        "crypto": crypto_stop_stats(trades),
    }

    # Compare to uniform 5% if we ran uniform grid
    uniform_5 = None
    if "uniform" in payload:
        uniform_5 = payload["uniform"].get("uniform_5pct", {}).get("metrics")
    payload["recommended_mode"] = "per_symbol"
    payload["recommended_stop_map"] = stop_map

    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nPer-symbol map: {stop_map}")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
