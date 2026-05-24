#!/usr/bin/env python3
"""
BTC-only risk grid: equity, vol_target scale, HWM pause — other sleeves fixed at live.

  python3 btc_breakout_clean/btc_risk_grid.py

Writes: btc_breakout_clean/btc_risk_grid_results.json
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

from btc_breakout_binance_paper_bot import LIVE_SYMBOLS, live_strategy_config, live_symbol_equity  # noqa: E402
from btc_breakout_paper_sim import max_drawdown, profit_factor  # noqa: E402
from strategy_validation import (  # noqa: E402
    preload_raw,
    run_full_book_live,
    run_sleeve,
    sleeve_window_metrics,
)

OUT_PATH = HERE / "btc_risk_grid_results.json"
BTC = "BTCUSD"
RET_FLOOR_PP = 0.5  # book return must be >= baseline - this many percentage points


def btc_sleeve_metrics(
    raw: dict[str, pd.DataFrame],
    *,
    equity: float,
    vol_scale: float,
    hwm_pct: float | None,
) -> dict[str, Any]:
    strat = live_strategy_config(BTC)
    if vol_scale != 1.0:
        strat = replace(strat, vol_target=strat.vol_target * vol_scale)
    sim_ov: dict[str, Any] = {}
    if hwm_pct is not None and hwm_pct > 0:
        sim_ov["hwm_pause_pct"] = float(hwm_pct)
    tr, cu, summary = run_sleeve(raw[BTC], BTC, strat, equity, sim_ov or None)
    if cu.empty:
        dd_pct = float("nan")
    else:
        eq = cu.set_index(pd.to_datetime(cu["date"], utc=True))["equity"].astype(float)
        dd_pct = 100.0 * max_drawdown(eq)
    ex = sleeve_window_metrics(tr, equity, EX_2024_TS, None) if not tr.empty else {}
    return {
        "trades": int(len(tr)),
        "sleeve_return_pct": float(summary.get("return_pct", float("nan"))),
        "sleeve_max_dd_pct": dd_pct,
        "sleeve_pf": float(summary.get("profit_factor", float("nan"))),
        "ex_2024": ex,
    }


EX_2024_TS = pd.Timestamp("2024-01-01", tz="UTC")


def run_book_fixed(
    raw: dict[str, pd.DataFrame],
    *,
    btc_equity: float,
    btc_vol_scale: float,
    btc_hwm_pct: float | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    strats = {s: live_strategy_config(s) for s in LIVE_SYMBOLS}
    if btc_vol_scale != 1.0:
        strats[BTC] = replace(strats[BTC], vol_target=strats[BTC].vol_target * btc_vol_scale)
    equities = {s: live_symbol_equity(s, 10_000.0) for s in LIVE_SYMBOLS}
    equities[BTC] = btc_equity
    sim_ov: dict[str, dict[str, Any]] = {}
    if btc_hwm_pct is not None and btc_hwm_pct > 0:
        sim_ov[BTC] = {"hwm_pause_pct": float(btc_hwm_pct)}
    curves, _, trades, _ = run_full_book_live(raw, LIVE_SYMBOLS, strats, sim_ov or None)
    initial = sum(equities.values())
    from strategy_validation import portfolio_equity_series

    port = portfolio_equity_series(curves, equities)
    final = float(port.iloc[-1]) if not port.empty else initial
    pnls = pd.to_numeric(trades["net_pnl"], errors="coerce") if not trades.empty else pd.Series(dtype=float)
    book = {
        "return_pct": 100.0 * (final / initial - 1.0),
        "max_drawdown_pct": 100.0 * max_drawdown(port) if not port.empty else float("nan"),
        "profit_factor": float(profit_factor(pnls)) if len(pnls) else float("nan"),
        "trades": int(len(pnls)),
    }
    btc_m = btc_sleeve_metrics(raw, equity=btc_equity, vol_scale=btc_vol_scale, hwm_pct=btc_hwm_pct)
    return book, btc_m


def main() -> None:
    raw = preload_raw(tuple(LIVE_SYMBOLS))
    live_eq = live_symbol_equity(BTC, 10_000.0)

    equities = [3_000.0, 4_000.0, 5_000.0, 7_500.0, 10_000.0]
    vol_scales = [0.75, 0.85, 1.0]
    hwm_levels: list[float | None] = [None, 8.0, 10.0, 12.0, 15.0, 20.0]

    rows: list[dict[str, Any]] = []
    for eq, vol, hwm in product(equities, vol_scales, hwm_levels):
        label = f"btc_eq{int(eq)}_vol{int(vol*100)}_hwm{int(hwm) if hwm else 'off'}"
        book, btc = run_book_fixed(raw, btc_equity=eq, btc_vol_scale=vol, btc_hwm_pct=hwm)
        rows.append(
            {
                "label": label,
                "btc_equity": eq,
                "btc_vol_scale": vol,
                "btc_hwm_pct": hwm,
                "book": book,
                "btc": btc,
            }
        )

    baseline_row = next(
        r
        for r in rows
        if r["btc_equity"] == live_eq and r["btc_vol_scale"] == 1.0 and r["btc_hwm_pct"] is None
    )
    b_ret = baseline_row["book"]["return_pct"]
    b_btc_dd = baseline_row["btc"]["sleeve_max_dd_pct"]
    b_book_dd = baseline_row["book"]["max_drawdown_pct"]
    ret_min = b_ret - RET_FLOOR_PP

    for r in rows:
        book = r["book"]
        btc = r["btc"]
        r["delta_book_ret_pp"] = book["return_pct"] - b_ret
        r["delta_book_dd_pp"] = book["max_drawdown_pct"] - b_book_dd
        r["delta_btc_dd_pp"] = btc["sleeve_max_dd_pct"] - b_btc_dd
        r["meets_ret_floor"] = book["return_pct"] >= ret_min
        r["btc_dd_better"] = btc["sleeve_max_dd_pct"] > b_btc_dd
        r["book_dd_better"] = book["max_drawdown_pct"] > b_book_dd

    qualified = [r for r in rows if r["meets_ret_floor"] and r["btc_dd_better"]]
    qualified.sort(
        key=lambda r: (
            -r["btc"]["sleeve_max_dd_pct"],  # less negative = better (higher)
            -r["book"]["return_pct"],
        ),
        reverse=True,
    )

    OUT_PATH.write_text(
        json.dumps(
            {
                "generated_at": pd.Timestamp.utcnow().isoformat(),
                "baseline": baseline_row,
                "ret_floor_pp": RET_FLOOR_PP,
                "ret_min_pct": ret_min,
                "grid_size": len(rows),
                "qualified_count": len(qualified),
                "top_qualified": qualified[:15],
                "all_variants": rows,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    print(f"BTC risk grid — baseline book ret={b_ret:.2f}% DD={b_book_dd:.2f}%")
    print(f"  BTC sleeve DD={b_btc_dd:.2f}% equity=${live_eq:,.0f}")
    print(f"  Floor: book return >= {ret_min:.2f}% (−{RET_FLOOR_PP}pp)\n")
    print(f"Qualified (ret floor + better BTC DD): {len(qualified)} / {len(rows)}\n")
    print(f"{'label':32} {'book_ret':>8} {'Δret':>6} {'btc_DD':>7} {'ΔbtcDD':>7} {'exPF':>5} {'hwm':>4}")
    for r in qualified[:12]:
        ex_pf = r["btc"]["ex_2024"].get("profit_factor", float("nan"))
        hwm = r["btc_hwm_pct"]
        h = f"{int(hwm)}" if hwm else "off"
        print(
            f"{r['label']:32} {r['book']['return_pct']:8.2f} {r['delta_book_ret_pp']:+6.2f} "
            f"{r['btc']['sleeve_max_dd_pct']:7.2f} {r['delta_btc_dd_pp']:+7.2f} {ex_pf:5.2f} {h:>4}"
        )
    if not qualified:
        print("  (none — showing closest by BTC DD with ret floor)")
        near = sorted(
            [r for r in rows if r["meets_ret_floor"]],
            key=lambda r: r["btc"]["sleeve_max_dd_pct"],
            reverse=True,
        )[:8]
        for r in near:
            print(
                f"  {r['label']:32} ret {r['book']['return_pct']:.2f}% "
                f"btc_DD {r['btc']['sleeve_max_dd_pct']:.2f}% ({r['delta_btc_dd_pp']:+.2f}pp)"
            )
    print(f"\nJSON: {OUT_PATH}")


if __name__ == "__main__":
    main()
