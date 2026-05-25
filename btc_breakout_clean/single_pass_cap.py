#!/usr/bin/env python3
"""
Portfolio concurrent-cap variants (research).

  - fcfs: first-come by entry_date (stable tie-break on sleeve name)
  - breakout_priority: same day, higher breakout_bps enters first
"""

from __future__ import annotations

import pandas as pd

from strategy_validation import (
    blocked_entries_max_concurrent,
    merge_sim_overrides,
    run_full_book,
)


def blocked_entries_cap_priority(
    all_trades: pd.DataFrame,
    max_concurrent: int,
    symbols: tuple[str, ...],
    *,
    priority: str = "fcfs",
) -> dict[str, frozenset[pd.Timestamp]]:
    syms = symbols
    blocked_by_sym: dict[str, set[pd.Timestamp]] = {s: set() for s in syms}
    if all_trades.empty or max_concurrent <= 0:
        return {s: frozenset() for s in syms}

    t = all_trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True).dt.normalize()
    t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True).dt.normalize()
    sym_col = "sleeve" if "sleeve" in t.columns else "symbol"
    if priority == "breakout" and "breakout_bps" in t.columns:
        t["breakout_bps"] = pd.to_numeric(t["breakout_bps"], errors="coerce").fillna(0.0)
        t = t.sort_values(["entry_date", "breakout_bps"], ascending=[True, False])
    else:
        t = t.sort_values("entry_date")

    active: list[tuple[pd.Timestamp, str]] = []
    for row in t.itertuples(index=False):
        entry = row.entry_date
        exit_ = row.exit_date
        sym = str(getattr(row, sym_col))
        active = [(ex, s) for ex, s in active if ex > entry]
        if len(active) >= max_concurrent:
            blocked_by_sym.setdefault(sym, set()).add(entry)
        else:
            active.append((exit_, sym))
    return {sym: frozenset(blocked_by_sym.get(sym, set())) for sym in syms}


def fcfs_matches_two_pass(
    all_trades: pd.DataFrame,
    max_concurrent: int,
    symbols: tuple[str, ...],
) -> bool:
    """Regression: priority FCFS should match legacy two-pass block sets."""
    a = blocked_entries_max_concurrent(all_trades, max_concurrent, symbols)
    b = blocked_entries_cap_priority(all_trades, max_concurrent, symbols, priority="fcfs")
    return a == b


def sim_overrides_for_cap_priority(
    raw_cache: dict,
    symbols: tuple[str, ...],
    strategies: dict,
    max_concurrent: int,
    *,
    priority: str = "fcfs",
    equities_by_symbol: dict[str, float] | None = None,
) -> dict[str, dict]:
    if max_concurrent <= 0 or len(symbols) <= 1:
        return {}
    _, _, base_trades, _ = run_full_book(
        raw_cache, symbols, strategies, equities_by_symbol=equities_by_symbol
    )
    blocked = blocked_entries_cap_priority(
        base_trades, max_concurrent, symbols, priority=priority
    )
    return {sym: {"blocked_entry_dates": blocked.get(sym, frozenset())} for sym in symbols}


def run_full_book_live_cap(
    raw_cache: dict,
    symbols: tuple[str, ...],
    strategies: dict,
    sim_overrides_by_symbol: dict[str, dict] | None = None,
    *,
    max_concurrent: int,
    cap_priority: str = "fcfs",
    equities_by_symbol: dict[str, float] | None = None,
):
    overrides = merge_sim_overrides(
        sim_overrides_by_symbol,
        sim_overrides_for_cap_priority(
            raw_cache,
            symbols,
            strategies,
            max_concurrent,
            priority=cap_priority,
            equities_by_symbol=equities_by_symbol,
        ),
        symbols,
    )
    return run_full_book(
        raw_cache, symbols, strategies, overrides, equities_by_symbol=equities_by_symbol
    )
