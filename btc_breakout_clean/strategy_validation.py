#!/usr/bin/env python3
"""
Reproducible OOS checks, per-sleeve stats, signal frequency, and book-level overlays.

Run from repo root:
  python3 btc_breakout_clean/strategy_validation.py
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
    LIVE_STRATEGY_PARAMS,
    LIVE_SYMBOLS,
    fetch_binance_daily,
    live_symbol_equity,
    live_symbol_source,
    live_strategy_config,
)
from btc_breakout_paper_sim import (  # noqa: E402
    SimConfig,
    StrategyConfig,
    add_indicators,
    cagr,
    default_skip_saturday_entry,
    dukascopy_cache_path,
    fetch_source_data,
    max_drawdown,
    profit_factor,
    simulate_account,
)

DATA_START = "2018-01-01"
SIM_START = "2018-01-01"
OUT_PATH = HERE / "strategy_validation_results.json"


def _sim_cfg(symbol: str, equity: float, **kwargs: Any) -> SimConfig:
    sym = symbol.upper()
    src = live_symbol_source(sym)
    base = SimConfig(
        source=src,
        data_start=DATA_START,
        sim_start=pd.Timestamp(SIM_START, tz="UTC"),
        end=None,
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
    if not kwargs:
        return base
    return replace(base, **kwargs)


def preload_raw(symbols: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        src = live_symbol_source(sym)
        if src == "binance":
            out[sym] = fetch_binance_daily(sym, DATA_START, None)
        else:
            out[sym] = fetch_source_data(_sim_cfg(sym, 10_000.0))
    return out


def run_sleeve(
    raw: pd.DataFrame,
    symbol: str,
    strat: StrategyConfig,
    equity: float,
    sim_overrides: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    overrides = sim_overrides or {}
    sim = _sim_cfg(symbol, equity, **overrides)
    df = add_indicators(raw, strat)
    return simulate_account(df, sim_cfg=sim, strat_cfg=strat)


def portfolio_equity_series(
    curves: dict[str, pd.DataFrame],
    initial_equity: dict[str, float],
) -> pd.Series:
    parts: list[pd.Series] = []
    for sym, curve in curves.items():
        if curve.empty:
            s = pd.Series(dtype=float)
        else:
            s = curve.set_index(pd.to_datetime(curve["date"], utc=True))["equity"].astype(float)
        parts.append(s.rename(sym))
    wide = pd.concat(parts, axis=1)
    for sym in wide.columns:
        ie = float(initial_equity[str(sym)])
        wide[sym] = wide[sym].ffill().fillna(ie)
    return wide.sum(axis=1).sort_index()


def portfolio_metrics(
    curves: dict[str, pd.DataFrame],
    trades: pd.DataFrame,
    initial_total: float,
) -> dict[str, Any]:
    port = portfolio_equity_series(curves, {s: float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in curves})
    if port.empty:
        return {"return_pct": 0.0, "max_drawdown_pct": float("nan"), "profit_factor": float("nan"), "trades": 0}
    final = float(port.iloc[-1])
    ret = final / initial_total - 1.0
    pnls = pd.to_numeric(trades["net_pnl"], errors="coerce") if not trades.empty else pd.Series(dtype=float)
    pf = profit_factor(pnls) if len(pnls) else float("nan")
    return {
        "return_pct": 100.0 * ret,
        "cagr_pct": 100.0 * cagr(ret, port.index[0], port.index[-1]),
        "max_drawdown_pct": 100.0 * max_drawdown(port),
        "profit_factor": float(pf) if pd.notna(pf) else float("nan"),
        "trades": int(len(pnls)),
        "final_equity": final,
    }


def drawdown_recovery_days(equity: pd.Series) -> int | None:
    if equity.empty or len(equity) < 2:
        return None
    dd = equity / equity.cummax() - 1.0
    trough_pos = int(dd.values.argmin())
    peak_before = float(equity.iloc[: trough_pos + 1].max())
    after = equity.iloc[trough_pos:]
    recovered = after[after >= peak_before]
    if recovered.empty:
        return None
    trough_date = equity.index[trough_pos]
    rec_date = recovered.index[0]
    return int((rec_date - trough_date).days)


def blocked_entries_max_concurrent(
    all_trades: pd.DataFrame,
    max_concurrent: int,
) -> dict[str, frozenset[pd.Timestamp]]:
    blocked_by_sym: dict[str, set[pd.Timestamp]] = {s: set() for s in LIVE_SYMBOLS}
    if all_trades.empty:
        return {s: frozenset() for s in LIVE_SYMBOLS}
    t = all_trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
    t = t.sort_values("entry_date")
    active: list[pd.Timestamp] = []
    for row in t.itertuples(index=False):
        entry = row.entry_date.normalize()
        exit_ = row.exit_date.normalize()
        sym = str(row.sleeve)
        active = [ex for ex in active if ex > entry]
        if len(active) >= max_concurrent:
            blocked_by_sym.setdefault(sym, set()).add(entry)
        else:
            active.append(exit_)
    return {sym: frozenset(blocked_by_sym.get(sym, set())) for sym in LIVE_SYMBOLS}


def trades_in_window(trades: pd.DataFrame, start: pd.Timestamp | None, end: pd.Timestamp | None) -> pd.DataFrame:
    if trades.empty:
        return trades
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    if start is not None:
        t = t.loc[t["entry_date"] >= start]
    if end is not None:
        t = t.loc[t["entry_date"] < end]
    return t


def sleeve_window_metrics(
    trades: pd.DataFrame,
    initial_equity: float,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> dict[str, Any]:
    w = trades_in_window(trades, start, end)
    pnls = pd.to_numeric(w["net_pnl"], errors="coerce") if not w.empty else pd.Series(dtype=float)
    wins = int((pnls > 0).sum()) if len(pnls) else 0
    ret_pct = 100.0 * float(pnls.sum()) / initial_equity if initial_equity else 0.0
    pf = float(profit_factor(pnls)) if len(pnls) else float("nan")
    years = 1.0
    if start is not None and end is not None:
        years = max((end - start).days / 365.25, 1e-9)
    elif not w.empty:
        d0 = w["entry_date"].min()
        d1 = w["entry_date"].max()
        years = max((d1 - d0).days / 365.25, 1e-9)
    trades_per_year = len(pnls) / years if years > 0 else float("nan")
    return {
        "trades": int(len(pnls)),
        "trades_per_year": round(float(trades_per_year), 2),
        "win_rate_pct": 100.0 * wins / len(pnls) if len(pnls) else float("nan"),
        "net_pnl": float(pnls.sum()) if len(pnls) else 0.0,
        "return_pct_on_sleeve": ret_pct,
        "profit_factor": pf,
        "avg_net_pnl": float(pnls.mean()) if len(pnls) else float("nan"),
    }


def beats_baseline(base: dict[str, float], cand: dict[str, float]) -> bool:
    return (
        cand["return_pct"] >= base["return_pct"] - 0.08
        and cand["profit_factor"] >= base["profit_factor"] - 0.03
        and cand["max_drawdown_pct"] >= base["max_drawdown_pct"] - 0.12
    )


def run_full_book(
    raw_cache: dict[str, pd.DataFrame],
    symbols: tuple[str, ...],
    strategies: dict[str, StrategyConfig],
    sim_overrides_by_symbol: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], pd.DataFrame, dict[str, float]]:
    curves: dict[str, pd.DataFrame] = {}
    trade_parts: list[pd.DataFrame] = []
    equities = {s: live_symbol_equity(s, 10_000.0) for s in symbols}
    overrides = sim_overrides_by_symbol or {}
    for sym in symbols:
        tr, cu, _ = run_sleeve(
            raw_cache[sym],
            sym,
            strategies[sym],
            equities[sym],
            overrides.get(sym),
        )
        curves[sym] = cu
        if not tr.empty:
            part = tr.copy()
            part["sleeve"] = sym
            trade_parts.append(part)
    all_trades = pd.concat(trade_parts, ignore_index=True) if trade_parts else pd.DataFrame()
    return curves, {}, all_trades, equities


def sim_overrides_for_max_concurrent(
    raw_cache: dict[str, pd.DataFrame],
    symbols: tuple[str, ...],
    strategies: dict[str, StrategyConfig],
    max_concurrent: int,
) -> dict[str, dict[str, Any]]:
    """Pass-1 uncapped replay → blocked entry dates for a portfolio concurrent cap."""
    if max_concurrent <= 0 or len(symbols) <= 1:
        return {}
    _, _, base_trades, _ = run_full_book(raw_cache, symbols, strategies)
    blocked = blocked_entries_max_concurrent(base_trades, max_concurrent)
    return {sym: {"blocked_entry_dates": blocked[sym]} for sym in symbols}


def merge_sim_overrides(
    base: dict[str, dict[str, Any]] | None,
    extra: dict[str, dict[str, Any]],
    symbols: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {s: dict((base or {}).get(s, {})) for s in symbols}
    for sym in symbols:
        if sym not in extra:
            continue
        merged = {**out.get(sym, {}), **extra[sym]}
        b0 = out.get(sym, {}).get("blocked_entry_dates")
        b1 = extra[sym].get("blocked_entry_dates")
        if b0 is not None and b1 is not None:
            merged["blocked_entry_dates"] = frozenset(b0) | frozenset(b1)
        out[sym] = merged
    return out


def run_full_book_live(
    raw_cache: dict[str, pd.DataFrame],
    symbols: tuple[str, ...],
    strategies: dict[str, StrategyConfig],
    sim_overrides_by_symbol: dict[str, dict[str, Any]] | None = None,
    *,
    max_concurrent: int | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], pd.DataFrame, dict[str, float]]:
    """Full book replay matching live: per-symbol stops + max concurrent entries (default 4)."""
    cap = LIVE_MAX_CONCURRENT_ENTRIES if max_concurrent is None else max_concurrent
    overrides = merge_sim_overrides(
        sim_overrides_by_symbol,
        sim_overrides_for_max_concurrent(raw_cache, symbols, strategies, cap),
        symbols,
    )
    return run_full_book(raw_cache, symbols, strategies, overrides)


def main() -> None:
    symbols = tuple(LIVE_SYMBOLS)
    print(f"Preloading OHLC for {symbols} ...", flush=True)
    raw_cache = preload_raw(symbols)
    print("Preload done.", flush=True)

    strategies = {s: live_strategy_config(s) for s in symbols}
    curves, _, all_trades, equities = run_full_book_live(raw_cache, symbols, strategies)
    initial_total = sum(equities.values())
    baseline = portfolio_metrics(curves, all_trades, initial_total)
    n_blocked_4 = sum(
        len(sim_overrides_for_max_concurrent(raw_cache, symbols, strategies, LIVE_MAX_CONCURRENT_ENTRIES)[s][
            "blocked_entry_dates"
        ])
        for s in symbols
    )

    # --- Per-sleeve full sample + OOS windows ---
    windows: list[tuple[str, pd.Timestamp | None, pd.Timestamp | None]] = [
        ("full", None, None),
        ("pre_2024", None, pd.Timestamp("2024-01-01", tz="UTC")),
        ("ex_2024", pd.Timestamp("2024-01-01", tz="UTC"), None),
        ("y2023", pd.Timestamp("2023-01-01", tz="UTC"), pd.Timestamp("2024-01-01", tz="UTC")),
        ("y2024", pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2025-01-01", tz="UTC")),
        ("y2025", pd.Timestamp("2025-01-01", tz="UTC"), pd.Timestamp("2026-01-01", tz="UTC")),
    ]

    per_sleeve: dict[str, Any] = {}
    for sym in symbols:
        tr, cu, summary = run_sleeve(raw_cache[sym], sym, strategies[sym], equities[sym])
        port_eq = cu.set_index(pd.to_datetime(cu["date"], utc=True))["equity"].astype(float) if not cu.empty else pd.Series(dtype=float)
        rec_days = drawdown_recovery_days(port_eq) if not port_eq.empty else None
        sym_windows = {label: sleeve_window_metrics(tr, equities[sym], start, end) for label, start, end in windows}
        per_sleeve[sym] = {
            "source": live_symbol_source(sym),
            "full_summary": summary,
            "max_drawdown_recovery_days": rec_days,
            "windows": sym_windows,
        }

    # --- Gold regime comparison ---
    xau_bull = replace(strategies["XAUUSD"], trend_mode="bull_only")
    _, _, xau_live_summary = run_sleeve(raw_cache["XAUUSD"], "XAUUSD", strategies["XAUUSD"], equities["XAUUSD"])
    _, _, xau_bull_summary = run_sleeve(raw_cache["XAUUSD"], "XAUUSD", xau_bull, equities["XAUUSD"])

    # --- Overlay: max 3 concurrent (baseline is live max 4) ---
    overlay_rows: list[dict[str, Any]] = []
    oc_curves, _, oc_trades, _ = run_full_book_live(
        raw_cache, symbols, strategies, max_concurrent=3
    )
    oc_metrics = portfolio_metrics(oc_curves, oc_trades, initial_total)
    blocked_3 = sim_overrides_for_max_concurrent(raw_cache, symbols, strategies, 3)
    total_blocked_3 = sum(len(blocked_3[s]["blocked_entry_dates"]) for s in symbols)
    overlay_rows.append(
        {
            "label": "max_3_concurrent_sleeves",
            "blocked_entry_events": total_blocked_3,
            "metrics": oc_metrics,
            "passes_baseline": beats_baseline(baseline, oc_metrics),
        }
    )

    # --- Overlay: per-sleeve HWM pause 12% ---
    hwm_curves: dict[str, pd.DataFrame] = {}
    hwm_parts: list[pd.DataFrame] = []
    for sym in symbols:
        tr, cu, _ = run_sleeve(
            raw_cache[sym],
            sym,
            strategies[sym],
            equities[sym],
            {"hwm_pause_pct": 12.0},
        )
        hwm_curves[sym] = cu
        if not tr.empty:
            part = tr.copy()
            part["sleeve"] = sym
            hwm_parts.append(part)
    hwm_trades = pd.concat(hwm_parts, ignore_index=True) if hwm_parts else pd.DataFrame()
    hwm_metrics = portfolio_metrics(hwm_curves, hwm_trades, initial_total)
    overlay_rows.append(
        {
            "label": "hwm_pause_12pct_per_sleeve",
            "metrics": hwm_metrics,
            "passes_baseline": beats_baseline(baseline, hwm_metrics),
        }
    )

    # --- Saturday skip impact (Dukascopy sleeves only) ---
    sat_rows: dict[str, Any] = {}
    for sym in symbols:
        if live_symbol_source(sym) != "dukascopy":
            continue
        tr_on, _, _ = run_sleeve(raw_cache[sym], sym, strategies[sym], equities[sym])
        tr_off, _, _ = run_sleeve(
            raw_cache[sym],
            sym,
            strategies[sym],
            equities[sym],
            {"skip_saturday_entry": False},
        )
        sat_rows[sym] = {
            "trades_skip_on": int(len(tr_on)),
            "trades_skip_off": int(len(tr_off)),
            "pnl_skip_on": float(tr_on["net_pnl"].sum()) if not tr_on.empty else 0.0,
            "pnl_skip_off": float(tr_off["net_pnl"].sum()) if not tr_off.empty else 0.0,
        }

    report = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "live_max_concurrent_entries": LIVE_MAX_CONCURRENT_ENTRIES,
        "blocked_entry_events_max_4": n_blocked_4,
        "baseline_portfolio": baseline,
        "per_sleeve": per_sleeve,
        "xau_regime_compare": {
            "sma200_95": xau_live_summary,
            "bull_only": xau_bull_summary,
        },
        "overlays": overlay_rows,
        "dukascopy_saturday_skip": sat_rows,
    }

    OUT_PATH.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(
        f"\n=== BASELINE PORTFOLIO ({len(symbols)} sleeves, max {LIVE_MAX_CONCURRENT_ENTRIES} concurrent, "
        f"{n_blocked_4} blocked entries) ==="
    )
    print(
        f"  return={baseline['return_pct']:.2f}%  maxDD={baseline['max_drawdown_pct']:.2f}%  "
        f"PF={baseline['profit_factor']:.3f}  trades={baseline['trades']}"
    )

    print("\n=== PER-SLEEVE (full sample) ===")
    for sym in symbols:
        w = per_sleeve[sym]["windows"]["full"]
        ex = per_sleeve[sym]["windows"]["ex_2024"]
        print(
            f"  {sym}: trades={w['trades']} ({w['trades_per_year']}/yr)  "
            f"ret={w['return_pct_on_sleeve']:.1f}%  PF={w['profit_factor']:.2f}  |  "
            f"ex-2024: trades={ex['trades']} PF={ex['profit_factor']:.2f} pnl=${ex['net_pnl']:,.0f}"
        )

    print("\n=== OVERLAYS vs BASELINE ===")
    for row in overlay_rows:
        m = row["metrics"]
        print(
            f"  {row['label']}: ret={m['return_pct']:.2f}% DD={m['max_drawdown_pct']:.2f}% "
            f"PF={m['profit_factor']:.3f} passes={row['passes_baseline']}"
        )

    print(f"\nFull JSON: {OUT_PATH}")


if __name__ == "__main__":
    main()
