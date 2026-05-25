#!/usr/bin/env python3
"""
Architectural playbook experiments (research only — live baseline unchanged).

Runs: synthetic 21:00 UTC bars, TWAP entry slippage, FCFS cap regression,
breakout-priority cap, idle-cash yield overlay.

Run: python3 btc_breakout_clean/architectural_experiments_validation.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import (  # noqa: E402
    LIVE_CRYPTO_SYMBOLS,
    LIVE_MAX_CONCURRENT_ENTRIES,
    LIVE_SYMBOLS,
    live_strategy_config,
)
from single_pass_cap import (  # noqa: E402
    fcfs_matches_two_pass,
    run_full_book_live_cap,
)
from strategy_validation import (  # noqa: E402
    beats_baseline,
    book_utilization_summary,
    portfolio_metrics,
    preload_raw,
    run_full_book_live,
)
from synthetic_pipeline import h1_to_institutional_daily  # noqa: E402
from timeframe_validation import load_h1  # noqa: E402
from yield_overlay import apply_overnight_yield  # noqa: E402

OUT_PATH = HERE / "architectural_experiments_validation_results.json"
YIELD_APR = 0.045
TWAP_MIN_BPS = 5.0
TWAP_VOL_MULT = 0.1


def build_synthetic_raw(
    baseline_raw: dict[str, pd.DataFrame],
    *,
    weekend_dampen: float = 1.0,
) -> dict[str, pd.DataFrame]:
    out = dict(baseline_raw)
    for sym in LIVE_CRYPTO_SYMBOLS:
        print(f"  synthetic H1→21:00 UTC: {sym}", flush=True)
        h1 = load_h1(sym)
        out[sym] = h1_to_institutional_daily(h1, weekend_dampen=weekend_dampen)
    return out


def twap_crypto_overrides() -> dict[str, dict[str, float]]:
    return {
        sym: {
            "entry_slippage_min_bps": TWAP_MIN_BPS,
            "entry_slippage_vol_mult": TWAP_VOL_MULT,
        }
        for sym in LIVE_CRYPTO_SYMBOLS
    }


def yield_overlay_metrics(
    curves: dict[str, pd.DataFrame],
    trades: pd.DataFrame,
    initial_total: float,
    sleeve_eq: dict[str, float],
    *,
    apr: float = YIELD_APR,
) -> dict[str, float]:
    port = apply_overnight_yield(curves, sleeve_eq, trades, apr=apr)
    if port.empty:
        return {"return_pct": float("nan"), "yield_uplift_pp": float("nan")}
    ret = 100.0 * (float(port.iloc[-1]) / initial_total - 1.0)
    base_final = sum(
        float(curves[s].iloc[-1]["equity"]) if not curves[s].empty else sleeve_eq[s]
        for s in curves
    )
    uplift = 100.0 * (float(port.iloc[-1]) - base_final) / initial_total
    return {"return_pct": round(ret, 2), "yield_uplift_pp": round(uplift, 2)}


def run_variant(
    name: str,
    raw_cache: dict[str, pd.DataFrame],
    *,
    sim_overrides: dict[str, dict[str, Any]] | None = None,
    cap_priority: str | None = None,
    gate_bucket: str = "promotion",
) -> dict[str, Any]:
    symbols = tuple(LIVE_SYMBOLS)
    strategies = {s: live_strategy_config(s) for s in symbols}
    if cap_priority is None:
        curves, _, trades, equities = run_full_book_live(
            raw_cache, symbols, strategies, sim_overrides
        )
    else:
        curves, _, trades, equities = run_full_book_live_cap(
            raw_cache,
            symbols,
            strategies,
            sim_overrides,
            max_concurrent=LIVE_MAX_CONCURRENT_ENTRIES,
            cap_priority=cap_priority,
        )
    initial_total = sum(equities.values())
    metrics = portfolio_metrics(curves, trades, initial_total, initial_equity_by_sleeve=equities)
    util = book_utilization_summary(curves, trades, equities)
    yld = yield_overlay_metrics(curves, trades, initial_total, equities)
    row: dict[str, Any] = {
        "variant": name,
        "gate_bucket": gate_bucket,
        **{k: round(float(v), 4) if isinstance(v, (float, np.floating)) and np.isfinite(v) else v
           for k, v in metrics.items() if k != "sleeve_max_drawdown_pct"},
        "utilization": util,
        "yield_overlay_4p5_apr": yld,
    }
    return row


def main() -> None:
    print("Architectural experiments (research only; live unchanged)", flush=True)
    print("Preloading baseline OHLC …", flush=True)
    baseline_raw = preload_raw(tuple(LIVE_SYMBOLS))
    symbols = tuple(LIVE_SYMBOLS)
    strategies = {s: live_strategy_config(s) for s in symbols}

    # FCFS regression on pass-1 trades
    _, _, pass1_trades, _ = run_full_book_live(baseline_raw, symbols, strategies)
    fcfs_ok = fcfs_matches_two_pass(
        pass1_trades, LIVE_MAX_CONCURRENT_ENTRIES, symbols
    )
    print(f"FCFS block-set regression vs two-pass: {'PASS' if fcfs_ok else 'FAIL'}", flush=True)

    rows: list[dict[str, Any]] = []
    baseline = run_variant("baseline_live_replay", baseline_raw, gate_bucket="baseline")
    rows.append(baseline)

    print("Building synthetic 21:00 UTC bars (crypto) …", flush=True)
    syn_raw = build_synthetic_raw(baseline_raw, weekend_dampen=1.0)
    rows.append(run_variant("synthetic_2100_utc", syn_raw))

    print("Synthetic + weekend dampen 0.5 …", flush=True)
    syn_damp = build_synthetic_raw(baseline_raw, weekend_dampen=0.5)
    rows.append(run_variant("synthetic_2100_dampen_0p5", syn_damp))

    print("TWAP entry slippage (crypto only) …", flush=True)
    rows.append(
        run_variant(
            "twap_entry_crypto",
            baseline_raw,
            sim_overrides=twap_crypto_overrides(),
        )
    )

    print("Synthetic 21:00 + TWAP crypto …", flush=True)
    rows.append(
        run_variant(
            "synthetic_2100_twap_crypto",
            syn_raw,
            sim_overrides=twap_crypto_overrides(),
        )
    )

    print("Single-pass FCFS cap replay …", flush=True)
    fcfs_row = run_variant(
        "single_pass_fcfs_cap",
        baseline_raw,
        cap_priority="fcfs",
        gate_bucket="regression",
    )
    fcfs_row["matches_baseline_metrics"] = (
        abs(fcfs_row["return_pct"] - baseline["return_pct"]) < 0.01
        and abs(fcfs_row["max_drawdown_pct"] - baseline["max_drawdown_pct"]) < 0.01
        and fcfs_row["trades"] == baseline["trades"]
    )
    rows.append(fcfs_row)

    print("Breakout-priority cap …", flush=True)
    rows.append(
        run_variant(
            "cap_breakout_priority",
            baseline_raw,
            cap_priority="breakout",
        )
    )

    for row in rows:
        if row.get("gate_bucket") == "baseline":
            row["passes_gate"] = True
        elif row.get("gate_bucket") == "regression":
            row["passes_gate"] = bool(row.get("matches_baseline_metrics"))
        else:
            row["passes_gate"] = beats_baseline(baseline, row)

    out = {
        "fcfs_block_regression": fcfs_ok,
        "baseline": baseline,
        "variants": [r for r in rows if r["variant"] != "baseline_live_replay"],
        "notes": [
            "Research hooks only; live defaults unchanged.",
            "Yield overlay is reporting-only (not in passes_gate).",
            f"TWAP: max({TWAP_MIN_BPS} bps, {TWAP_VOL_MULT}×vol20) on crypto entries.",
        ],
    }
    OUT_PATH.write_text(json.dumps(out, indent=2, default=str) + "\n")
    print(f"\nWrote {OUT_PATH}", flush=True)
    print("\nVariant summary:", flush=True)
    for r in rows:
        gate = "PASS" if r.get("passes_gate") else "fail"
        yld = r.get("yield_overlay_4p5_apr", {})
        print(
            f"  {r['variant']:28} ret={r['return_pct']:6.1f}% dd={r['max_drawdown_pct']:6.2f}% "
            f"pf={r['profit_factor']:.2f} sharpe={r.get('sharpe_ratio', float('nan')):.2f} "
            f"trades={r['trades']} gate={gate} yield+={yld.get('yield_uplift_pp', 0):.2f}pp",
            flush=True,
        )


if __name__ == "__main__":
    main()
