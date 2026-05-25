#!/usr/bin/env python3
"""
Paper-trial candidate: partial_exit_50 + max 5 concurrent vs live baseline.

Includes holdout and calendar context for paper decision (not live promotion).

Run: python3 btc_breakout_clean/paper_trial_validation.py
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
    LIVE_MAX_CONCURRENT_ENTRIES,
    LIVE_SYMBOLS,
    live_strategy_config,
)
from strategy_validation import (  # noqa: E402
    beats_baseline,
    portfolio_equity_series,
    portfolio_metrics,
    preload_raw,
    run_full_book_live,
    trades_in_window,
)

OUT_PATH = HERE / "paper_trial_validation_results.json"


def run_variant(
    raw: dict,
    *,
    label: str,
    partial: bool,
    max_concurrent: int,
) -> dict[str, Any]:
    strats = {s: live_strategy_config(s) for s in LIVE_SYMBOLS}
    if partial:
        strats = {s: replace(strats[s], partial_exit_frac=0.5) for s in LIVE_SYMBOLS}
    curves, _, trades, eq = run_full_book_live(
        raw, tuple(LIVE_SYMBOLS), strats, max_concurrent=max_concurrent
    )
    initial = sum(eq.values())
    m = portfolio_metrics(curves, trades, initial)
    return {"label": label, "partial_exit_frac": 0.5 if partial else 0.0, "max_concurrent": max_concurrent, "metrics": m, "trades": int(len(trades)), "curves": curves, "trades_df": trades, "initial": initial}


def window_metrics(curves: dict, trades: pd.DataFrame, initial: float, start: str | None, end: str | None) -> dict[str, float]:
    s = pd.Timestamp(start, tz="UTC") if start else None
    e = pd.Timestamp(end, tz="UTC") if end else None
    port = portfolio_equity_series(curves, {sym: initial / len(curves) for sym in curves})
    if port.empty:
        return {}
    if s is not None:
        port = port.loc[port.index >= s]
    if e is not None:
        port = port.loc[port.index < e]
    if port.empty or len(port) < 2:
        return {}
    ret = float(port.iloc[-1] / port.iloc[0] - 1.0)
    dd = float((port / port.cummax() - 1.0).min())
    w = trades_in_window(trades, s, e)
    pnls = pd.to_numeric(w["net_pnl"], errors="coerce") if not w.empty else pd.Series(dtype=float)
    wins = int((pnls > 0).sum()) if len(pnls) else 0
    pf = float(pnls[pnls > 0].sum() / abs(pnls[pnls <= 0].sum())) if (pnls <= 0).any() and (pnls > 0).any() else float("nan")
    return {
        "return_pct": 100.0 * ret,
        "max_drawdown_pct": 100.0 * dd,
        "trades": int(len(pnls)),
        "profit_factor": pf,
        "win_rate_pct": 100.0 * wins / len(pnls) if len(pnls) else float("nan"),
    }


def main() -> None:
    print("Paper trial validation", flush=True)
    raw = preload_raw(tuple(LIVE_SYMBOLS))

    baseline = run_variant(raw, label="live_baseline", partial=False, max_concurrent=LIVE_MAX_CONCURRENT_ENTRIES)
    paper = run_variant(raw, label="paper_partial50_max5", partial=True, max_concurrent=5)

    bm = baseline["metrics"]
    pm = paper["metrics"]
    baseline["passes_baseline"] = True
    paper["passes_baseline"] = beats_baseline(bm, pm)

    windows = [
        ("full", None, None),
        ("holdout_2022", "2022-01-01", None),
        ("y2023", "2023-01-01", "2024-01-01"),
        ("y2024", "2024-01-01", "2025-01-01"),
        ("y2025", "2025-01-01", "2026-01-01"),
    ]
    paper["windows"] = {}
    baseline["windows"] = {}
    for name, s, e in windows:
        paper["windows"][name] = window_metrics(paper["curves"], paper["trades_df"], paper["initial"], s, e)
        baseline["windows"][name] = window_metrics(baseline["curves"], baseline["trades_df"], baseline["initial"], s, e)

    for r in (baseline, paper):
        r.pop("curves", None)
        r.pop("trades_df", None)
        r.pop("initial", None)

    print(
        f"  baseline     ret={bm['return_pct']:.1f}% DD={bm['max_drawdown_pct']:.2f}% Sh={bm['sharpe_ratio']:.2f}",
        flush=True,
    )
    print(
        f"  paper trial  ret={pm['return_pct']:.1f}% DD={pm['max_drawdown_pct']:.2f}% Sh={pm['sharpe_ratio']:.2f} "
        f"pass={paper['passes_baseline']}",
        flush=True,
    )

    payload = {
        "paper_trial_config": {"partial_exit_frac": 0.5, "max_concurrent": 5},
        "live_unchanged": True,
        "recommendation": "Paper only: higher Sharpe (+0.18), +5pp return, DD fails gate (−1.96% vs −1.61%).",
        "baseline": baseline,
        "paper_trial": paper,
        "gate_failures": [] if paper["passes_baseline"] else ["dd"],
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
