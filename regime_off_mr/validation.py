#!/usr/bin/env python3
"""
Regime-off mechanism discovery v2 (standalone).

  python3 regime_off_mr/validation.py

Writes: regime_off_mr/validation_results.json
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from itertools import product
from pathlib import Path
from typing import Any

import numpy as np
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
        if mechanism in ("M1_stretch_mr", "M3_stretch_bounce"):
            stretches = [(150, 400), (200, 500), (200, 550), (250, 600), (300, 700)]
            holds = [(5, 8), (6, 10), (7, 12)]
            ret5s = [-4.0, -6.0]
            off_days = [2, 3, 5]
            cooldowns = [3, 5, 7]
            for (smin, smax), (hmin, hmax), r5, off_d, cd in product(
                stretches, holds, ret5s, off_days, cooldowns
            ):
                grids.append(
                    replace(
                        base,
                        stretch_min_bps=smin,
                        stretch_max_bps=smax,
                        hold_days=hmin,
                        hold_max=hmax,
                        min_ret5_pct=r5,
                        min_regime_off_days=off_d,
                        cooldown_days=cd,
                    )
                )
        else:
            for lb, tag, hold, cd in product(
                [20, 30],
                [50.0, 75.0, 100.0],
                [(4, 7), (5, 9)],
                [5, 7, 10],
            ):
                grids.append(
                    replace(
                        base,
                        lookback=lb,
                        tag_bps=tag,
                        hold_days=hold[0],
                        hold_max=hold[1],
                        cooldown_days=cd,
                    )
                )
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


def best_per_symbol_mechanism(rows: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
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
                    float(r["ex_2024"].get("profit_factor") or 0)
                    if np.isfinite(float(r["ex_2024"].get("profit_factor") or float("nan")))
                    else 0.0,
                    float(r["full"].get("profit_factor") or 0),
                    r["full"]["return_pct"],
                    -r["full"]["max_drawdown_pct"],
                ),
            )
            out[sym][mech] = best
    return out


def main() -> None:
    print(f"Loading {RESEARCH_SYMBOLS}…", flush=True)
    raw = {sym: load_daily(sym) for sym in RESEARCH_SYMBOLS}

    rows: list[dict[str, Any]] = []
    for mech in MECHANISMS:
        grid = param_grid(mech)
        print(f"\n{mech}: {len(grid)} variants…", flush=True)
        for i, params in enumerate(grid):
            rows.append(run_variant(raw, params))
            if (i + 1) % 50 == 0 or i + 1 == len(grid):
                print(f"  {i + 1}/{len(grid)}", flush=True)

    best = best_per_symbol_mechanism(rows)
    baselines = {
        sym: (best.get(sym, {}).get("M3_stretch_bounce") or {}).get("full")
        or (best.get(sym, {}).get("M1_stretch_mr") or {}).get("full")
        or {"return_pct": 0, "profit_factor": 0, "max_drawdown_pct": 0}
        for sym in RESEARCH_SYMBOLS
    }

    discoveries: list[str] = []
    for sym in RESEARCH_SYMBOLS:
        for mech in MECHANISMS:
            b = best.get(sym, {}).get(mech)
            if not b:
                continue
            tag = "✓" if b["passes_discovery"] else "—"
            ex_pf = b["ex_2024"].get("profit_factor", float("nan"))
            mix = b["summary"].get("exit_mix", {})
            discoveries.append(
                f"{tag} {sym} {mech}: ret={b['full']['return_pct']:.1f}% "
                f"PF={b['full']['profit_factor']:.2f} exPF={ex_pf:.2f} "
                f"trades={b['full']['trades']} DD={b['full']['max_drawdown_pct']:.1f}% | {b['label']}"
            )
            if mix:
                discoveries.append(f"    exits: {mix}")

    n_pass = sum(1 for r in rows if r["passes_discovery"])
    results = {
        "version": 2,
        "generated_at": pd.Timestamp.now("UTC").isoformat(),
        "symbols": list(RESEARCH_SYMBOLS),
        "mechanisms": list(MECHANISMS),
        "variants_run": len(rows),
        "variants_passing_discovery": n_pass,
        "best_by_symbol_mechanism": best,
        "discovery_lines": discoveries,
    }
    OUT_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")

    print("\n=== REGIME-OFF v2 ===")
    for line in discoveries:
        print(f"  {line}")
    print(f"\nPassing: {n_pass}/{len(rows)} → {OUT_PATH}")


if __name__ == "__main__":
    main()
