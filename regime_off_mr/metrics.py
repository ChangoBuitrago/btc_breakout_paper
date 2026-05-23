"""Solo discovery gates for regime-off research."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from regime_off_mr.config import (
    MAX_DD_PCT,
    MAX_EX_PF_IF_FEW_TRADES,
    MAX_TOP_TRADE_PNL_SHARE,
    MAX_TRADES_FULL,
    MIN_PF_EX_2024,
    MIN_PF_FULL,
    MIN_TRADES_EX_2024,
    MIN_TRADES_FULL,
)


def window_metrics(trades: pd.DataFrame, start: str | None, end: str | None) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "profit_factor": float("nan"), "net_pnl": 0.0, "return_pct_on_equity": 0.0}
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    if start:
        t = t.loc[t["entry_date"] >= pd.Timestamp(start, tz="UTC")]
    if end:
        t = t.loc[t["entry_date"] < pd.Timestamp(end, tz="UTC")]
    pnls = pd.to_numeric(t["net_pnl"], errors="coerce")
    from regime_off_mr.sim import profit_factor

    return {
        "trades": int(len(pnls)),
        "profit_factor": float(profit_factor(pnls)) if len(pnls) else float("nan"),
        "net_pnl": float(pnls.sum()),
        "return_pct_on_equity": 100.0 * float(pnls.sum()) / 10_000.0,
    }


def top_trade_share(trades: pd.DataFrame) -> float:
    if trades.empty:
        return 0.0
    pnls = pd.to_numeric(trades["net_pnl"], errors="coerce")
    total = float(pnls.sum())
    if abs(total) < 1e-9:
        return 0.0
    return float(pnls.max()) / total


def passes_discovery(full: dict[str, Any], ex: dict[str, Any], trades: pd.DataFrame) -> bool:
    pf = float(full.get("profit_factor") or 0)
    pf_ex = float(ex.get("profit_factor") or 0)
    n_full = int(full.get("trades") or 0)
    n_ex = int(ex.get("trades") or 0)

    if not np.isfinite(pf) or pf < MIN_PF_FULL:
        return False
    if n_ex < MIN_TRADES_EX_2024 or not np.isfinite(pf_ex) or pf_ex < MIN_PF_EX_2024:
        return False
    if n_full < MIN_TRADES_FULL or n_full > MAX_TRADES_FULL:
        return False
    if float(full.get("max_drawdown_pct") or 0) < MAX_DD_PCT:
        return False
    if top_trade_share(trades) > MAX_TOP_TRADE_PNL_SHARE:
        return False
    if n_ex < 8 and pf_ex > MAX_EX_PF_IF_FEW_TRADES:
        return False
    return True


def beats_baseline(base: dict[str, float], cand: dict[str, float]) -> bool:
    return (
        cand["return_pct"] >= base["return_pct"] - 8.0
        and cand["profit_factor"] >= base["profit_factor"] - 0.03
        and cand["max_drawdown_pct"] >= base["max_drawdown_pct"] - 12.0
    )
