#!/usr/bin/env python3
"""
Next-batch experiments (research only; live baseline unchanged).

  - BTC bars: Binance BTCUSDT vs Dukascopy BTCUSD
  - gap_skip post-BTC migration
  - Tiered sizing by breakout strength (1.0-1.5x)
  - New sleeves: LTCUSDT, AVAXUSDT (solo + 9-sleeve book @ $100k equal weight)
  - Gate failure reasons (return / pf / dd)

Run: python3 btc_breakout_clean/next_batch_experiments_validation.py
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
    fetch_binance_daily,
    live_strategy_config,
)
from strategy_validation import (  # noqa: E402
    DATA_START,
    beats_baseline,
    portfolio_metrics,
    preload_raw,
    run_full_book_live,
    run_sleeve,
)

OUT_PATH = HERE / "next_batch_experiments_validation_results.json"
BOOK_TOTAL = 100_000.0
NEW_SLEEVES = ("LTCUSDT", "AVAXUSDT")


def gate_failure_reasons(base: dict[str, float], cand: dict[str, float]) -> list[str]:
    reasons: list[str] = []
    if cand["return_pct"] < base["return_pct"] - 0.08:
        reasons.append("return")
    if cand["profit_factor"] < base["profit_factor"] - 0.03:
        reasons.append("pf")
    if cand["max_drawdown_pct"] < base["max_drawdown_pct"] - 0.12:
        reasons.append("dd")
    return reasons


def gate_bucket(reasons: list[str]) -> str:
    if not reasons:
        return "pass"
    if reasons == ["return"]:
        return "return_only"
    if set(reasons) <= {"return", "pf"}:
        return "return_only"
    if "dd" in reasons:
        return "dd_or_mixed"
    return "other"


def with_btc_binance(raw: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    out = dict(raw)
    out["BTCUSD"] = fetch_binance_daily("BTCUSDT", DATA_START, None)
    return out


def build_strats(
    symbols: tuple[str, ...],
    *,
    per_symbol: dict[str, dict[str, Any]] | None = None,
    all_kw: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    strats: dict[str, Any] = {}
    for s in symbols:
        if extra and s in extra:
            strats[s] = extra[s]
        else:
            strats[s] = live_strategy_config(s)
        if all_kw:
            strats[s] = replace(strats[s], **all_kw)
        if per_symbol and s in per_symbol:
            strats[s] = replace(strats[s], **per_symbol[s])
    return strats


def new_sleeve_config(symbol: str) -> Any:
    if symbol == "LTCUSDT":
        return replace(
            live_strategy_config("BNBUSDT"),
            stop_loss_pct=0.05,
            lookback=15,
            buffer_bps=125.0,
            trend_mode="bull_only",
        )
    if symbol == "AVAXUSDT":
        return replace(
            live_strategy_config("SOLUSDT"),
            stop_loss_pct=0.12,
            lookback=20,
            buffer_bps=75.0,
            trend_mode="sma200_95",
        )
    raise ValueError(symbol)


def equal_equities(symbols: tuple[str, ...]) -> dict[str, float]:
    eq = BOOK_TOTAL / len(symbols)
    return {s: eq for s in symbols}


def run_book(
    raw: dict[str, pd.DataFrame],
    symbols: tuple[str, ...],
    strats: dict[str, Any],
    *,
    label: str,
    category: str,
    note: str = "",
    max_concurrent: int = LIVE_MAX_CONCURRENT_ENTRIES,
) -> dict[str, Any]:
    eq = equal_equities(symbols)
    curves, _, trades, _ = run_full_book_live(
        raw, symbols, strats, max_concurrent=max_concurrent, equities_by_symbol=eq
    )
    initial = sum(eq.values())
    m = portfolio_metrics(curves, trades, initial, initial_equity_by_sleeve=eq)
    return {
        "label": label,
        "category": category,
        "note": note,
        "symbols": list(symbols),
        "book_total_usd": initial,
        "max_concurrent": max_concurrent,
        "metrics": m,
        "trades": int(m.get("trades", 0)),
    }


def run_solo_screen(raw: dict[str, pd.DataFrame], symbol: str, strat: Any) -> dict[str, Any]:
    eq = BOOK_TOTAL / len(LIVE_SYMBOLS)
    tr, cu, summary = run_sleeve(raw[symbol], symbol, strat, eq)
    return {
        "symbol": symbol,
        "trades": int(len(tr)),
        "return_pct": float(summary.get("return_pct", 0.0)),
        "max_dd_pct": float(summary.get("max_drawdown_pct", float("nan"))),
        "profit_factor": float(summary.get("profit_factor", float("nan"))),
        "cagr_pct": float(summary.get("cagr_pct", float("nan"))),
    }


def main() -> None:
    print("Next-batch experiments (live unchanged)", flush=True)
    raw_duk = preload_raw(tuple(LIVE_SYMBOLS))
    raw_bin_btc = with_btc_binance(raw_duk)

    # Preload new sleeves
    raw_ext = dict(raw_duk)
    for sym in NEW_SLEEVES:
        raw_ext[sym] = fetch_binance_daily(sym, DATA_START, None)

    rows: list[dict[str, Any]] = []

    baseline = run_book(
        raw_duk,
        tuple(LIVE_SYMBOLS),
        build_strats(tuple(LIVE_SYMBOLS)),
        label="live_baseline_8",
        category="baseline",
        note="Dukascopy BTC; $12.5k/sleeve implicit via live config",
    )
    # Re-run with explicit equal $100k/8 for fair add-sleeve compare
    baseline_eq = run_book(
        raw_duk,
        tuple(LIVE_SYMBOLS),
        build_strats(tuple(LIVE_SYMBOLS)),
        label="baseline_8_equal_100k",
        category="baseline",
        note="$100k / 8 sleeves (fair compare for 9-sleeve tests)",
    )
    bm = baseline_eq["metrics"]
    rows.extend([baseline, baseline_eq])
    for b in (baseline, baseline_eq):
        b["passes_baseline"] = True
        b["gate_failures"] = []
        b["gate_bucket"] = "baseline"

    print(
        f"BASELINE (8 @ $100k/8) ret={bm['return_pct']:.1f}% DD={bm['max_drawdown_pct']:.2f}% "
        f"PF={bm['profit_factor']:.2f} Sh={bm['sharpe_ratio']:.2f} tr={baseline_eq['trades']}",
        flush=True,
    )

    def add(row: dict[str, Any]) -> None:
        if row["label"].startswith("baseline"):
            row["passes_baseline"] = True
            row["gate_failures"] = []
            row["gate_bucket"] = "baseline"
        else:
            row["gate_failures"] = gate_failure_reasons(bm, row["metrics"])
            row["passes_baseline"] = len(row["gate_failures"]) == 0
            row["gate_bucket"] = gate_bucket(row["gate_failures"])
        rows.append(row)
        m = row["metrics"]
        print(
            f"  {row['label']:42} ret={m['return_pct']:6.1f}% DD={m['max_drawdown_pct']:6.2f}% "
            f"worst={m['worst_sleeve_max_drawdown_pct']:5.1f}% PF={m['profit_factor']:.2f} "
            f"Sh={m['sharpe_ratio']:.2f} tr={row['trades']:3} "
            f"bucket={row['gate_bucket']} fail={row['gate_failures'] or '-'}",
            flush=True,
        )

    print("\n=== BTC Binance migration ===", flush=True)
    add(
        run_book(
            raw_bin_btc,
            tuple(LIVE_SYMBOLS),
            build_strats(tuple(LIVE_SYMBOLS)),
            label="btc_binance_btcusdt_bars",
            category="btc_migration",
            note="BTCUSD sleeve on Binance BTCUSDT daily OHLC",
        )
    )
    add(
        run_book(
            raw_bin_btc,
            tuple(LIVE_SYMBOLS),
            build_strats(tuple(LIVE_SYMBOLS), all_kw={"max_gap_entry_pct": 2.5}),
            label="btc_binance_plus_gap_skip_2p5",
            category="btc_migration",
            note="Binance BTC bars + gap skip 2.5%",
        )
    )

    print("\n=== Tiered sizing ===", flush=True)
    add(
        run_book(
            raw_duk,
            tuple(LIVE_SYMBOLS),
            build_strats(
                tuple(LIVE_SYMBOLS),
                all_kw={"tiered_sizing_by_breakout": True, "tiered_sizing_max_mult": 1.5},
            ),
            label="tiered_sizing_1p5x",
            category="tiered_sizing",
            note="size × clip(breakout_bps/buffer_bps, 1.0, 1.5)",
        )
    )
    add(
        run_book(
            raw_bin_btc,
            tuple(LIVE_SYMBOLS),
            build_strats(
                tuple(LIVE_SYMBOLS),
                all_kw={
                    "tiered_sizing_by_breakout": True,
                    "tiered_sizing_max_mult": 1.5,
                    "max_gap_entry_pct": 2.5,
                },
            ),
            label="btc_binance_tiered_gap_skip",
            category="tiered_sizing",
            note="Binance BTC + tiered 1.5x + gap skip",
        )
    )

    print("\n=== Hygiene / paper (recap) ===", flush=True)
    add(
        run_book(
            raw_duk,
            tuple(LIVE_SYMBOLS),
            build_strats(
                tuple(LIVE_SYMBOLS),
                per_symbol={s: {"max_alloc": 0.50} for s in ("SOLUSDT", "DOGEUSDT")},
            ),
            label="sol_doge_max_alloc_50",
            category="hygiene",
        )
    )
    add(
        run_book(
            raw_duk,
            tuple(LIVE_SYMBOLS),
            build_strats(tuple(LIVE_SYMBOLS), all_kw={"partial_exit_frac": 0.5}),
            label="partial_exit_50_max5",
            category="paper_trial",
            max_concurrent=5,
        )
    )

    print("\n=== New sleeves: solo screen ===", flush=True)
    solo_rows: list[dict[str, Any]] = []
    for sym in NEW_SLEEVES:
        cfg = new_sleeve_config(sym)
        solo = run_solo_screen(raw_ext, sym, cfg)
        solo["strategy"] = sym
        solo_rows.append(solo)
        print(
            f"  SOLO {sym} tr={solo['trades']} ret={solo['return_pct']:.1f}% "
            f"DD={solo['max_dd_pct']:.1f}% PF={solo['profit_factor']:.2f}",
            flush=True,
        )

    print("\n=== New sleeves: 9-sleeve book ($100k equal weight) ===", flush=True)
    for sym in NEW_SLEEVES:
        syms9 = tuple(LIVE_SYMBOLS) + (sym,)
        extra = {sym: new_sleeve_config(sym)}
        add(
            run_book(
                raw_ext,
                syms9,
                build_strats(syms9, extra=extra),
                label=f"book_9_add_{sym.lower()}",
                category="new_sleeve",
                note=f"8 live + {sym}; $100k/{len(syms9)} per sleeve",
            )
        )

    passing = [
        r for r in rows
        if r.get("passes_baseline") and not r["label"].startswith(("baseline", "live_baseline"))
    ]
    by_bucket: dict[str, list[str]] = {}
    for r in rows:
        if r["label"].startswith("baseline"):
            continue
        by_bucket.setdefault(r["gate_bucket"], []).append(r["label"])

    payload = {
        "baseline_for_gate": "baseline_8_equal_100k",
        "baseline_metrics": bm,
        "variants": [r for r in rows if not r["label"].startswith("live_baseline")],
        "solo_new_sleeves": solo_rows,
        "passing_labels": [r["label"] for r in passing],
        "passing_by_bucket": by_bucket,
        "recommendations": {
            "btc_migration": "Compare btc_binance vs baseline; promote only if gate pass or return_only with better Sharpe",
            "gap_skip_after_btc": "btc_binance_plus_gap_skip_2p5",
            "tiered_sizing": "tiered_sizing_1p5x",
            "new_sleeve_solo_best": max(solo_rows, key=lambda x: (x.get("profit_factor") or 0, x.get("return_pct") or 0))["symbol"]
            if solo_rows
            else None,
            "cap_loosen": "rejected_prior_batch",
            "live_unchanged": True,
        },
        "note": "Live btc_breakout_binance_paper_bot.py unchanged.",
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nPassing: {payload['passing_labels'] or ['(none)']}")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
