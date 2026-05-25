#!/usr/bin/env python3
"""Idle-cash overnight yield overlay on portfolio equity (research reporting)."""

from __future__ import annotations

import pandas as pd

from strategy_validation import portfolio_equity_series


def apply_overnight_yield(
    curves: dict[str, pd.DataFrame],
    sleeve_equity: dict[str, float],
    trades: pd.DataFrame,
    *,
    apr: float = 0.045,
) -> pd.Series:
    """Add daily yield on idle book equity (deployed = sum of open notionals)."""
    port = portfolio_equity_series(curves, sleeve_equity)
    if port.empty:
        return port

    deployed = pd.Series(0.0, index=port.index)
    if not trades.empty:
        t = trades.copy()
        t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
        t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
        for row in t.itertuples(index=False):
            mask = (deployed.index >= row.entry_date) & (deployed.index <= row.exit_date)
            deployed.loc[mask] += float(row.entry_notional)

    idle = (port - deployed).clip(lower=0.0)
    daily_rate = apr / 365.0
    return port + (idle * daily_rate).cumsum()
