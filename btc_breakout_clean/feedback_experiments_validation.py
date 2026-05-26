#!/usr/bin/env python3
"""
Feedback / post-optimization experiment batch (research only — live unchanged).

  A. Regime-only entry null hypothesis
  B. Walk-forward windows (dev / oos_1 / sealed / full)
  C. Two-factor capped sizing vs vol-only vs flat
  D. Regime-break exit
  E. Crypto volume filter
  F. Partial exit at 2×vol20 target
  G. Vol-scaled hold_max

Run: python3 btc_breakout_clean/feedback_experiments_validation.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import (  # noqa: E402
    LIVE_CRYPTO_SYMBOLS,
    LIVE_SYMBOLS,
    live_strategy_config,
)
from strategy_validation import (  # noqa: E402
    beats_baseline,
    portfolio_metrics,
    preload_raw,
    run_full_book_live,
)
from walk_forward_utils import metrics_all_windows  # noqa: E402

OUT_PATH = HERE / "feedback_experiments_validation_results.json"


def _live_strats() -> dict[str, Any]:
    return {s: live_strategy_config(s) for s in LIVE_SYMBOLS}


def _apply_crypto_only(
    strats: dict[str, Any],
    kw: dict[str, Any],
) -> dict[str, Any]:
    out = dict(strats)
    for sym in LIVE_CRYPTO_SYMBOLS:
        if sym in out:
            out[sym] = replace(out[sym], **kw)
    return out


def build_variants() -> dict[str, tuple[str, Callable[[], dict[str, Any]]]]:
    live = _live_strats()

    def regime_continuous() -> dict[str, Any]:
        return {s: replace(live[s], regime_only_entry=True, regime_only_edge=False) for s in LIVE_SYMBOLS}

    def regime_edge() -> dict[str, Any]:
        return {s: replace(live[s], regime_only_entry=True, regime_only_edge=True) for s in LIVE_SYMBOLS}

    def sizing_capped_two_factor() -> dict[str, Any]:
        return {
            s: replace(
                live[s],
                tiered_sizing_by_breakout=True,
                tiered_sizing_min_mult=0.8,
                tiered_sizing_max_mult=1.3,
            )
            for s in LIVE_SYMBOLS
        }

    def sizing_flat() -> dict[str, Any]:
        return {s: replace(live[s], flat_sizing_frac=0.20) for s in LIVE_SYMBOLS}

    def regime_break_exit() -> dict[str, Any]:
        return {s: replace(live[s], regime_break_exit=True) for s in LIVE_SYMBOLS}

    def crypto_volume() -> dict[str, Any]:
        return _apply_crypto_only(live, {"entry_min_volume_ratio": 1.2})

    def partial_vol_target() -> dict[str, Any]:
        return {
            s: replace(live[s], partial_exit_frac=0.5, partial_exit_vol_mult=2.0)
            for s in LIVE_SYMBOLS
        }

    def vol_scaled_hold() -> dict[str, Any]:
        return {s: replace(live[s], vol_scaled_hold_max=True) for s in LIVE_SYMBOLS}

    return {
        "baseline_live": ("Live breakout replay", _live_strats),
        "regime_only_continuous": (
            "A: regime_on entry (no breakout), re-enter while regime holds",
            regime_continuous,
        ),
        "regime_only_edge": (
            "A: regime flip-up entry only",
            regime_edge,
        ),
        "sizing_capped_two_factor": (
            "C: vol sizing × clip(bps/buffer, 0.8, 1.3)",
            sizing_capped_two_factor,
        ),
        "sizing_flat_20pct": ("C: flat 20% size_frac", sizing_flat),
        "regime_break_exit": ("D: exit at BE/loss when regime off", regime_break_exit),
        "crypto_volume_1p2x": ("E: crypto volume > 1.2× 20d median", crypto_volume),
        "partial_exit_2vol": ("F: 50% partial at +2×vol20", partial_vol_target),
        "vol_scaled_hold_max": ("G: hold_max scaled by ref_vol/vol20", vol_scaled_hold),
    }


def run_variant(
    raw: dict,
    name: str,
    note: str,
    strats: dict[str, Any],
) -> dict[str, Any]:
    symbols = tuple(LIVE_SYMBOLS)
    curves, _, trades, equities = run_full_book_live(raw, symbols, strats)
    initial = sum(equities.values())
    full = portfolio_metrics(curves, trades, initial, initial_equity_by_sleeve=equities)
    windows = metrics_all_windows(curves, trades, equities)
    return {
        "variant": name,
        "note": note,
        "full_sample": {k: full[k] for k in ("return_pct", "max_drawdown_pct", "profit_factor", "sharpe_ratio", "trades")},
        "walk_forward": windows,
    }


def gate_vs_baseline(row: dict[str, Any], baseline: dict[str, Any]) -> bool:
    return beats_baseline(baseline["full_sample"], row["full_sample"])


def main() -> None:
    print("Feedback experiments (research only; live unchanged)", flush=True)
    raw = preload_raw(tuple(LIVE_SYMBOLS))
    variants = build_variants()
    rows: list[dict[str, Any]] = []

    for name, (note, builder) in variants.items():
        print(f"  {name} …", flush=True)
        rows.append(run_variant(raw, name, note, builder()))

    baseline = rows[0]
    for row in rows:
        row["passes_gate_full"] = gate_vs_baseline(row, baseline) if row["variant"] != "baseline_live" else True
        oos = row["walk_forward"].get("oos_1", {})
        base_oos = baseline["walk_forward"].get("oos_1", {})
        row["beats_baseline_oos_1"] = (
            row["variant"] == "baseline_live"
            or (
                oos.get("return_pct", -1e9) >= base_oos.get("return_pct", 0) - 0.08
                and oos.get("max_drawdown_pct", -999) >= base_oos.get("max_drawdown_pct", 0) - 0.12
            )
        )

    out = {
        "windows": {
            "dev": "2018-01-01 → 2021-01-01",
            "oos_1": "2021-01-01 → 2023-01-01",
            "sealed": "2023-01-01 → end",
            "full": "2018-01-01 → end",
        },
        "baseline": baseline,
        "variants": [r for r in rows if r["variant"] != "baseline_live"],
        "notes": [
            "Research hooks default off in live bot.",
            "Promotion: prefer oos_1 + sealed over full-sample gate.",
        ],
    }
    OUT_PATH.write_text(json.dumps(out, indent=2, default=str) + "\n")
    print(f"\nWrote {OUT_PATH}\n", flush=True)
    print(f"{'variant':28} {'full ret':>8} {'oos_1':>8} {'sealed':>8} {'gate':>5} {'oos':>5}", flush=True)
    for r in rows:
        fs = r["full_sample"]
        oos = r["walk_forward"]["oos_1"]
        sealed = r["walk_forward"]["sealed"]
        print(
            f"{r['variant']:28} {fs['return_pct']:7.1f}% {oos['return_pct']:7.1f}% "
            f"{sealed['return_pct']:7.1f}% {'PASS' if r['passes_gate_full'] else 'fail':>5} "
            f"{'PASS' if r['beats_baseline_oos_1'] else 'fail':>5}",
            flush=True,
        )


if __name__ == "__main__":
    main()
