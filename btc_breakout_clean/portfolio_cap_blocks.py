"""Portfolio max-concurrent cap map — disk cache + derive from pass-1 trades."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from btc_breakout_binance_paper_bot import LIVE_SYMBOLS
from strategy_validation import blocked_entries_max_concurrent

CAP_BLOCKS_FILENAME = "portfolio_cap_blocks.json"


def portfolio_cap_blocks_path(state_dir: Path) -> Path:
    return state_dir / CAP_BLOCKS_FILENAME


def write_portfolio_cap_blocks(
    state_dir: Path,
    blocked_by_sym: dict[str, frozenset[pd.Timestamp]],
    *,
    max_concurrent: int,
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "max_concurrent": max_concurrent,
        "blocked": {
            sym: sorted(d.strftime("%Y-%m-%d") for d in dates)
            for sym, dates in blocked_by_sym.items()
        },
    }
    portfolio_cap_blocks_path(state_dir).write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )


def read_portfolio_cap_blocks(
    state_dir: Path,
    symbols: list[str] | None = None,
) -> dict[str, frozenset[pd.Timestamp]] | None:
    path = portfolio_cap_blocks_path(state_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    syms = symbols or list(LIVE_SYMBOLS)
    raw = payload.get("blocked") or {}
    out: dict[str, frozenset[pd.Timestamp]] = {}
    for sym in syms:
        key = sym.upper()
        dates = raw.get(key) or raw.get(sym) or []
        out[key] = frozenset(pd.to_datetime(d, utc=True).normalize() for d in dates)
    return out


def portfolio_blocked_from_results(
    results: list[dict[str, Any]],
    max_concurrent: int,
    symbols: list[str] | None = None,
) -> dict[str, frozenset[pd.Timestamp]]:
    """Cap map from pass-1 trades already in memory (no extra replay)."""
    syms = symbols or list(LIVE_SYMBOLS)
    if max_concurrent <= 0:
        return {s.upper(): frozenset() for s in syms}
    parts: list[pd.DataFrame] = []
    for result in results:
        trades = result.get("trades")
        if trades is None or trades.empty:
            continue
        t = trades.copy()
        t["sleeve"] = str(result.get("symbol", "")).upper()
        parts.append(t)
    if not parts:
        return {s.upper(): frozenset() for s in syms}
    all_trades = pd.concat(parts, ignore_index=True)
    return blocked_entries_max_concurrent(all_trades, max_concurrent, tuple(s.upper() for s in syms))
