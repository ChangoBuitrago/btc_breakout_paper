#!/usr/bin/env python3
"""
Momentum-fade ablation: which exit components matter?

Live fade = giveback from peak + close < SMA50 + SMA50 20-bar slope < 0.

Run: python3 btc_breakout_clean/momentum_fade_ablation_validation.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import LIVE_SYMBOLS, live_strategy_config  # noqa: E402
from strategy_validation import (  # noqa: E402
    beats_baseline,
    portfolio_metrics,
    preload_raw,
    run_full_book_live,
)

OUT_PATH = HERE / "momentum_fade_ablation_validation_results.json"

MODES: dict[str, dict[str, Any]] = {
    "baseline": {
        "momentum_fade_use_giveback": True,
        "momentum_fade_use_sma50": True,
        "momentum_fade_use_sma50_slope": True,
        "note": "Live default (all three fade triggers)",
    },
    "no_sma50_slope": {
        "momentum_fade_use_giveback": True,
        "momentum_fade_use_sma50": True,
        "momentum_fade_use_sma50_slope": False,
        "note": "Drop SMA50 slope < 0 (reviewer ablation)",
    },
    "no_sma50": {
        "momentum_fade_use_giveback": True,
        "momentum_fade_use_sma50": False,
        "momentum_fade_use_sma50_slope": True,
        "note": "Drop close < SMA50",
    },
    "giveback_only": {
        "momentum_fade_use_giveback": True,
        "momentum_fade_use_sma50": False,
        "momentum_fade_use_sma50_slope": False,
        "note": "Peak giveback only",
    },
    "no_giveback": {
        "momentum_fade_use_giveback": False,
        "momentum_fade_use_sma50": True,
        "momentum_fade_use_sma50_slope": True,
        "note": "SMA50 + slope only (no peak giveback)",
    },
}


def run_mode(raw: dict, mode: str) -> dict[str, Any]:
    kw = {k: v for k, v in MODES[mode].items() if k != "note"}
    symbols = tuple(LIVE_SYMBOLS)
    strats = {s: replace(live_strategy_config(s), **kw) for s in symbols}
    curves, _, trades, equities = run_full_book_live(raw, symbols, strats)
    initial = sum(equities.values())
    m = portfolio_metrics(curves, trades, initial)
    sym_col = "sleeve" if "sleeve" in trades.columns else "symbol"
    fade_n = 0
    max_n = 0
    if not trades.empty and "exit_reason" in trades.columns:
        fade_n = int((trades["exit_reason"] == "momentum_fade").sum())
        max_n = int((trades["exit_reason"] == "max_hold").sum())
    return {
        "label": mode,
        "note": MODES[mode]["note"],
        "fade_kw": kw,
        "metrics": m,
        "trades": int(m.get("trades", 0)),
        "momentum_fade_exits": fade_n,
        "max_hold_exits": max_n,
    }


def main() -> None:
    print("Momentum-fade ablation (live unchanged)", flush=True)
    raw = preload_raw(tuple(LIVE_SYMBOLS))
    rows: list[dict[str, Any]] = []
    for mode in MODES:
        row = run_mode(raw, mode)
        rows.append(row)
        m = row["metrics"]
        print(
            f"  {mode:18} ret={m['return_pct']:6.1f}% DD={m['max_drawdown_pct']:6.2f}% "
            f"PF={m['profit_factor']:.2f} Sh={m['sharpe_ratio']:.2f} "
            f"fade={row['momentum_fade_exits']:3} max_hold={row['max_hold_exits']:3}",
            flush=True,
        )

    baseline = rows[0]["metrics"]
    for r in rows:
        r["passes_baseline"] = beats_baseline(baseline, r["metrics"]) if r["label"] != "baseline" else True

    passing = [r["label"] for r in rows if r.get("passes_baseline") and r["label"] != "baseline"]
    payload = {
        "baseline_label": "baseline",
        "baseline_metrics": baseline,
        "modes": rows,
        "passing_labels": passing,
        "recommendation": "Keep live all-three fade unless ablation passes gate with clear benefit.",
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nPassing vs baseline: {passing or ['(none)']}")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
