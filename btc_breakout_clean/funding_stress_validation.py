#!/usr/bin/env python3
"""
Funding stress on live baseline (crypto sleeves treated as if perps).

Modes:
  - baseline spot replay (no funding in sim)
  - historical drag: actual Binance funding during each crypto hold
  - flat drag: 0.01 / 0.03 / 0.05 / 0.10% per 8h on deployed notional
  - funding_skip_3bps: skip crypto entries when prior-day funding max > 3 bps

Run: python3 btc_breakout_clean/funding_stress_validation.py
"""

from __future__ import annotations

import json
import sys
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
    LIVE_SYMBOLS,
    live_strategy_config,
)
from oob_experiments_validation import blocked_high_funding, fetch_funding_daily  # noqa: E402
from strategy_validation import (  # noqa: E402
    beats_baseline,
    portfolio_equity_series,
    portfolio_metrics,
    preload_raw,
    run_full_book_live,
)

OUT_PATH = HERE / "funding_stress_validation_results.json"
CRYPTO = frozenset(LIVE_CRYPTO_SYMBOLS) - {"BTCUSD"}


def load_funding_cache() -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    for sym in CRYPTO:
        try:
            s = fetch_funding_daily(sym, "2017-01-01")
            if not s.empty:
                out[sym] = s
        except Exception:
            continue
    return out


def trade_funding_drag(
    entry: pd.Timestamp,
    exit: pd.Timestamp,
    notional: float,
    funding: pd.Series,
    *,
    flat_rate: float | None = None,
) -> float:
    if notional <= 0:
        return 0.0
    entry = pd.Timestamp(entry).tz_convert("UTC").normalize()
    exit = pd.Timestamp(exit).tz_convert("UTC").normalize()
    days = pd.date_range(entry, exit, freq="D", tz="UTC")
    drag = 0.0
    for d in days:
        if flat_rate is not None:
            rate = flat_rate
        elif funding.empty:
            rate = 0.0
        else:
            prior = funding.index[funding.index <= d]
            rate = float(funding.loc[prior[-1]]) if len(prior) else 0.0
        drag += notional * rate * 3.0  # three 8h prints per day
    return drag


def apply_drag_to_trades(
    trades: pd.DataFrame,
    funding_cache: dict[str, pd.Series],
    *,
    flat_rate: float | None = None,
) -> tuple[pd.DataFrame, float]:
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
    sym_col = "sleeve" if "sleeve" in t.columns else "symbol"
    drags: list[float] = []
    for row in t.itertuples(index=False):
        sym = str(getattr(row, sym_col))
        if sym not in CRYPTO:
            drags.append(0.0)
            continue
        notional = float(getattr(row, "entry_notional", 0.0) or 0.0)
        fs = funding_cache.get(sym, pd.Series(dtype=float))
        drags.append(
            trade_funding_drag(row.entry_date, row.exit_date, notional, fs, flat_rate=flat_rate)
        )
    t["funding_drag"] = drags
    t["net_pnl_adj"] = pd.to_numeric(t["net_pnl"], errors="coerce") - t["funding_drag"]
    return t, float(t["funding_drag"].sum())


def metrics_after_drag(
    curves: dict[str, pd.DataFrame],
    trades_adj: pd.DataFrame,
    initial: float,
    sleeve_eq: dict[str, float],
) -> dict[str, Any]:
    """Approximate book metrics after proportional funding drag per sleeve."""
    t = trades_adj.copy()
    sym_col = "sleeve" if "sleeve" in t.columns else "symbol"
    drag_by_sym = t.groupby(sym_col)["funding_drag"].sum().to_dict()
    pnl_by_sym = t.groupby(sym_col)["net_pnl"].sum().to_dict()

    adj_curves: dict[str, pd.DataFrame] = {}
    for sym, cu in curves.items():
        c = cu.copy()
        eq = c["equity"].astype(float)
        total_pnl = float(pnl_by_sym.get(sym, 0.0))
        drag = float(drag_by_sym.get(sym, 0.0))
        if total_pnl != 0.0 and drag != 0.0:
            # scale equity path down proportionally to funding drag
            scale = 1.0 - drag / total_pnl
            base = float(sleeve_eq.get(sym, initial / len(curves)))
            adj = base + (eq - base) * scale
            c["equity"] = adj
        adj_curves[sym] = c

    tr = t.copy()
    tr["net_pnl"] = tr["net_pnl_adj"]
    return portfolio_metrics(adj_curves, tr, initial, initial_equity_by_sleeve=sleeve_eq)


def run_baseline(raw: dict[str, pd.DataFrame]) -> tuple[dict, dict, pd.DataFrame, dict, float]:
    strats = {s: live_strategy_config(s) for s in LIVE_SYMBOLS}
    curves, _, trades, eq = run_full_book_live(
        raw, tuple(LIVE_SYMBOLS), strats, max_concurrent=LIVE_MAX_CONCURRENT_ENTRIES
    )
    initial = sum(eq.values())
    m = portfolio_metrics(curves, trades, initial)
    return strats, curves, trades, eq, initial


def run_funding_skip(raw: dict[str, pd.DataFrame], strats: dict[str, Any]) -> dict[str, Any]:
    blocked = blocked_high_funding(raw, max_rate=0.0003)
    overrides = {sym: {"blocked_entry_dates": blocked.get(sym, frozenset())} for sym in LIVE_SYMBOLS}
    curves, _, trades, eq = run_full_book_live(
        raw,
        tuple(LIVE_SYMBOLS),
        strats,
        sim_overrides_by_symbol=overrides,
        max_concurrent=LIVE_MAX_CONCURRENT_ENTRIES,
    )
    initial = sum(eq.values())
    m = portfolio_metrics(curves, trades, initial)
    return {"label": "funding_skip_3bps", "metrics": m, "trades": int(len(trades))}


def main() -> None:
    print("Funding stress validation", flush=True)
    raw = preload_raw(tuple(LIVE_SYMBOLS))
    funding_cache = load_funding_cache()
    print(f"Loaded funding for {list(funding_cache.keys())}", flush=True)

    _, curves, trades, eq, initial = run_baseline(raw)
    sleeve_eq = {s: float(eq[s]) for s in LIVE_SYMBOLS}
    bm = portfolio_metrics(curves, trades, initial)
    net_pnl = float(pd.to_numeric(trades["net_pnl"], errors="coerce").sum())

    rows: list[dict[str, Any]] = [
        {
            "label": "spot_baseline",
            "kind": "baseline",
            "metrics": bm,
            "trades": int(len(trades)),
            "total_funding_drag_usd": 0.0,
            "drag_pct_of_net_pnl": 0.0,
        }
    ]

    # Historical drag
    t_hist, drag_hist = apply_drag_to_trades(trades, funding_cache)
    m_hist = metrics_after_drag(curves, t_hist, initial, sleeve_eq)
    rows.append(
        {
            "label": "historical_funding_drag",
            "kind": "post_hoc_drag",
            "metrics": m_hist,
            "trades": int(len(trades)),
            "total_funding_drag_usd": drag_hist,
            "drag_pct_of_net_pnl": 100.0 * drag_hist / net_pnl if net_pnl else float("nan"),
        }
    )

    for bps, rate in [(1, 0.0001), (3, 0.0003), (5, 0.0005), (10, 0.001)]:
        _, drag = apply_drag_to_trades(trades, funding_cache, flat_rate=rate)
        t_adj, _ = apply_drag_to_trades(trades, funding_cache, flat_rate=rate)
        m = metrics_after_drag(curves, t_adj, initial, sleeve_eq)
        rows.append(
            {
                "label": f"flat_{bps}bps_per_8h",
                "kind": "post_hoc_drag",
                "flat_rate_per_8h": rate,
                "metrics": m,
                "trades": int(len(trades)),
                "total_funding_drag_usd": drag,
                "drag_pct_of_net_pnl": 100.0 * drag / net_pnl if net_pnl else float("nan"),
            }
        )

    strats = {s: live_strategy_config(s) for s in LIVE_SYMBOLS}
    skip = run_funding_skip(raw, strats)
    skip["kind"] = "sim_skip"
    rows.append(skip)

    for r in rows:
        m = r["metrics"]
        drag_note = ""
        if r.get("total_funding_drag_usd"):
            drag_note = f" drag=${r['total_funding_drag_usd']:.0f} ({r.get('drag_pct_of_net_pnl', 0):.1f}% pnl)"
        print(
            f"  {r['label']:28} ret={m['return_pct']:6.1f}% DD={m['max_drawdown_pct']:6.2f}% "
            f"PF={m['profit_factor']:.2f} Sh={m['sharpe_ratio']:.2f}{drag_note}",
            flush=True,
        )

    for r in rows:
        r["passes_baseline"] = r["label"] == "spot_baseline" or beats_baseline(bm, r["metrics"])

    payload = {
        "baseline_label": "spot_baseline",
        "baseline_metrics": bm,
        "net_pnl_spot_usd": net_pnl,
        "note": "Crypto ex-BTCUSD as perps; 3 funding prints/day; spot sim unchanged.",
        "variants": rows,
        "passing_labels": [r["label"] for r in rows if r.get("passes_baseline") and r["label"] != "spot_baseline"],
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
