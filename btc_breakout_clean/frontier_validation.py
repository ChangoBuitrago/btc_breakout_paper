#!/usr/bin/env python3
"""
Return / DD / Sharpe frontier: vol_target × max_alloc × max_concurrent,
plus near-miss OOB combos and SOL stop + max-3 sleep package.

Run from repo root:
  python3 btc_breakout_clean/frontier_validation.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from itertools import product
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
    portfolio_metrics,
    preload_raw,
    run_full_book_live,
)

OUT_PATH = HERE / "frontier_validation_results.json"
BASE_VT = 0.015  # live vol_target (1.5%)


def run_case(
    raw: dict[str, pd.DataFrame],
    *,
    label: str,
    vol_target: float | None = None,
    max_alloc: float | None = None,
    max_concurrent: int = LIVE_MAX_CONCURRENT_ENTRIES,
    strategy_kw_all: dict[str, Any] | None = None,
    sol_stop_pct: float | None = None,
) -> dict[str, Any]:
    symbols = tuple(LIVE_SYMBOLS)
    strats = {s: live_strategy_config(s) for s in symbols}
    kw_global: dict[str, Any] = dict(strategy_kw_all or {})
    if vol_target is not None:
        kw_global["vol_target"] = vol_target
    if max_alloc is not None:
        kw_global["max_alloc"] = max_alloc
    if kw_global:
        for s in symbols:
            strats[s] = replace(strats[s], **kw_global)
    if sol_stop_pct is not None and "SOLUSDT" in strats:
        strats["SOLUSDT"] = replace(strats["SOLUSDT"], stop_loss_pct=sol_stop_pct)

    curves, _, trades, equities = run_full_book_live(
        raw, symbols, strats, max_concurrent=max_concurrent
    )
    initial = sum(equities.values())
    m = portfolio_metrics(curves, trades, initial)
    return {
        "label": label,
        "vol_target": vol_target if vol_target is not None else BASE_VT,
        "max_alloc": max_alloc if max_alloc is not None else 0.75,
        "max_concurrent": max_concurrent,
        "sol_stop_pct": sol_stop_pct,
        "strategy_kw": strategy_kw_all or {},
        "metrics": m,
        "trades": int(m.get("trades", 0)),
    }


def main() -> None:
    symbols = tuple(LIVE_SYMBOLS)
    print(f"Preloading OHLC for {symbols} ...", flush=True)
    raw = preload_raw(symbols)
    print("Preload done.\n", flush=True)

    rows: list[dict[str, Any]] = []

    # --- Baseline ---
    baseline = run_case(raw, label="live_baseline")
    rows.append(baseline)
    bm = baseline["metrics"]
    print(
        f"BASELINE  ret={bm['return_pct']:.1f}%  DD={bm['max_drawdown_pct']:.2f}%  "
        f"Sharpe={bm['sharpe_ratio']:.2f}  PF={bm['profit_factor']:.2f}  trades={baseline['trades']}",
        flush=True,
    )

    # --- 1) vol_target × max_alloc × max_concurrent ---
    vol_scales = (0.75, 0.85, 1.0, 1.1, 1.25)
    allocs = (0.50, 0.60, 0.75)
    caps = (2, 3, 4, 5, 6)

    grid_n = len(vol_scales) * len(allocs) * len(caps)
    print(f"\n=== Frontier grid ({grid_n} cases) ===", flush=True)
    for i, (vs, alloc, cap) in enumerate(product(vol_scales, allocs, caps), 1):
        vt = BASE_VT * vs
        label = f"vt{vs:.2f}_alloc{int(alloc * 100)}_max{cap}"
        if vs == 1.0 and alloc == 0.75 and cap == LIVE_MAX_CONCURRENT_ENTRIES:
            continue  # duplicate baseline
        r = run_case(
            raw,
            label=label,
            vol_target=vt,
            max_alloc=alloc,
            max_concurrent=cap,
        )
        r["passes_baseline"] = beats_baseline(bm, r["metrics"])
        rows.append(r)
        m = r["metrics"]
        print(
            f"[{i}/{grid_n}] {label:28} ret={m['return_pct']:6.1f}%  DD={m['max_drawdown_pct']:6.2f}%  "
            f"Sharpe={m['sharpe_ratio']:.2f}  pass={r['passes_baseline']}",
            flush=True,
        )

    # --- 2) Near-miss combos (live vol/alloc unless noted) ---
    combos: list[tuple[str, dict[str, Any]]] = [
        ("partial_exit_50", {"strategy_kw_all": {"partial_exit_frac": 0.5}}),
        ("gap_skip_2p5", {"strategy_kw_all": {"max_gap_entry_pct": 2.5}}),
        (
            "partial_gap_max4",
            {
                "strategy_kw_all": {"partial_exit_frac": 0.5, "max_gap_entry_pct": 2.5},
            },
        ),
        (
            "partial_gap_max3",
            {
                "max_concurrent": 3,
                "strategy_kw_all": {"partial_exit_frac": 0.5, "max_gap_entry_pct": 2.5},
            },
        ),
        ("sol_stop_10pct", {"sol_stop_pct": 0.10}),
        ("sol_stop_8pct", {"sol_stop_pct": 0.08}),
        ("max3_sol10", {"max_concurrent": 3, "sol_stop_pct": 0.10}),
        (
            "sleep_pkg",
            {
                "max_concurrent": 3,
                "sol_stop_pct": 0.10,
                "strategy_kw_all": {"partial_exit_frac": 0.5, "max_gap_entry_pct": 2.5},
            },
        ),
        ("max5_live_sizing", {"max_concurrent": 5}),
        ("max6_live_sizing", {"max_concurrent": 6}),
        (
            "partial_max5",
            {
                "max_concurrent": 5,
                "strategy_kw_all": {"partial_exit_frac": 0.5},
            },
        ),
        (
            "adaptive_lookback_wide",
            {"strategy_kw_all": {"adaptive_lookback_wide": True}},
        ),
    ]

    print(f"\n=== Near-miss combos ({len(combos)} cases) ===", flush=True)
    for label, kw in combos:
        r = run_case(raw, label=label, **kw)
        r["passes_baseline"] = beats_baseline(bm, r["metrics"])
        rows.append(r)
        m = r["metrics"]
        print(
            f"  {label:28} ret={m['return_pct']:6.1f}%  DD={m['max_drawdown_pct']:6.2f}%  "
            f"Sharpe={m['sharpe_ratio']:.2f}  pass={r['passes_baseline']}",
            flush=True,
        )

    # --- Rankings ---
    grid_rows = [r for r in rows if r["label"] != "live_baseline" and "partial" not in r["label"] and "gap" not in r["label"] and "sol" not in r["label"] and "sleep" not in r["label"] and "adaptive" not in r["label"] and "max5" not in r["label"] and "max6" not in r["label"]]
    # grid rows are those with vt in label
    grid_rows = [r for r in rows if r["label"].startswith("vt")]

    by_sharpe = sorted(grid_rows, key=lambda x: x["metrics"]["sharpe_ratio"], reverse=True)[:8]
    by_dd = sorted(grid_rows, key=lambda x: x["metrics"]["max_drawdown_pct"], reverse=True)[:8]
    by_ret = sorted(grid_rows, key=lambda x: x["metrics"]["return_pct"], reverse=True)[:8]
    passing = [r for r in rows if r.get("passes_baseline")]
    best_pass_sharpe = max(passing, key=lambda x: x["metrics"]["sharpe_ratio"]) if passing else None
    best_pass_dd = max(passing, key=lambda x: x["metrics"]["max_drawdown_pct"]) if passing else None

    combo_rows = [r for r in rows if not r["label"].startswith("vt") and r["label"] != "live_baseline"]
    best_combo_sharpe = max(combo_rows, key=lambda x: x["metrics"]["sharpe_ratio"]) if combo_rows else None

    payload = {
        "baseline": baseline,
        "grid_cases": len(grid_rows),
        "combo_cases": len(combo_rows),
        "all_results": rows,
        "top_sharpe_grid": [{"label": r["label"], "metrics": r["metrics"]} for r in by_sharpe],
        "shallowest_dd_grid": [{"label": r["label"], "metrics": r["metrics"]} for r in by_dd],
        "top_return_grid": [{"label": r["label"], "metrics": r["metrics"]} for r in by_ret],
        "passing_count": len(passing),
        "best_passing_sharpe": best_pass_sharpe,
        "best_passing_dd": best_pass_dd,
        "best_combo_sharpe": best_combo_sharpe,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    print("\n=== Top Sharpe (grid) ===", flush=True)
    for r in by_sharpe[:5]:
        m = r["metrics"]
        print(f"  {r['label']:28} Sharpe={m['sharpe_ratio']:.2f}  ret={m['return_pct']:.1f}%  DD={m['max_drawdown_pct']:.2f}%", flush=True)
    print("\n=== Shallowest DD (grid) ===", flush=True)
    for r in by_dd[:5]:
        m = r["metrics"]
        print(f"  {r['label']:28} DD={m['max_drawdown_pct']:.2f}%  ret={m['return_pct']:.1f}%  Sharpe={m['sharpe_ratio']:.2f}", flush=True)
    if best_combo_sharpe:
        m = best_combo_sharpe["metrics"]
        print(
            f"\nBest combo Sharpe: {best_combo_sharpe['label']}  "
            f"Sharpe={m['sharpe_ratio']:.2f}  ret={m['return_pct']:.1f}%  DD={m['max_drawdown_pct']:.2f}%  "
            f"pass={best_combo_sharpe.get('passes_baseline')}",
            flush=True,
        )
    print(f"\nPassing baseline gate: {len(passing)} / {len(rows)}")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
