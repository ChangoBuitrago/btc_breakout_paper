#!/usr/bin/env python3
"""
Improvement plan experiments vs live baseline (2018+, max 4 unless noted).

Does NOT change live params. Run:
  python3 btc_breakout_clean/improvement_experiments_validation.py
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
    portfolio_metrics,
    preload_raw,
    run_full_book_live,
)

OUT_PATH = HERE / "improvement_experiments_validation_results.json"

CRYPTO_EX_DOGE = ("BTCUSD", "ETHUSDT", "BNBUSDT", "SOLUSDT")
METALS_OIL = ("XAUUSD", "XAGUSD", "BRENT")


def build_strats(
    per_symbol: dict[str, dict[str, Any]] | None = None,
    all_kw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    strats = {s: live_strategy_config(s) for s in LIVE_SYMBOLS}
    if all_kw:
        for s in LIVE_SYMBOLS:
            strats[s] = replace(strats[s], **all_kw)
    if per_symbol:
        for s, kw in per_symbol.items():
            strats[s] = replace(strats[s], **kw)
    return strats


def run_case(
    raw: dict[str, pd.DataFrame],
    *,
    label: str,
    category: str,
    strats: dict[str, Any],
    max_concurrent: int = LIVE_MAX_CONCURRENT_ENTRIES,
    note: str = "",
) -> dict[str, Any]:
    curves, _, trades, equities = run_full_book_live(
        raw, tuple(LIVE_SYMBOLS), strats, max_concurrent=max_concurrent
    )
    initial = sum(equities.values())
    m = portfolio_metrics(curves, trades, initial)
    return {
        "label": label,
        "category": category,
        "note": note,
        "max_concurrent": max_concurrent,
        "metrics": m,
        "trades": int(m.get("trades", 0)),
    }


def cap_per_symbol(caps: dict[str, float | None]) -> dict[str, dict[str, Any]]:
    return {s: {"max_breakout_bps": caps[s]} for s in caps}


def main() -> None:
    print("Improvement experiments vs live baseline (no live changes)", flush=True)
    raw = preload_raw(tuple(LIVE_SYMBOLS))
    print(f"Preloaded {len(raw)} symbols.\n", flush=True)

    rows: list[dict[str, Any]] = []

    # --- Baseline ---
    baseline = run_case(
        raw,
        label="live_baseline",
        category="baseline",
        strats=build_strats(),
        note="Current production params",
    )
    rows.append(baseline)
    bm = baseline["metrics"]
    print(
        f"BASELINE  ret={bm['return_pct']:.1f}%  DD={bm['max_drawdown_pct']:.2f}%  "
        f"worst={bm['worst_sleeve_max_drawdown_pct']:.1f}% ({bm['worst_sleeve']})  "
        f"PF={bm['profit_factor']:.2f}  Sharpe={bm['sharpe_ratio']:.2f}  trades={baseline['trades']}",
        flush=True,
    )

    def add(row: dict[str, Any]) -> None:
        row["passes_baseline"] = beats_baseline(bm, row["metrics"])
        m = row["metrics"]
        rows.append(row)
        print(
            f"  {row['label']:40} ret={m['return_pct']:6.1f}%  DD={m['max_drawdown_pct']:6.2f}%  "
            f"worst={m['worst_sleeve_max_drawdown_pct']:5.1f}%  PF={m['profit_factor']:.2f}  "
            f"Sh={m['sharpe_ratio']:.2f}  tr={row['trades']:3}  pass={row['passes_baseline']}",
            flush=True,
        )

    # --- Group 1: risk hygiene ---
    print("\n=== Group 1: risk hygiene ===", flush=True)
    add(
        run_case(
            raw,
            label="sol_doge_max_alloc_50",
            category="group1_hygiene",
            strats=build_strats(per_symbol={s: {"max_alloc": 0.50} for s in ("SOLUSDT", "DOGEUSDT")}),
            note="SOL/DOGE max_alloc 75% -> 50%",
        )
    )
    add(
        run_case(
            raw,
            label="xag_stop_7pct",
            category="group1_hygiene",
            strats=build_strats(per_symbol={"XAGUSD": {"stop_loss_pct": 0.07}}),
            note="XAG hard stop 7%",
        )
    )
    add(
        run_case(
            raw,
            label="xag_stop_8pct",
            category="group1_hygiene",
            strats=build_strats(per_symbol={"XAGUSD": {"stop_loss_pct": 0.08}}),
            note="XAG hard stop 8%",
        )
    )
    add(
        run_case(
            raw,
            label="hygiene_bundle_alloc50_xag7",
            category="group1_hygiene",
            strats=build_strats(
                per_symbol={
                    **{s: {"max_alloc": 0.50} for s in ("SOLUSDT", "DOGEUSDT")},
                    "XAGUSD": {"stop_loss_pct": 0.07},
                }
            ),
            note="SOL/DOGE alloc 50% + XAG stop 7%",
        )
    )

    # --- Group 2: near-miss / execution ---
    print("\n=== Group 2: gap skip / partial exit ===", flush=True)
    add(
        run_case(
            raw,
            label="gap_skip_2p5",
            category="group2_execution",
            strats=build_strats(all_kw={"max_gap_entry_pct": 2.5}),
            note="Skip entry if overnight gap > 2.5%",
        )
    )
    add(
        run_case(
            raw,
            label="hygiene_alloc50_gap_skip",
            category="group2_execution",
            strats=build_strats(
                per_symbol={s: {"max_alloc": 0.50} for s in ("SOLUSDT", "DOGEUSDT")},
                all_kw={"max_gap_entry_pct": 2.5},
            ),
            note="SOL/DOGE alloc 50% + gap skip 2.5%",
        )
    )
    add(
        run_case(
            raw,
            label="hygiene_full_bundle",
            category="group2_execution",
            strats=build_strats(
                per_symbol={
                    **{s: {"max_alloc": 0.50} for s in ("SOLUSDT", "DOGEUSDT")},
                    "XAGUSD": {"stop_loss_pct": 0.07},
                },
                all_kw={"max_gap_entry_pct": 2.5},
            ),
            note="Group1 bundle + gap skip",
        )
    )
    add(
        run_case(
            raw,
            label="partial_exit_50_max5",
            category="group2_paper",
            strats=build_strats(all_kw={"partial_exit_frac": 0.5}),
            max_concurrent=5,
            note="Paper trial: half exit on fade, max 5 concurrent",
        )
    )

    # --- Group 3: exhaustion cap sweep (per asset class) ---
    print("\n=== Group 3: max_breakout_bps sweep ===", flush=True)
    for cap in (300.0, 350.0, 400.0):
        ps = {s: {"max_breakout_bps": cap} for s in CRYPTO_EX_DOGE}
        add(
            run_case(
                raw,
                label=f"cap_{int(cap)}_crypto_ex_doge",
                category="group3_cap_sweep",
                strats=build_strats(per_symbol=ps),
                note=f"BTC/ETH/BNB/SOL cap {cap} bps; DOGE/metals at 225",
            )
        )
    ps_none = {s: {"max_breakout_bps": None} for s in CRYPTO_EX_DOGE}
    add(
        run_case(
            raw,
            label="cap_none_crypto_ex_doge",
            category="group3_cap_sweep",
            strats=build_strats(per_symbol=ps_none),
            note="No exhaustion cap on BTC/ETH/BNB/SOL",
        )
    )
    add(
        run_case(
            raw,
            label="cap_275_metals_brent",
            category="group3_cap_sweep",
            strats=build_strats(per_symbol={s: {"max_breakout_bps": 275.0} for s in METALS_OIL}),
            note="XAU/XAG/BRENT cap 275 bps",
        )
    )
    add(
        run_case(
            raw,
            label="cap_none_metals_brent",
            category="group3_cap_sweep",
            strats=build_strats(per_symbol={s: {"max_breakout_bps": None} for s in METALS_OIL}),
            note="No cap on metals/oil",
        )
    )

    passing = [r for r in rows if r.get("passes_baseline") and r["label"] != "live_baseline"]
    by_cat: dict[str, list[str]] = {}
    for r in rows:
        if r.get("passes_baseline") and r["label"] != "live_baseline":
            by_cat.setdefault(r["category"], []).append(r["label"])

    best_sharpe = max(
        (r for r in rows if r["label"] != "live_baseline"),
        key=lambda x: x["metrics"]["sharpe_ratio"],
    )
    shallowest = max(rows, key=lambda x: x["metrics"]["max_drawdown_pct"])

    payload = {
        "baseline": baseline,
        "variants": [r for r in rows if r["label"] != "live_baseline"],
        "passing_count": len(passing),
        "passing_labels": [r["label"] for r in passing],
        "passing_by_category": by_cat,
        "best_sharpe_non_baseline": {
            "label": best_sharpe["label"],
            "metrics": best_sharpe["metrics"],
            "passes_baseline": best_sharpe.get("passes_baseline"),
        },
        "shallowest_dd": {
            "label": shallowest["label"],
            "metrics": shallowest["metrics"],
        },
        "recommendations": {
            "unconditional_hygiene": [
                r["label"]
                for r in rows
                if r["category"] == "group1_hygiene"
                and r["metrics"]["worst_sleeve_max_drawdown_pct"] > bm["worst_sleeve_max_drawdown_pct"]
            ],
            "paper_trial": [
                r["label"]
                for r in rows
                if r["category"] == "group2_paper" or r["label"] == "partial_exit_50_max5"
            ],
            "cap_sweep_best_return": max(
                (r for r in rows if r["category"] == "group3_cap_sweep"),
                key=lambda x: x["metrics"]["return_pct"],
            )["label"]
            if any(r["category"] == "group3_cap_sweep" for r in rows)
            else None,
        },
        "note": "Live baseline unchanged; promotion gate = beats_baseline() tolerances.",
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nPassing gate: {len(passing)} — {payload['passing_labels'] or ['(none)']}")
    print(f"Best Sharpe: {best_sharpe['label']} ({best_sharpe['metrics']['sharpe_ratio']:.2f})")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
