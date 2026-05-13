#!/usr/bin/env python3
"""Find crypto sleeve params for 6-sleeve book vs 4-sleeve baseline."""

from __future__ import annotations

import itertools
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import LIVE_SYMBOLS, live_strategy_config, fetch_binance_daily
from btc_breakout_paper_sim import (
    SimConfig,
    add_indicators,
    cagr,
    fetch_dukascopy_instrument,
    dukascopy_cache_path,
    max_drawdown,
    profit_factor,
    simulate_account,
)

DATA_START = "2018-01-01"
CRYPTO = ("BTCUSD", "ETHUSDT", "BNBUSDT")
METALS = ("XAUUSD", "XAGUSD", "XCUUSD")
ALL6 = CRYPTO + METALS


def load_raw(sym: str) -> pd.DataFrame:
    if sym.endswith("USDT"):
        return fetch_binance_daily(sym, DATA_START, None)
    return fetch_dukascopy_instrument(sym, dukascopy_cache_path(sym), DATA_START, None, False, False)


def sim_cfg(sym: str) -> SimConfig:
    src = "binance" if sym.endswith("USDT") else "dukascopy"
    return SimConfig(
        source=src,
        data_start=DATA_START,
        sim_start=pd.Timestamp(DATA_START, tz="UTC"),
        end=None,
        equity=10_000.0,
        include_current=False,
        cache_path=Path(""),
        dukascopy_path=dukascopy_cache_path(sym) if src == "dukascopy" else Path(""),
        refresh_cache=False,
        show_trades=0,
        write_files=False,
        out_dir=Path("."),
        instrument=sym,
    )


def crypto_strat(sym: str, lb: int, buf: float, mx: float, hold: int):
    base = live_strategy_config(sym)
    return replace(
        base,
        lookback=lb,
        buffer_bps=buf,
        max_breakout_bps=mx,
        hold_days=hold,
        trend_mode="bull_only",
        fee_bps=10.0,
        compound=True,
    )


def portfolio(curves: dict[str, pd.DataFrame], parts: list[pd.Series], n: int) -> dict[str, float]:
    series = []
    for sym, cu in curves.items():
        s = cu.set_index(pd.to_datetime(cu["date"], utc=True))["equity"].astype(float).rename(sym)
        series.append(s)
    wide = pd.concat(series, axis=1).ffill().fillna(10_000.0)
    port = wide.sum(axis=1).sort_index()
    init = 10_000.0 * n
    final = float(port.iloc[-1])
    ret = final / init - 1.0
    pnls = pd.concat(parts, ignore_index=True) if parts else pd.Series(dtype=float)
    return {
        "return_pct": 100.0 * ret,
        "cagr_pct": 100.0 * cagr(ret, port.index[0], port.index[-1]),
        "max_dd_pct": 100.0 * max_drawdown(port),
        "pf": float(profit_factor(pnls)) if len(pnls) else float("nan"),
        "trades": int(len(pnls)),
    }


def run_book(
    crypto_params: dict[str, tuple[int, float, float, int]],
    symbols: tuple[str, ...],
    ind_cache: dict[tuple[str, int, float, float], pd.DataFrame],
) -> dict[str, float]:
    curves: dict[str, pd.DataFrame] = {}
    parts: list[pd.Series] = []
    for sym in symbols:
        if sym in CRYPTO:
            lb, buf, mx, hold = crypto_params[sym]
            strat = crypto_strat(sym, lb, buf, mx, hold)
            df = ind_cache[(sym, lb, buf, mx)]
        else:
            strat = live_strategy_config(sym)
            df = ind_cache[(sym, "metal")]
        trades, curve, _ = simulate_account(df, sim_cfg=sim_cfg(sym), strat_cfg=strat)
        curves[sym] = curve
        if not trades.empty:
            parts.append(trades["net_pnl"])
    return portfolio(curves, parts, len(symbols))


def build_cache(raw: dict[str, pd.DataFrame]) -> dict:
    cache: dict = {}
    lookbacks = [10, 15, 20, 25]
    buffers = [100.0, 125.0, 150.0, 175.0]
    maxbps = [200.0, 225.0, 250.0, 275.0, 300.0, 350.0, 400.0]
    for sym in CRYPTO:
        for lb, buf, mx in itertools.product(lookbacks, buffers, maxbps):
            cache[(sym, lb, buf, mx)] = add_indicators(raw[sym], crypto_strat(sym, lb, buf, mx, 5))
    for sym in METALS:
        cache[(sym, "metal")] = add_indicators(raw[sym], live_strategy_config(sym))
    return cache


def passes(base: dict[str, float], cand: dict[str, float]) -> bool:
    return (
        cand["return_pct"] >= base["return_pct"] - 0.08
        and cand["pf"] >= base["pf"] - 0.03
        and cand["max_dd_pct"] >= base["max_dd_pct"] - 0.12
    )


def main() -> None:
    raw = {s: load_raw(s) for s in ALL6}
    cache = build_cache(raw)
    holds = [5, 7, 10]

    curves, parts = {}, []
    for sym in LIVE_SYMBOLS:
        strat = live_strategy_config(sym)
        if sym == "BTCUSD":
            df = cache[(sym, 15, 100.0, 225.0)]
        else:
            df = cache[(sym, "metal")]
        tr, cu, _ = simulate_account(df, sim_cfg=sim_cfg(sym), strat_cfg=strat)
        curves[sym] = cu
        if not tr.empty:
            parts.append(tr["net_pnl"])
    base4 = portfolio(curves, parts, 4)
    print("baseline4", base4)

    seeds = [
        {"BTCUSD": (15, 100, 225, 5), "ETHUSDT": (10, 150, 300, 10), "BNBUSDT": (15, 100, 400, 7)},
        {"BTCUSD": (15, 125, 225, 5), "ETHUSDT": (15, 125, 275, 7), "BNBUSDT": (15, 125, 300, 7)},
        {"BTCUSD": (15, 150, 225, 5), "ETHUSDT": (15, 150, 300, 10), "BNBUSDT": (15, 150, 350, 7)},
        {"BTCUSD": (20, 125, 200, 7), "ETHUSDT": (15, 125, 250, 10), "BNBUSDT": (15, 125, 300, 7)},
    ]

    best = seeds[0]
    best_m = run_book(best, ALL6, cache)
    for seed in seeds:
        for hold_combo in itertools.product(holds, repeat=3):
            params = {
                sym: (seed[sym][0], seed[sym][1], seed[sym][2], hold_combo[i])
                for i, sym in enumerate(CRYPTO)
            }
            m = run_book(params, ALL6, cache)
            if passes(base4, m) and (m["return_pct"], m["pf"], m["max_dd_pct"]) > (
                best_m["return_pct"],
                best_m["pf"],
                best_m["max_dd_pct"],
            ):
                best, best_m = params, m

    # greedy per symbol
    lookbacks = [10, 15, 20, 25]
    buffers = [100.0, 125.0, 150.0, 175.0]
    maxbps = [200.0, 225.0, 250.0, 275.0, 300.0, 350.0, 400.0]
    for sym in CRYPTO:
        improved = True
        while improved:
            improved = False
            for lb, buf, mx, hold in itertools.product(lookbacks, buffers, maxbps, holds):
                trial = dict(best)
                trial[sym] = (lb, buf, mx, hold)
                m = run_book(trial, ALL6, cache)
                if passes(base4, m) and (m["return_pct"], m["pf"], m["max_dd_pct"]) > (
                    best_m["return_pct"],
                    best_m["pf"],
                    best_m["max_dd_pct"],
                ):
                    best, best_m = trial, m
                    improved = True

    print("best", best)
    print("metrics6", best_m)
    print("passes", passes(base4, best_m))


if __name__ == "__main__":
    main()
