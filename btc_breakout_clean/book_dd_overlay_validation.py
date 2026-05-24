#!/usr/bin/env python3
"""
Book-level DD overlays on top of current live book (per-symbol crypto stops).

Grids: max concurrent entries, global max_alloc, global vol_target scale,
per-sleeve HWM pause.

Run from repo root:
  python3 btc_breakout_clean/book_dd_overlay_validation.py
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
    LIVE_STRATEGY_PARAMS,
    LIVE_SYMBOLS,
    live_strategy_config,
)
from strategy_validation import (  # noqa: E402
    beats_baseline,
    portfolio_metrics,
    preload_raw,
    run_full_book_live,
    sim_overrides_for_max_concurrent,
)

OUT_PATH = HERE / "book_dd_overlay_validation_results.json"


def run_variant(
    raw: dict[str, pd.DataFrame],
    *,
    label: str,
    strategy_kw_all: dict[str, Any] | None = None,
    sim_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    symbols = tuple(LIVE_SYMBOLS)
    strats = {s: live_strategy_config(s) for s in symbols}
    if strategy_kw_all:
        for s in symbols:
            strats[s] = replace(strats[s], **strategy_kw_all)
    curves, _, trades, equities = run_full_book_live(
        raw, symbols, strats, sim_overrides, max_concurrent=LIVE_MAX_CONCURRENT_ENTRIES
    )
    initial = sum(equities.values())
    metrics = portfolio_metrics(curves, trades, initial)
    return {"label": label, "metrics": metrics, "trades": int(metrics.get("trades", 0))}


def run_max_concurrent(raw: dict[str, pd.DataFrame], cap: int) -> dict[str, Any]:
    symbols = tuple(LIVE_SYMBOLS)
    strats = {s: live_strategy_config(s) for s in symbols}
    blocked = sim_overrides_for_max_concurrent(raw, symbols, strats, cap)
    total_blocked = sum(len(blocked[s]["blocked_entry_dates"]) for s in symbols)
    curves, _, trades, equities = run_full_book_live(raw, symbols, strats, max_concurrent=cap)
    initial = sum(equities.values())
    metrics = portfolio_metrics(curves, trades, initial)
    return {
        "label": f"max_{cap}_concurrent",
        "metrics": metrics,
        "trades": int(metrics.get("trades", 0)),
        "blocked_entry_events": total_blocked,
        "max_concurrent": cap,
    }


def dd_delta_vs_base(base_dd: float, cand_dd: float) -> float:
    """Positive = shallower DD (improvement). Both are negative %."""
    return cand_dd - base_dd


def main() -> None:
    symbols = tuple(LIVE_SYMBOLS)
    print(f"Preloading OHLC for {symbols} ...", flush=True)
    raw = preload_raw(symbols)
    print("Preload done.\n", flush=True)

    rows: list[dict[str, Any]] = []

    symbols = tuple(LIVE_SYMBOLS)
    strats = {s: live_strategy_config(s) for s in symbols}
    curves, _, trades, equities = run_full_book_live(raw, symbols, strats)
    initial = sum(equities.values())
    baseline = {
        "label": "live_baseline_stops_max4",
        "metrics": portfolio_metrics(curves, trades, initial),
        "trades": int(len(trades)),
    }
    rows.append(baseline)
    bm = baseline["metrics"]
    print(
        f"BASELINE  ret={bm['return_pct']:.1f}%  PF={bm['profit_factor']:.2f}  "
        f"DD={bm['max_drawdown_pct']:.2f}%  trades={baseline['trades']}",
        flush=True,
    )

    variants: list[dict[str, Any]] = []

    # --- 1) Max concurrent (live = 4; test tighter caps) ---
    for cap in (3, 2):
        variants.append(run_max_concurrent(raw, cap))

    # --- 2) Global sizing caps ---
    for alloc in (0.60, 0.50):
        variants.append(
            run_variant(raw, label=f"max_alloc_{int(alloc * 100)}pct", strategy_kw_all={"max_alloc": alloc})
        )
    for vol_scale in (0.85, 0.75):
        base_vt = live_strategy_config("BTCUSD").vol_target
        variants.append(
            run_variant(
                raw,
                label=f"vol_target_x{vol_scale}",
                strategy_kw_all={"vol_target": base_vt * vol_scale},
            )
        )

    # --- 3) Per-sleeve HWM pause ---
    for hwm in (8.0, 10.0, 12.0, 15.0):
        sim_ov = {s: {"hwm_pause_pct": hwm} for s in symbols}
        curves, _, trades, equities = run_full_book_live(
            raw, symbols, {s: live_strategy_config(s) for s in symbols}, sim_ov
        )
        initial = sum(equities.values())
        metrics = portfolio_metrics(curves, trades, initial)
        variants.append({"label": f"hwm_pause_{int(hwm)}pct_all_sleeves", "metrics": metrics, "trades": int(len(trades))})

    for v in variants:
        m = v["metrics"]
        v["dd_delta_pct"] = dd_delta_vs_base(bm["max_drawdown_pct"], m["max_drawdown_pct"])
        v["passes_baseline"] = beats_baseline(bm, m)
        rows.append(v)
        extra = ""
        if "blocked_entry_events" in v:
            extra = f"  blocked={v['blocked_entry_events']}"
        print(
            f"{v['label']:32} ret={m['return_pct']:6.1f}%  PF={m['profit_factor']:.2f}  "
            f"DD={m['max_drawdown_pct']:6.2f}%  ΔDD={v['dd_delta_pct']:+.2f}pp  "
            f"pass={v['passes_baseline']}{extra}",
            flush=True,
        )

    # Best DD (shallowest) among variants
    best_dd = max(variants, key=lambda x: x["metrics"]["max_drawdown_pct"])
    # Best DD among those passing baseline gate
    passing = [v for v in variants if v["passes_baseline"]]
    best_pass = max(passing, key=lambda x: x["metrics"]["max_drawdown_pct"]) if passing else None

    payload = {
        "baseline": baseline,
        "variants": variants,
        "best_dd_label": best_dd["label"],
        "best_dd_metrics": best_dd["metrics"],
        "best_passing_baseline": best_pass,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nBest DD: {best_dd['label']} ({best_dd['metrics']['max_drawdown_pct']:.2f}%)")
    if best_pass:
        print(f"Best DD passing gate: {best_pass['label']} ({best_pass['metrics']['max_drawdown_pct']:.2f}%)")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
