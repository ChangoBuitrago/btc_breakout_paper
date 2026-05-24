"""Book-level overlays for out-of-box experiment validation."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np
import pandas as pd

from btc_breakout_binance_paper_bot import LIVE_SYMBOLS
from btc_breakout_paper_sim import StrategyConfig, add_indicators


def daily_signal_counts(
    raw: dict[str, pd.DataFrame],
    strategies: dict[str, StrategyConfig],
) -> pd.Series:
    """Count sleeves with signal=True per calendar day (signal bar date)."""
    counts: dict[pd.Timestamp, int] = {}
    for sym, df in raw.items():
        ind = add_indicators(df, strategies[sym])
        for ts, sig in ind["signal"].items():
            if bool(sig):
                d = pd.Timestamp(ts).normalize()
                counts[d] = counts.get(d, 0) + 1
    return pd.Series(counts, dtype=int).sort_index()


def blocked_global_risk_on(
    signal_counts: pd.Series,
    symbols: tuple[str, ...],
    *,
    min_signals: int,
) -> dict[str, frozenset[pd.Timestamp]]:
    """Block next-day entries when prior day had fewer than min_signals across book."""
    blocked: dict[str, set[pd.Timestamp]] = {s: set() for s in symbols}
    if signal_counts.empty or min_signals <= 0:
        return {s: frozenset() for s in symbols}
    days = signal_counts.index.sort_values()
    for i in range(1, len(days)):
        prev_day = days[i - 1]
        entry_day = days[i]
        if int(signal_counts.get(prev_day, 0)) < min_signals:
            for s in symbols:
                blocked[s].add(entry_day)
    return {s: frozenset(blocked[s]) for s in symbols}


def _return_corr_matrix(raw: dict[str, pd.DataFrame], symbols: tuple[str, ...], window: int = 60) -> pd.DataFrame:
    parts: dict[str, pd.Series] = {}
    for s in symbols:
        if s not in raw or raw[s].empty:
            continue
        parts[s] = raw[s]["close"].astype(float).pct_change()
    wide = pd.DataFrame(parts).dropna(how="all")
    if wide.shape[0] < 10:
        return pd.DataFrame(np.eye(len(symbols)), index=list(symbols), columns=list(symbols))
    return wide.tail(window).corr().fillna(0.0)


def blocked_marginal_risk(
    pass1_trades: pd.DataFrame,
    raw: dict[str, pd.DataFrame],
    symbols: tuple[str, ...],
    *,
    max_concurrent: int = 4,
) -> dict[str, frozenset[pd.Timestamp]]:
    """Greedy day-by-day: when >max concurrent, block highest-correlation candidates."""
    blocked: dict[str, set[pd.Timestamp]] = {s: set() for s in symbols}
    if pass1_trades.empty:
        return {s: frozenset() for s in symbols}
    corr = _return_corr_matrix(raw, symbols)
    t = pass1_trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True).dt.normalize()
    t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True).dt.normalize()
    sym_col = "sleeve" if "sleeve" in t.columns else "symbol"
    open_positions: list[tuple[str, pd.Timestamp]] = []
    for entry_day in sorted(t["entry_date"].unique()):
        open_positions = [(s, ex) for s, ex in open_positions if ex > entry_day]
        day_rows = t.loc[t["entry_date"] == entry_day]
        cands = [(str(r[sym_col]), r.exit_date) for _, r in day_rows.iterrows()]
        if len(open_positions) + len(cands) <= max_concurrent:
            open_positions.extend(cands)
            continue
        open_syms = {s for s, _ in open_positions}
        ranked: list[tuple[float, str, pd.Timestamp]] = []
        for sym, ex in cands:
            if not open_syms or sym not in corr.columns:
                ranked.append((0.0, sym, ex))
            else:
                vals = [
                    abs(float(corr.loc[sym, o]))
                    for o in open_syms
                    if o in corr.columns and o != sym
                ]
                ranked.append((max(vals) if vals else 0.0, sym, ex))
        ranked.sort(key=lambda x: x[0])
        allowed = max(0, max_concurrent - len(open_positions))
        for score, sym, ex in ranked[allowed:]:
            blocked.setdefault(sym, set()).add(entry_day)
        for score, sym, ex in ranked[:allowed]:
            open_positions.append((sym, ex))
    return {s: frozenset(blocked[s]) for s in symbols}


def merge_blocked(
    *maps: dict[str, frozenset[pd.Timestamp]],
    symbols: tuple[str, ...],
) -> dict[str, frozenset[pd.Timestamp]]:
    out: dict[str, set[pd.Timestamp]] = {s: set() for s in symbols}
    for m in maps:
        for s in symbols:
            out[s] |= set(m.get(s, frozenset()))
    return {s: frozenset(out[s]) for s in symbols}


def scale_strategies_vol_target(
    strategies: dict[str, StrategyConfig],
    scale: float,
) -> dict[str, StrategyConfig]:
    if scale == 1.0:
        return strategies
    return {s: replace(st, vol_target=st.vol_target * scale) for s, st in strategies.items()}


def filter_symbols_rolling_sharpe(
    solo_metrics: dict[str, float],
    *,
    min_sharpe: float = 0.0,
) -> tuple[str, ...]:
    return tuple(s for s in LIVE_SYMBOLS if solo_metrics.get(s, -999.0) >= min_sharpe)


def opportunity_vol_scale(signal_counts: pd.Series, entry_day: pd.Timestamp, *, low_scale: float = 0.5) -> float:
    """Scale vol when yesterday's book signal count was low."""
    prev = entry_day - pd.Timedelta(days=1)
    cnt = int(signal_counts.get(prev, 0))
    return low_scale if cnt < 2 else 1.0
