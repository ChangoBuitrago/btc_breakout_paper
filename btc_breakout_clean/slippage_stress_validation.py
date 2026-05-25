#!/usr/bin/env python3
"""
Slippage / friction stress on live baseline (+extra bps per side on top of live fees).

Models worse next-open fills and higher all-in costs. Live unchanged.

Run: python3 btc_breakout_clean/slippage_stress_validation.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

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

OUT_PATH = HERE / "slippage_stress_validation_results.json"

EXTRA_BPS = (0, 5, 10, 20)


def build_strats(extra_bps: float) -> dict[str, Any]:
    strats: dict[str, Any] = {}
    for s in LIVE_SYMBOLS:
        base = live_strategy_config(s)
        strats[s] = replace(base, fee_bps=base.fee_bps + extra_bps)
    return strats


def main() -> None:
    print("Slippage stress (+extra bps per side vs live fees)", flush=True)
    raw = preload_raw(tuple(LIVE_SYMBOLS))
    rows: list[dict[str, Any]] = []

    for extra in EXTRA_BPS:
        label = "live_baseline" if extra == 0 else f"extra_{int(extra)}bps_per_side"
        strats = build_strats(extra)
        curves, _, trades, eq = run_full_book_live(
            raw, tuple(LIVE_SYMBOLS), strats, max_concurrent=LIVE_MAX_CONCURRENT_ENTRIES
        )
        initial = sum(eq.values())
        m = portfolio_metrics(curves, trades, initial)
        row = {
            "label": label,
            "extra_bps_per_side": extra,
            "fee_bps_by_sleeve": {s: strats[s].fee_bps for s in LIVE_SYMBOLS},
            "metrics": m,
            "trades": int(m.get("trades", 0)),
        }
        rows.append(row)
        print(
            f"  +{int(extra):2}bps/side  ret={m['return_pct']:6.1f}% DD={m['max_drawdown_pct']:6.2f}% "
            f"PF={m['profit_factor']:.2f} Sh={m['sharpe_ratio']:.2f} tr={row['trades']}",
            flush=True,
        )

    bm = rows[0]["metrics"]
    for r in rows:
        r["passes_baseline"] = r["label"] == "live_baseline" or beats_baseline(bm, r["metrics"])

    passing = [r["label"] for r in rows if r.get("passes_baseline") and r["label"] != "live_baseline"]
    payload = {
        "baseline_label": "live_baseline",
        "baseline_metrics": bm,
        "note": "Extra bps added to each sleeve's live fee_bps (both entry and exit).",
        "variants": rows,
        "passing_labels": passing,
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nPassing vs baseline: {passing or ['(none)']}")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
