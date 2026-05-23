#!/usr/bin/env python3
"""
Regime-off mechanism discovery (standalone — no breakout book coupling).

  python3 regime_off_mr/validation.py

Reads Dukascopy cache from btc_breakout_clean/cache/ (read-only).
Writes: regime_off_mr/validation_results.json
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
sys.path.insert(0, str(HERE.parent))

from regime_off_mr.config import (  # noqa: E402
    EX_2024,
    MECHANISMS,
    RESEARCH_SYMBOLS,
    MechanismId,
    SleeveParams,
    default_sleeve,
)
from regime_off_mr.data import load_daily  # noqa: E402
from regime_off_mr.metrics import beats_baseline, passes_discovery, window_metrics  # noqa: E402
from regime_off_mr.sim import simulate  # noqa: E402

OUT_PATH = HERE / "validation_results.json"


def param_grid(mechanism: MechanismId) -> list[SleeveParams]:
    grids: list[SleeveParams] = []
    for sym in RESEARCH_SYMBOLS:
        base = default_sleeve(sym, mechanism)
        if mechanism == "M0_bear_breakout":
            for lb, buf, hold in product([15, 30], [75.0, 100.0], [5, 7, 9]):
                grids.append(
                    replace(base, lookback=lb, buffer_bps=buf, hold_days=hold)
                )
        elif mechanism == "M1_stretch_mr":
            for stretch, hold in product([100.0, 150.0, 200.0, 250.0], [5, 6, 7, 8]):
                grids.append(replace(base, stretch_min_bps=stretch, hold_days=hold))
        else:
            for lb, tag, hold in product([15, 30], [50.0, 75.0, 100.0], [4, 5, 6, 7]):
                grids.append(replace(base, lookback=lb, tag_bps=tag, hold_days=hold))
    return grids


def run_variant(raw: dict[str, pd.DataFrame], params: SleeveParams) -> dict[str, Any]:
    trades, summary = simulate(raw[params.symbol], params)
    ex = window_metrics(trades, EX_2024, None)
    full_m = {
        "return_pct": summary["return_pct"],
        "profit_factor": summary["profit_factor"],
        "max_drawdown_pct": summary["max_drawdown_pct"],
        "trades": summary["trades"],
    }
    return {
        "symbol": params.symbol,
        "mechanism": params.mechanism,
        "label": params.label(),
        "summary": summary,
        "full": full_m,
        "ex_2024": ex,
        "passes_discovery": passes_discovery({**summary, **full_m}, ex, trades),
    }


def best_per_symbol_symbol_mechanism(
    rows: list[dict[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for sym in RESEARCH_SYMBOLS:
        out[sym] = {}
        for mech in MECHANISMS:
            candidates = [r for r in rows if r["symbol"] == sym and r["mechanism"] == mech]
            if not candidates:
                continue
            passing = [c for c in candidates if c["passes_discovery"]]
            pool = passing if passing else candidates
            best = max(
                pool,
                key=lambda r: (
                    float(r["ex_2024"].get("profit_factor") or 0),
                    float(r["full"].get("profit_factor") or 0),
                    r["full"]["return_pct"],
                ),
            )
            out[sym][mech] = best
    return out


def main() -> None:
    print(f"Loading {RESEARCH_SYMBOLS} from Dukascopy cache…", flush=True)
    raw: dict[str, pd.DataFrame] = {}
    for sym in RESEARCH_SYMBOLS:
        raw[sym] = load_daily(sym)
        print(f"  {sym}: {len(raw[sym])} daily bars", flush=True)

    rows: list[dict[str, Any]] = []
    for mech in MECHANISMS:
        grid = param_grid(mech)
        print(f"\n{mech}: {len(grid)} variants…", flush=True)
        for i, params in enumerate(grid):
            row = run_variant(raw, params)
            rows.append(row)
            if (i + 1) % 12 == 0 or i + 1 == len(grid):
                print(f"  {i + 1}/{len(grid)}", flush=True)

    best = best_per_symbol_symbol_mechanism(rows)

    # Per-symbol baseline = best M1 (or best passing any mech)
    baselines: dict[str, dict[str, Any]] = {}
    for sym in RESEARCH_SYMBOLS:
        m1 = best.get(sym, {}).get("M1_stretch_mr")
        if m1:
            baselines[sym] = m1["full"]
        else:
            any_pass = [best[sym][m] for m in best.get(sym, {}) if best[sym][m].get("passes_discovery")]
            baselines[sym] = any_pass[0]["full"] if any_pass else {"return_pct": 0, "profit_factor": 0, "max_drawdown_pct": 0}

    discoveries: list[str] = []
    for sym in RESEARCH_SYMBOLS:
        for mech in MECHANISMS:
            b = best.get(sym, {}).get(mech)
            if not b:
                continue
            tag = "✓ DISCOVERY" if b["passes_discovery"] else "—"
            discoveries.append(
                f"{tag} {sym} {mech}: ret={b['full']['return_pct']:.1f}% "
                f"PF={b['full']['profit_factor']:.2f} exPF={b['ex_2024'].get('profit_factor', float('nan')):.2f} "
                f"trades={b['full']['trades']} DD={b['full']['max_drawdown_pct']:.1f}% | {b['label']}"
            )
            if baselines.get(sym) and mech != "M1_stretch_mr":
                b["beats_M1_baseline"] = beats_baseline(baselines[sym], b["full"])

    n_pass = sum(1 for r in rows if r["passes_discovery"])
    results = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "symbols": list(RESEARCH_SYMBOLS),
        "mechanisms": list(MECHANISMS),
        "gates": {
            "min_pf_full": 1.15,
            "min_pf_ex_2024": 1.0,
            "min_trades_full": 15,
            "min_trades_ex_2024": 3,
            "max_dd_pct": -15.0,
        },
        "variants_run": len(rows),
        "variants_passing_discovery": n_pass,
        "best_by_symbol_mechanism": best,
        "discovery_lines": discoveries,
        "all_variants": rows,
    }
    OUT_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    print("\n=== REGIME-OFF DISCOVERY (solo) ===")
    for line in discoveries:
        print(f"  {line}")
    print(f"\nPassing discovery gate: {n_pass}/{len(rows)}")
    print(f"Full report: {OUT_PATH}")


if __name__ == "__main__":
    main()
