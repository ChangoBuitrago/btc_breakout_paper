#!/usr/bin/env python3
"""
Out-of-box experiment grid vs live baseline (2018+, 8 sleeves, max 4 concurrent).

Run:
  .venv/bin/python btc_breakout_clean/oob_experiments_validation.py
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import (  # noqa: E402
    LIVE_CRYPTO_SYMBOLS,
    LIVE_MAX_CONCURRENT_ENTRIES,
    LIVE_STRATEGY_PARAMS,
    LIVE_SYMBOLS,
    live_strategy_config,
    live_symbol_source,
)
from oob_book_overlays import (  # noqa: E402
    blocked_global_risk_on,
    blocked_marginal_risk,
    daily_signal_counts,
    merge_blocked,
    scale_strategies_vol_target,
)
from portfolio_param_sweep import beats_or_ties_baseline  # noqa: E402
from strategy_validation import (  # noqa: E402
    blocked_entries_max_concurrent,
    portfolio_metrics,
    preload_raw,
    run_full_book,
    run_full_book_live,
    sim_overrides_for_max_concurrent,
)

OUT_PATH = HERE / "oob_experiments_validation_results.json"
BINANCE_FAPI = "https://fapi.binance.com"

# strategy_kw applied to all sleeves; book_* for portfolio overlays
MODES: dict[str, dict[str, Any]] = {
    "baseline": {"category": "baseline", "strategy": {}, "note": "Live"},
    # Portfolio brain
    "signal_decay_3d": {"category": "portfolio", "strategy": {"signal_max_pending_days": 3}, "note": "Expire stale pending 3d"},
    "signal_decay_5d": {"category": "portfolio", "strategy": {"signal_max_pending_days": 5}, "note": "Expire stale pending 5d"},
    "global_risk_on_3": {"category": "portfolio", "book": "global_risk", "min_signals": 3, "note": "Need ≥3 sleeves signaled prior day"},
    "global_risk_on_5": {"category": "portfolio", "book": "global_risk", "min_signals": 5, "note": "Need ≥5 sleeves signaled prior day"},
    "marginal_risk_4": {"category": "portfolio", "book": "marginal_risk", "note": "Low-corr priority vs FCFS max-4"},
    "opportunity_vol_85": {"category": "portfolio", "book": "vol_scale", "vol_scale": 0.85, "note": "Book vol_target ×0.85"},
    # Payoff shaping
    "stop_cooldown_5d": {"category": "payoff", "strategy": {"post_stop_cooldown_days": 5}, "note": "No re-entry 5d after stop"},
    "stop_cooldown_10d": {"category": "payoff", "strategy": {"post_stop_cooldown_days": 10}, "note": "No re-entry 10d after stop"},
    "extend_hold_3d": {"category": "payoff", "strategy": {"extend_hold_on_new_highs": 3, "extend_hold_max_extra": 9}, "note": "Extend hold on new highs"},
    "partial_exit_50": {"category": "payoff", "strategy": {"partial_exit_frac": 0.5}, "note": "Half off on momentum fade"},
    # Signal physics
    "gap_skip_1p5": {"category": "signal", "strategy": {"max_gap_entry_pct": 1.5}, "note": "Skip if open gap >1.5%"},
    "gap_skip_2p5": {"category": "signal", "strategy": {"max_gap_entry_pct": 2.5}, "note": "Skip if open gap >2.5%"},
    "vol_adaptive_buffer": {"category": "signal", "strategy": {"vol_buffer_vol_mult": 0.5}, "note": "Wider buffer in high vol"},
    "two_close_confirm": {"category": "signal", "strategy": {"require_two_close_confirm": True}, "note": "Two closes above band"},
    "adaptive_lookback_wide": {"category": "signal", "strategy": {"adaptive_lookback_wide": True}, "note": "Wide lookback when vol elevated"},
    # Meta / microstructure
    "meta_low_vol_70pct": {"category": "meta", "strategy": {"meta_vol20_max_pctile": 0.70}, "note": "Skip entry if vol20 > 70th pct"},
    "funding_skip_3bps": {"category": "meta", "book": "funding", "funding_max": 0.0003, "note": "Skip crypto entry if funding high"},
    "dynamic_drop_xcu": {"category": "meta", "book": "drop_symbols", "drop": ("XCUUSD",), "note": "Drop weak copper sleeve"},
    "dynamic_drop_doge_brent": {"category": "meta", "book": "drop_symbols", "drop": ("DOGEUSDT", "BRENT"), "note": "Drop DOGE+BRENT"},
}


def fetch_funding_daily(symbol: str, start: str) -> pd.Series:
    """Binance USDT-M funding rate → daily max (8h prints)."""
    sym = symbol.replace("USDT", "USDT")
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    rows: list[tuple[pd.Timestamp, float]] = []
    end_ms: int | None = None
    while True:
        params: dict[str, Any] = {"symbol": sym, "limit": 1000}
        if end_ms is not None:
            params["endTime"] = end_ms
        url = f"{BINANCE_FAPI}/fapi/v1/fundingRate?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=20) as resp:
            chunk = json.loads(resp.read().decode())
        if not chunk:
            break
        for row in chunk:
            ts = pd.Timestamp(int(row["fundingTime"]), unit="ms", tz="UTC")
            if ts >= pd.Timestamp(start, tz="UTC"):
                rows.append((ts, float(row["fundingRate"])))
        if len(chunk) < 1000:
            break
        end_ms = int(chunk[0]["fundingTime"]) - 1
        if int(chunk[-1]["fundingTime"]) < start_ms:
            break
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series({t: v for t, v in rows}).sort_index()
    daily = s.resample("D").max()
    return daily


def blocked_high_funding(
    raw: dict[str, pd.DataFrame],
    *,
    max_rate: float,
) -> dict[str, frozenset[pd.Timestamp]]:
    blocked: dict[str, set[pd.Timestamp]] = {s: set() for s in LIVE_SYMBOLS}
    for sym in LIVE_CRYPTO_SYMBOLS:
        if sym not in raw or live_symbol_source(sym) != "binance":
            continue
        try:
            funding = fetch_funding_daily(sym, "2017-01-01")
        except Exception:
            continue
        if funding.empty:
            continue
        bad_days = funding[funding > max_rate].index.normalize().unique()
        for d in bad_days:
            blocked[sym].add(pd.Timestamp(d, tz="UTC"))
    return {s: frozenset(blocked[s]) for s in LIVE_SYMBOLS}


def strats_for_mode(mode: str) -> dict[str, Any]:
    ov = dict(MODES[mode].get("strategy", {}))
    return {s: replace(live_strategy_config(s), **ov) for s in LIVE_SYMBOLS}


def build_overrides(
    raw: dict[str, pd.DataFrame],
    mode: str,
    strats: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    meta = MODES[mode]
    symbols = tuple(s for s in LIVE_SYMBOLS if s not in meta.get("drop", ()))
    cap = sim_overrides_for_max_concurrent(raw, symbols, strats, LIVE_MAX_CONCURRENT_ENTRIES)
    extra: list[dict[str, frozenset[pd.Timestamp]]] = []

    if meta.get("book") == "global_risk":
        counts = daily_signal_counts(raw, strats)
        extra.append(blocked_global_risk_on(counts, symbols, min_signals=int(meta["min_signals"])))
    elif meta.get("book") == "marginal_risk":
        _, _, t1, _ = run_full_book(raw, symbols, strats)
        if not t1.empty:
            extra.append(
                blocked_marginal_risk(t1, raw, symbols, max_concurrent=LIVE_MAX_CONCURRENT_ENTRIES)
            )
    elif meta.get("book") == "funding":
        extra.append(blocked_high_funding(raw, max_rate=float(meta["funding_max"])))

    if not extra:
        return {sym: dict(cap.get(sym, {})) for sym in symbols}
    merged = merge_blocked(cap, *extra, symbols=symbols)
    return {sym: {"blocked_entry_dates": merged[sym]} for sym in symbols}


def run_mode(raw: dict[str, pd.DataFrame], mode: str) -> dict[str, Any]:
    meta = MODES[mode]
    drop = tuple(meta.get("drop", ()))
    symbols = tuple(s for s in LIVE_SYMBOLS if s not in drop)
    strats = strats_for_mode(mode)
    if meta.get("book") == "vol_scale":
        strats = scale_strategies_vol_target(strats, float(meta.get("vol_scale", 1.0)))
    overrides = build_overrides(raw, mode, strats)
    curves, _, trades, _ = run_full_book_live(raw, symbols, strats, overrides)
    initial = sum(float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in symbols)
    return portfolio_metrics(curves, trades, initial, initial_equity_by_sleeve={s: initial / len(symbols) for s in symbols})


def main() -> None:
    print("OOB experiment grid vs live baseline", flush=True)
    raw = preload_raw(tuple(LIVE_SYMBOLS))
    print(f"Loaded {len(raw)} symbols.\n", flush=True)

    results: dict[str, Any] = {}
    for mode in MODES:
        print(f"--- {mode} ---", flush=True)
        try:
            m = run_mode(raw, mode)
            results[mode] = {"ok": True, "note": MODES[mode]["note"], "category": MODES[mode]["category"], "metrics": m}
            print(
                f"  ret={m['return_pct']:6.1f}%  DD={m['max_drawdown_pct']:6.2f}%  "
                f"PF={m['profit_factor']:.2f}  Sharpe={m['sharpe_ratio']:.2f}  trades={m['trades']}",
                flush=True,
            )
        except Exception as exc:
            results[mode] = {"ok": False, "error": str(exc), "note": MODES[mode]["note"]}
            print(f"  FAILED: {exc}", flush=True)

    baseline = results.get("baseline", {}).get("metrics", {})
    passing: list[str] = []
    if baseline:
        for mode, r in results.items():
            if mode == "baseline" or not r.get("ok"):
                continue
            r["passes_baseline"] = beats_or_ties_baseline(baseline, r["metrics"])
            if r.get("passes_baseline"):
                passing.append(mode)

    by_cat: dict[str, list[str]] = {}
    for mode, r in results.items():
        if r.get("passes_baseline"):
            by_cat.setdefault(r.get("category", "?"), []).append(mode)

    payload = {
        "modes": {k: {"note": v["note"], "category": v["category"]} for k, v in MODES.items()},
        "baseline_metrics": baseline,
        "results": results,
        "passing_modes": passing,
        "passing_by_category": by_cat,
        "recommended": passing[0] if len(passing) == 1 else ("baseline" if not passing else max(passing, key=lambda k: results[k]["metrics"]["return_pct"])),
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nPassing ({len(passing)}): {passing or ['(none)']}")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
