#!/usr/bin/env python3
"""
Focused Algo 1 edge polish — small set of changes, promotion gate vs live book.

  python3 btc_breakout_clean/edge_polish.py

Writes: btc_breakout_clean/edge_polish_results.json
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

from btc_breakout_binance_paper_bot import LIVE_SYMBOLS, live_strategy_config  # noqa: E402
from strategy_validation import (  # noqa: E402
    beats_baseline,
    portfolio_metrics,
    preload_raw,
    run_full_book,
    sleeve_window_metrics,
)

OUT_PATH = HERE / "edge_polish_results.json"
EX_2024 = pd.Timestamp("2024-01-01", tz="UTC")
CRYPTO = frozenset({"BTCUSD", "ETHUSDT", "BNBUSDT", "DOGEUSDT"})


def run_variant(
    raw: dict[str, pd.DataFrame],
    symbols: tuple[str, ...],
    *,
    label: str,
    strategy_overrides: dict[str, dict[str, Any]] | None = None,
    sim_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    from btc_breakout_paper_sim import StrategyConfig

    strats = {s: live_strategy_config(s) for s in symbols}
    for sym, kw in (strategy_overrides or {}).items():
        if sym in strats:
            strats[sym] = replace(strats[sym], **kw)
    curves, _, trades, equities = run_full_book(raw, symbols, strats, sim_overrides)
    initial = sum(equities.values())
    metrics = portfolio_metrics(curves, trades, initial)
    ex = sleeve_window_metrics(trades, initial, EX_2024, None) if not trades.empty else {}
    per_sym_ex: dict[str, Any] = {}
    for sym in symbols:
        sub = trades.loc[trades["sleeve"] == sym] if not trades.empty else trades
        per_sym_ex[sym] = sleeve_window_metrics(sub, equities[sym], EX_2024, None)
    return {
        "label": label,
        "symbols": list(symbols),
        "metrics": metrics,
        "ex_2024": ex,
        "per_sleeve_ex_2024": per_sym_ex,
        "trades": int(metrics.get("trades", 0)),
    }


def ex_pf_ok(ex: dict[str, Any], min_pf: float = 1.05, min_trades: int = 5) -> bool:
    n = int(ex.get("trades") or 0)
    pf = float(ex.get("profit_factor") or 0)
    return n >= min_trades and pf >= min_pf


def promote(base: dict[str, Any], cand: dict[str, Any]) -> dict[str, bool]:
    bm, cm = base["metrics"], cand["metrics"]
    gate = beats_baseline(bm, cm)
    ex_b = base.get("ex_2024", {})
    ex_c = cand.get("ex_2024", {})
    ex_gate = True
    if ex_b and ex_c:
        bpf = float(ex_b.get("profit_factor") or 0)
        cpf = float(ex_c.get("profit_factor") or 0)
        ex_gate = cpf >= bpf - 0.05
    return {"beats_baseline": gate, "ex_2024_not_worse": ex_gate, "promote": gate and ex_gate}


def main() -> None:
    live = tuple(LIVE_SYMBOLS)
    print(f"Edge polish — baseline {len(live)} sleeves: {live}\n", flush=True)
    raw = preload_raw(live)

    variants: list[dict[str, Any]] = []

    def add(label: str, symbols: tuple[str, ...], **kwargs: Any) -> None:
        variants.append(run_variant(raw, symbols, label=label, **kwargs))

    add("baseline", live)

    # Composition
    add("drop_doge", tuple(s for s in live if s != "DOGEUSDT"))

    # BTC ex-2024 was weakest in validation (PF ~1.35)
    add(
        "btc_sma200_95",
        live,
        strategy_overrides={"BTCUSD": {"trend_mode": "sma200_95"}},
    )
    add(
        "btc_tighter_cap_175",
        live,
        strategy_overrides={"BTCUSD": {"max_breakout_bps": 175.0}},
    )
    add(
        "btc_sma200_95_cap_175",
        live,
        strategy_overrides={"BTCUSD": {"trend_mode": "sma200_95", "max_breakout_bps": 175.0}},
    )

    # All crypto exhaustion cap (H4)
    crypto_ov = {s: {"max_breakout_bps": 175.0} for s in CRYPTO if s in live}
    add("crypto_cap_175", live, strategy_overrides=crypto_ov)

    # Momentum regime for crypto
    slope_ov = {s: {"trend_mode": "sma50_slope_up"} for s in CRYPTO if s in live}
    add("crypto_sma50_slope_up", live, strategy_overrides=slope_ov)

    # Risk: pause BTC entries after 12% sleeve DD (live risk policy candidate)
    add(
        "hwm_pause_btc_only",
        live,
        sim_overrides={"BTCUSD": {"hwm_pause_pct": 12.0}},
    )

    # Enforce live max-4 concurrent (already in bot config)
    from strategy_validation import blocked_entries_max_concurrent

    strats = {s: live_strategy_config(s) for s in live}
    _, _, base_trades, _ = run_full_book(raw, live, strats)
    blocked = blocked_entries_max_concurrent(base_trades, 4)
    add(
        "max_4_concurrent",
        live,
        sim_overrides={s: {"blocked_entry_dates": blocked[s]} for s in live},
    )

    baseline = variants[0]
    rows: list[dict[str, Any]] = []
    for v in variants:
        pr = promote(baseline, v)
        row = {**v, **pr}
        rows.append(row)

    rows.sort(
        key=lambda r: (
            not r["promote"],
            -float(r["metrics"]["return_pct"]),
            -float(r["metrics"]["profit_factor"]),
        ),
    )

    OUT_PATH.write_text(
        json.dumps(
            {"generated_at": pd.Timestamp.utcnow().isoformat(), "variants": rows},
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )

    bm = baseline["metrics"]
    print(f"BASELINE: ret={bm['return_pct']:.1f}% PF={bm['profit_factor']:.2f} DD={bm['max_drawdown_pct']:.2f}% trades={bm['trades']}")
    bex = baseline.get("ex_2024", {})
    print(f"  ex-2024: PF={bex.get('profit_factor', float('nan')):.2f} trades={bex.get('trades', 0)}\n")

    print("=== VARIANTS (✓ = passes return/PF/DD + ex-2024 not worse) ===")
    for r in rows:
        m = r["metrics"]
        ex = r.get("ex_2024", {})
        mark = "✓" if r["promote"] else "—"
        print(
            f"{mark} {r['label']}: ret={m['return_pct']:.1f}% PF={m['profit_factor']:.2f} "
            f"DD={m['max_drawdown_pct']:.2f}% | exPF={ex.get('profit_factor', float('nan')):.2f} "
            f"exT={ex.get('trades', 0)}"
        )

    promoted = [r["label"] for r in rows if r["promote"] and r["label"] != "baseline"]
    print(f"\nPromoted: {promoted or '(none — keep baseline)'}")
    print(f"JSON: {OUT_PATH}")


if __name__ == "__main__":
    main()
