#!/usr/bin/env python3
"""
Offline sweep: replay the multi-sleeve paper portfolio under parameter variants,
aggregate sleeve equity curves (same method as the daily runner summary), and
compare portfolio return / PF / max DD to baseline.

Run from repo root:  python3 btc_breakout_clean/portfolio_param_sweep.py
Or from btc_breakout_clean:  python3 portfolio_param_sweep.py
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
    LIVE_STRATEGY_PARAMS,
    LIVE_SYMBOLS,
    live_symbol_equity,
    live_symbol_source,
    live_strategy_config,
)
from btc_breakout_paper_sim import (  # noqa: E402
    DUKASCOPY_INSTRUMENTS,
    SimConfig,
    StrategyConfig,
    TREND_MODE_CHOICES,
    add_indicators,
    cagr,
    default_skip_saturday_entry,
    dukascopy_cache_path,
    fetch_source_data,
    max_drawdown,
    profit_factor,
    simulate_account,
)

# Match run_binance_paper_daily defaults so local Dukascopy caches stay valid without re-download.
DATA_START = "2018-01-01"
SIM_START = "2018-01-01"
END: str | None = None


def _sim_cfg(symbol: str, equity: float) -> SimConfig:
    sym = symbol.upper()
    src = live_symbol_source(sym)
    return SimConfig(
        source=src,
        data_start=DATA_START,
        sim_start=pd.Timestamp(SIM_START, tz="UTC"),
        end=END,
        equity=equity,
        include_current=False,
        cache_path=Path(""),
        dukascopy_path=dukascopy_cache_path(sym) if src == "dukascopy" else Path(""),
        refresh_cache=False,
        show_trades=0,
        write_files=False,
        out_dir=Path("."),
        instrument=sym,
        skip_saturday_entry=default_skip_saturday_entry(src),
    )


def portfolio_equity_series(
    curves: dict[str, pd.DataFrame],
    initial_equity: dict[str, float],
) -> pd.Series:
    series_list: list[pd.Series] = []
    for sym, curve in curves.items():
        if curve.empty:
            s = pd.Series(dtype=float)
        else:
            s = curve.set_index(pd.to_datetime(curve["date"], utc=True))["equity"].sort_index()
            s = s.astype(float)
        series_list.append(s.rename(sym))
    wide = pd.concat(series_list, axis=1)
    for sym in wide.columns:
        ie = float(initial_equity[str(sym)])
        wide[sym] = wide[sym].ffill()
        wide[sym] = wide[sym].fillna(ie)
    return wide.sum(axis=1).sort_index()


def preload_raw(symbols: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        out[sym] = fetch_source_data(_sim_cfg(sym, 10_000.0))
    return out


def run_portfolio(
    raw_by_symbol: dict[str, pd.DataFrame],
    symbols: tuple[str, ...],
    strategies: dict[str, StrategyConfig],
    equities: dict[str, float] | None = None,
) -> dict[str, Any]:
    if equities is None:
        eq = {s: live_symbol_equity(s, 10_000.0) for s in symbols}
    else:
        eq = dict(equities)
    curves: dict[str, pd.DataFrame] = {}
    trade_parts: list[pd.DataFrame] = []
    for sym in symbols:
        strat = strategies[sym]
        sim_cfg = _sim_cfg(sym, eq[sym])
        raw = raw_by_symbol[sym]
        df = add_indicators(raw, strat)
        trades, curve, _ = simulate_account(df, sim_cfg=sim_cfg, strat_cfg=strat)
        curves[sym] = curve
        if not trades.empty:
            t = trades.copy()
            t["sleeve"] = sym
            trade_parts.append(t)
    init_eq = {s: float(eq[s]) for s in symbols}
    port_eq = portfolio_equity_series(curves, init_eq)
    if port_eq.empty:
        return {
            "return_pct": 0.0,
            "cagr_pct": 0.0,
            "max_drawdown_pct": float("nan"),
            "profit_factor": float("nan"),
            "trades": 0,
            "final_equity": float(sum(init_eq.values())),
            "initial_equity": float(sum(init_eq.values())),
        }
    initial_total = float(sum(init_eq.values()))
    final_equity = float(port_eq.iloc[-1])
    total_return = final_equity / initial_total - 1.0
    start_ts, end_ts = port_eq.index[0], port_eq.index[-1]
    pnls = (
        pd.concat([t["net_pnl"] for t in trade_parts], ignore_index=True)
        if trade_parts
        else pd.Series(dtype=float)
    )
    pf = profit_factor(pnls) if len(pnls) else float("nan")
    return {
        "return_pct": 100.0 * total_return,
        "cagr_pct": 100.0 * cagr(total_return, start_ts, end_ts),
        "max_drawdown_pct": 100.0 * max_drawdown(port_eq),
        "profit_factor": float(pf) if pd.notna(pf) else float("nan"),
        "trades": int(len(pnls)),
        "final_equity": final_equity,
        "initial_equity": initial_total,
    }


def baseline_strategies() -> dict[str, StrategyConfig]:
    return {s: live_strategy_config(s) for s in LIVE_SYMBOLS}


def beats_or_ties_baseline(
    base: dict[str, Any],
    cand: dict[str, Any],
    *,
    ret_tol: float = 0.08,
    pf_tol: float = 0.03,
    dd_tol: float = 0.12,
) -> bool:
    """True if candidate is flat or better on return, PF, and max DD (DD is negative %)."""
    br, bp, bd = base["return_pct"], base["profit_factor"], base["max_drawdown_pct"]
    cr, cp, cd = cand["return_pct"], cand["profit_factor"], cand["max_drawdown_pct"]
    if not (cr >= br - ret_tol):
        return False
    if pd.isna(bp) and pd.isna(cp):
        pass
    elif pd.isna(cp):
        return False
    elif pd.isna(bp):
        if not pd.isna(cp) and cp >= 1.0:
            pass
        else:
            return False
    else:
        if not (cp >= bp - pf_tol):
            return False
    # drawdown: less negative is better; cd >= bd means shallower or equal
    if not (cd >= bd - dd_tol):
        return False
    return True


def main() -> None:
    symbols = tuple(LIVE_SYMBOLS)
    base_strats = baseline_strategies()
    base_eq = {s: float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in symbols}
    extra_syms = ("US500",) if "US500" in DUKASCOPY_INSTRUMENTS else ()
    preload_list = tuple(dict.fromkeys(list(symbols) + list(extra_syms)))
    print(f"Preloading OHLC for {preload_list} ...", flush=True)
    raw_cache = preload_raw(preload_list)
    print("Preload done.", flush=True)

    baseline = run_portfolio(raw_cache, symbols, base_strats, base_eq)
    rows: list[dict[str, Any]] = []

    def record(category: str, label: str, cand: dict[str, Any], meta: dict[str, Any]) -> None:
        ok = beats_or_ties_baseline(baseline, cand)
        rows.append(
            {
                "category": category,
                "label": label,
                "passes": ok,
                "return_pct": cand["return_pct"],
                "cagr_pct": cand["cagr_pct"],
                "max_drawdown_pct": cand["max_drawdown_pct"],
                "profit_factor": cand["profit_factor"],
                "trades": cand["trades"],
                "meta": meta,
            }
        )

    record("baseline", "current LIVE_STRATEGY_PARAMS", baseline, {})

    # --- 1) Trend mode, one symbol at a time ---
    for sym in symbols:
        cur = base_strats[sym].trend_mode
        for mode in TREND_MODE_CHOICES:
            if mode == cur:
                continue
            st = dict(base_strats)
            st[sym] = replace(st[sym], trend_mode=mode)
            cand = run_portfolio(raw_cache, symbols, st, base_eq)
            record("trend_per_symbol", f"{sym} -> {mode}", cand, {"symbol": sym, "trend_mode": mode})

    # --- 2) Global buffer / max_breakout (same for all sleeves) ---
    for buf, mx in [
        (75.0, 200.0),
        (75.0, 225.0),
        (100.0, 200.0),
        (100.0, 250.0),
        (125.0, 225.0),
        (125.0, 250.0),
    ]:
        st = {s: replace(base_strats[s], buffer_bps=buf, max_breakout_bps=mx) for s in symbols}
        cand = run_portfolio(raw_cache, symbols, st, base_eq)
        record("buffer_max_global", f"buffer={buf} max_bps={mx}", cand, {"buffer_bps": buf, "max_breakout_bps": mx})

    # --- 3) Lookback ±5 per symbol (univariate) ---
    for sym in symbols:
        lb0 = base_strats[sym].lookback
        for lb in (max(5, lb0 - 5), lb0 + 5):
            if lb == lb0:
                continue
            st = dict(base_strats)
            st[sym] = replace(st[sym], lookback=lb)
            cand = run_portfolio(raw_cache, symbols, st, base_eq)
            record("lookback_per_symbol", f"{sym} lookback {lb0} -> {lb}", cand, {"symbol": sym, "lookback": lb})

    # --- 4) Add US500 sleeve ($10k), total $50k start — compare level metrics ---
    extra = "US500"
    if extra in DUKASCOPY_INSTRUMENTS:
        sym5 = symbols + (extra,)
        eq5 = {**base_eq, extra: 10_000.0}
        st5 = dict(base_strats)
        st5[extra] = base_strats["XCUUSD"]
        cand5 = run_portfolio(raw_cache, sym5, st5, eq5)
        record(
            "add_sleeve",
            "US500 $10k, clone XCUUSD strategy params",
            cand5,
            {"extra": extra, "n_sleeves": 5},
        )

    # --- 5) Trail ATR (global) ---
    for trail in (0.5, 1.0, 1.5, 2.0, 2.5):
        st = {s: replace(base_strats[s], trail_atr=trail) for s in symbols}
        cand = run_portfolio(raw_cache, symbols, st, base_eq)
        record("trail_atr_global", f"trail_atr={trail}", cand, {"trail_atr": trail})

    # --- Output ---
    out_path = HERE / "portfolio_sweep_results.json"
    serializable = []
    for r in rows:
        x = {k: v for k, v in r.items() if k != "meta"}
        x["meta"] = r["meta"]
        serializable.append(x)
    out_path.write_text(json.dumps(serializable, indent=2), encoding="utf-8")

    b = baseline
    print("=== BASELINE (portfolio sum of sleeve equities) ===")
    print(
        f"  symbols={list(symbols)}  initial=${b['initial_equity']:,.0f}  "
        f"final=${b['final_equity']:,.0f}  trades={b['trades']}"
    )
    print(
        f"  return={b['return_pct']:.2f}%  CAGR={b['cagr_pct']:.2f}%  "
        f"maxDD={b['max_drawdown_pct']:.2f}%  PF={b['profit_factor']:.3f}"
    )
    print()

    winners = [r for r in rows if r["passes"] and r["category"] != "baseline"]
    print(f"=== VARIANTS PASSING return/PF/DD (flat within tolerance) ===  count={len(winners)}")
    for r in sorted(winners, key=lambda x: -x["return_pct"])[:25]:
        print(
            f"  [{r['category']}] {r['label']}\n"
            f"      ret={r['return_pct']:.2f}% CAGR={r['cagr_pct']:.2f}% "
            f"DD={r['max_drawdown_pct']:.2f}% PF={r['profit_factor']:.3f} trades={r['trades']}"
        )
    if not winners:
        print("  (none — keep current LIVE_STRATEGY_PARAMS)")
    print()
    print(f"Full table written to {out_path}")


if __name__ == "__main__":
    main()
