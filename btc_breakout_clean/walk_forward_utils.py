#!/usr/bin/env python3
"""Walk-forward window metrics for research validation (live unchanged)."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from btc_breakout_paper_sim import max_drawdown, profit_factor
from strategy_validation import (
    annualized_vol_and_sharpe,
    portfolio_equity_series,
    trades_in_window,
)

WF_WINDOWS: dict[str, tuple[pd.Timestamp, pd.Timestamp | None]] = {
    "dev": (pd.Timestamp("2018-01-01", tz="UTC"), pd.Timestamp("2021-01-01", tz="UTC")),
    "oos_1": (pd.Timestamp("2021-01-01", tz="UTC"), pd.Timestamp("2023-01-01", tz="UTC")),
    "sealed": (pd.Timestamp("2023-01-01", tz="UTC"), None),
    "full": (pd.Timestamp("2018-01-01", tz="UTC"), None),
}


def window_portfolio_metrics(
    curves: dict[str, pd.DataFrame],
    trades: pd.DataFrame,
    equities: dict[str, float],
    start: pd.Timestamp,
    end: pd.Timestamp | None,
) -> dict[str, Any]:
    port = portfolio_equity_series(curves, equities)
    if port.empty:
        return {"return_pct": float("nan"), "max_drawdown_pct": float("nan"), "trades": 0}

    if end is not None:
        port = port[(port.index >= start) & (port.index < end)]
    else:
        port = port[port.index >= start]
    if len(port) < 2:
        return {"return_pct": float("nan"), "max_drawdown_pct": float("nan"), "trades": 0}

    base = float(port.iloc[0])
    final = float(port.iloc[-1])
    ret = 100.0 * (final / base - 1.0) if base > 0 else float("nan")
    dd = 100.0 * max_drawdown(port)
    w_trades = trades_in_window(trades, start, end)
    pnls = pd.to_numeric(w_trades["net_pnl"], errors="coerce") if not w_trades.empty else pd.Series(dtype=float)
    pf = float(profit_factor(pnls)) if len(pnls) else float("nan")
    _, sharpe = annualized_vol_and_sharpe(port)
    return {
        "return_pct": round(ret, 2),
        "max_drawdown_pct": round(dd, 2),
        "profit_factor": round(pf, 3) if np.isfinite(pf) else float("nan"),
        "sharpe_ratio": round(sharpe, 3) if np.isfinite(sharpe) else float("nan"),
        "trades": int(len(pnls)),
    }


def metrics_all_windows(
    curves: dict[str, pd.DataFrame],
    trades: pd.DataFrame,
    equities: dict[str, float],
) -> dict[str, dict[str, Any]]:
    return {
        name: window_portfolio_metrics(curves, trades, equities, start, end)
        for name, (start, end) in WF_WINDOWS.items()
    }
