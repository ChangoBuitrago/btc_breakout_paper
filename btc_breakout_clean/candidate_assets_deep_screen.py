#!/usr/bin/env python3
"""
Deep candidate sleeve search: expanded universe, param grid, book swaps, correlation.

Run from repo root (15–45 min depending on cache):
  python3 btc_breakout_clean/candidate_assets_deep_screen.py

Optional quick mode (fewer symbols / smaller grid):
  python3 btc_breakout_clean/candidate_assets_deep_screen.py --quick
"""

from __future__ import annotations

import argparse
import itertools
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
    fetch_binance_daily,
    live_symbol_source,
    live_strategy_config,
)
from btc_breakout_paper_sim import (  # noqa: E402
    DUKASCOPY_INSTRUMENTS,
    SimConfig,
    StrategyConfig,
    add_indicators,
    default_skip_saturday_entry,
    dukascopy_cache_path,
    fetch_source_data,
    profit_factor,
    simulate_account,
)
from portfolio_param_sweep import beats_or_ties_baseline, run_portfolio  # noqa: E402

OUT_PATH = HERE / "candidate_assets_deep_screen_results.json"
DATA_START = "2018-01-01"
MIN_BARS = 800
EX_2024 = pd.Timestamp("2024-01-01", tz="UTC")

# Binance alts to screen (liquid USDT pairs)
BINANCE_CANDIDATES: dict[str, str] = {
    "SOLUSDT": "crypto_alt",
    "LINKUSDT": "crypto_alt",
    "ADAUSDT": "crypto_alt",
    "AVAXUSDT": "crypto_alt",
    "XRPUSDT": "crypto_alt",
    "DOGEUSDT": "crypto_alt",
    "ATOMUSDT": "crypto_alt",
}

# Dukascopy (exclude live sleeves and aliases)
DUKA_CLASS = {
    "US500": "equity_index",
    "NAS100": "equity_index",
    "US2000": "equity_index",
    "BRENT": "energy",
    "CL": "energy",
    "USDJPY": "fx",
    "EURJPY": "fx",
    "GBPJPY": "fx",
    "AUDJPY": "fx",
    "CADJPY": "fx",
    "CHFJPY": "fx",
}

FEE_BPS = {"crypto_alt": 10.0, "energy": 5.0, "equity_index": 2.0, "fx": 2.0, "metal": 2.0}

COARSE_TEMPLATES: dict[str, dict[str, Any]] = {
    "crypto": {"lookback": 15, "buffer_bps": 125.0, "hold_min": 6, "hold_max": 10, "trend_mode": "bull_only"},
    "metal": {"lookback": 30, "buffer_bps": 100.0, "hold_min": 9, "hold_max": 15, "trend_mode": "bull_only"},
    "gold": {"lookback": 30, "buffer_bps": 100.0, "hold_min": 9, "hold_max": 15, "trend_mode": "sma200_95"},
    "short": {"lookback": 15, "buffer_bps": 100.0, "hold_min": 4, "hold_max": 5, "trend_mode": "bull_only"},
    "fx": {"lookback": 20, "buffer_bps": 75.0, "hold_min": 6, "hold_max": 10, "trend_mode": "bull_only"},
}

TEMPLATE_BY_CLASS = {
    "crypto_alt": ("crypto", "metal", "short"),
    "energy": ("short", "metal", "crypto"),
    "equity_index": ("metal", "gold", "crypto"),
    "fx": ("fx", "metal", "short"),
}

# Deep grid (applied to phase-1 winners only)
GRID_LOOKBACKS = [10, 15, 20, 30]
GRID_BUFFERS = [75.0, 100.0, 125.0, 150.0]
GRID_HOLDS = [(4, 5), (6, 10), (9, 15), (13, 15)]
GRID_TRENDS = ["bull_only", "sma200_95"]
GRID_MAX_BPS = 225.0

SWAP_TARGETS = ("XCUUSD", "BTCUSD")  # weakest sleeves in prior validation


def build_universe(quick: bool) -> dict[str, str]:
    live = set(LIVE_SYMBOLS)
    out: dict[str, str] = {}
    for sym, cls in DUKA_CLASS.items():
        if sym in live or sym not in DUKASCOPY_INSTRUMENTS:
            continue
        out[sym] = cls
    for sym, cls in BINANCE_CANDIDATES.items():
        if sym not in live:
            out[sym] = cls
    if quick:
        keep = {
            "US500",
            "NAS100",
            "BRENT",
            "CL",
            "SOLUSDT",
            "LINKUSDT",
            "USDJPY",
        }
        out = {k: v for k, v in out.items() if k in keep}
    return out


def _sim_cfg(symbol: str, equity: float = 10_000.0) -> SimConfig:
    sym = symbol.upper()
    src = "binance" if sym.endswith("USDT") else "dukascopy"
    return SimConfig(
        source=src,
        data_start=DATA_START,
        sim_start=pd.Timestamp(DATA_START, tz="UTC"),
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


def preload_raw(symbols: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        if sym.endswith("USDT"):
            out[sym] = fetch_binance_daily(sym, DATA_START, None)
        else:
            out[sym] = fetch_source_data(_sim_cfg(sym))
    return out


def strat_from_kw(asset_class: str, **kw: Any) -> StrategyConfig:
    base = live_strategy_config("ETHUSDT")
    hmin = int(kw["hold_min"])
    hmax = int(kw["hold_max"])
    return replace(
        base,
        lookback=int(kw["lookback"]),
        buffer_bps=float(kw["buffer_bps"]),
        max_breakout_bps=float(kw.get("max_breakout_bps", GRID_MAX_BPS)),
        hold_days=hmin,
        hold_min=hmin,
        hold_max=hmax,
        dynamic_hold=hmax > hmin,
        trend_mode=str(kw["trend_mode"]),
        fee_bps=float(kw.get("fee_bps", FEE_BPS.get(asset_class, 10.0))),
        trail_atr=0.0,
        compound=True,
    )


class SimCache:
    def __init__(self) -> None:
        self._ind: dict[tuple[str, int, float, float, str], pd.DataFrame] = {}

    def run(
        self,
        raw: pd.DataFrame,
        symbol: str,
        strat: StrategyConfig,
        equity: float = 10_000.0,
    ) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
        key = (symbol, strat.lookback, strat.buffer_bps, strat.max_breakout_bps or 0.0, strat.trend_mode)
        if key not in self._ind:
            self._ind[key] = add_indicators(raw, strat)
        df = self._ind[key]
        sim = _sim_cfg(symbol, equity)
        trades, curve, summary = simulate_account(df, sim_cfg=sim, strat_cfg=strat)
        return trades, curve, summary


def window_pnl(trades: pd.DataFrame, start: pd.Timestamp | None, end: pd.Timestamp | None) -> pd.Series:
    if trades.empty:
        return pd.Series(dtype=float)
    t = trades.copy()
    t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
    if start is not None:
        t = t[t["exit_date"] >= start]
    if end is not None:
        t = t[t["exit_date"] < end]
    return pd.to_numeric(t["net_pnl"], errors="coerce")


def solo_stats(trades: pd.DataFrame, summary: dict[str, Any], equity: float) -> dict[str, Any]:
    pnls = window_pnl(trades, None, None)
    ex = window_pnl(trades, EX_2024, None)
    y24 = window_pnl(trades, pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2025-01-01", tz="UTC"))
    y25 = window_pnl(trades, pd.Timestamp("2025-01-01", tz="UTC"), pd.Timestamp("2026-01-01", tz="UTC"))
    years = 1.0
    if not trades.empty:
        t = trades.copy()
        t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
        years = max((t["exit_date"].max() - t["exit_date"].min()).days / 365.25, 1e-9)

    def pack(s: pd.Series) -> dict[str, float]:
        if s.empty:
            return {"trades": 0, "pnl": 0.0, "pf": float("nan")}
        return {
            "trades": int(len(s)),
            "pnl": float(s.sum()),
            "pf": float(profit_factor(s)),
        }

    return {
        "trades": int(len(pnls)),
        "trades_per_year": round(len(pnls) / years, 2),
        "return_pct_full": float(summary.get("return_pct", 0.0)),
        "max_dd_pct": float(summary.get("max_drawdown_pct", float("nan"))),
        "cagr_pct": float(summary.get("cagr_pct", 0.0)),
        "pf_full": float(profit_factor(pnls)) if len(pnls) else float("nan"),
        "full": pack(pnls),
        "ex_2024": pack(ex),
        "y2024": pack(y24),
        "y2025": pack(y25),
        "score_ex_2024": float(ex.sum()) * (float(profit_factor(ex)) if len(ex) else 0.0),
    }


def coarse_pass(
    cache: SimCache,
    raw: pd.DataFrame,
    symbol: str,
    asset_class: str,
) -> tuple[dict[str, Any], StrategyConfig, dict[str, Any]]:
    names = TEMPLATE_BY_CLASS.get(asset_class, ("metal", "crypto"))
    best_kw: dict[str, Any] | None = None
    best_stats: dict[str, Any] | None = None
    best_strat: StrategyConfig | None = None
    for name in names:
        kw = dict(COARSE_TEMPLATES[name])
        kw["fee_bps"] = FEE_BPS.get(asset_class, 10.0)
        strat = strat_from_kw(asset_class, **kw)
        tr, _, summ = cache.run(raw, symbol, strat)
        st = solo_stats(tr, summ, 10_000.0)
        if best_stats is None or st["score_ex_2024"] > best_stats["score_ex_2024"]:
            best_stats, best_kw, best_strat = st, {**kw, "template": name}, strat
    assert best_stats is not None and best_kw is not None and best_strat is not None
    return best_kw, best_strat, best_stats


def deep_grid(
    cache: SimCache,
    raw: pd.DataFrame,
    symbol: str,
    asset_class: str,
    seed_kw: dict[str, Any],
) -> tuple[dict[str, Any], StrategyConfig, dict[str, Any]]:
    best_stats: dict[str, Any] | None = None
    best_kw: dict[str, Any] | None = None
    best_strat: StrategyConfig | None = None
    fee = FEE_BPS.get(asset_class, 10.0)
    for lb, buf, (hmin, hmax), trend in itertools.product(
        GRID_LOOKBACKS, GRID_BUFFERS, GRID_HOLDS, GRID_TRENDS
    ):
        kw = {
            "lookback": lb,
            "buffer_bps": buf,
            "hold_min": hmin,
            "hold_max": hmax,
            "trend_mode": trend,
            "max_breakout_bps": GRID_MAX_BPS,
            "fee_bps": fee,
        }
        strat = strat_from_kw(asset_class, **kw)
        tr, _, summ = cache.run(raw, symbol, strat)
        st = solo_stats(tr, summ, 10_000.0)
        if best_stats is None or st["score_ex_2024"] > best_stats["score_ex_2024"]:
            best_stats, best_kw, best_strat = st, kw, strat
    assert best_stats is not None and best_kw is not None and best_strat is not None
    return best_kw, best_strat, best_stats


def equity_returns(curve: pd.DataFrame, start: pd.Timestamp) -> pd.Series:
    if curve.empty:
        return pd.Series(dtype=float)
    c = curve.copy()
    c["date"] = pd.to_datetime(c["date"], utc=True)
    s = c.set_index("date")["equity"].astype(float).sort_index()
    s = s[s.index >= start]
    return s.pct_change().dropna()


def book_tests(
    raw_live: dict[str, pd.DataFrame],
    baseline: dict[str, Any],
    base_strats: dict[str, StrategyConfig],
    base_eq: dict[str, float],
    symbol: str,
    strat: StrategyConfig,
    raw_cand: pd.DataFrame,
) -> dict[str, Any]:
    live = tuple(LIVE_SYMBOLS)
    out: dict[str, Any] = {}
    # add 7th
    sym7 = live + (symbol,)
    raw7 = {**raw_live, symbol: raw_cand}
    eq7 = {**base_eq, symbol: 10_000.0}
    st7 = {**base_strats, symbol: strat}
    m7 = run_portfolio(raw7, sym7, st7, eq7)
    out["add_7th"] = {**m7, "passes": beats_or_ties_baseline(baseline, m7)}
    # swaps
    for drop in SWAP_TARGETS:
        if drop not in live:
            continue
        sym_s = tuple(s for s in live if s != drop) + (symbol,)
        raw_s = {k: v for k, v in raw_live.items() if k != drop}
        raw_s[symbol] = raw_cand
        eq_s = {k: v for k, v in base_eq.items() if k != drop}
        eq_s[symbol] = 10_000.0
        st_s = {k: v for k, v in base_strats.items() if k != drop}
        st_s[symbol] = strat
        ms = run_portfolio(raw_s, sym_s, st_s, eq_s)
        out[f"replace_{drop}"] = {**ms, "passes": beats_or_ties_baseline(baseline, ms)}
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="Smaller symbol set")
    ap.add_argument("--top-grid", type=int, default=10, help="How many symbols get full param grid")
    args = ap.parse_args()

    universe = build_universe(args.quick)
    live = tuple(LIVE_SYMBOLS)
    print(f"Universe: {len(universe)} candidates | live book: {len(live)} sleeves", flush=True)

    print("Loading live book…", flush=True)
    raw_live = preload_raw(live)
    base_strats = {s: live_strategy_config(s) for s in live}
    base_eq = {s: float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in live}
    baseline = run_portfolio(raw_live, live, base_strats, base_eq)
    print(
        f"Baseline: ret={baseline['return_pct']:.1f}% PF={baseline['profit_factor']:.2f} "
        f"DD={baseline['max_drawdown_pct']:.2f}%",
        flush=True,
    )

    cache = SimCache()
    phase1: list[dict[str, Any]] = []

    print("\n=== Phase 1: coarse templates ===", flush=True)
    for sym, asset_class in universe.items():
        try:
            raw = preload_raw((sym,))[sym]
        except Exception as exc:
            print(f"  {sym}: SKIP load ({exc})", flush=True)
            continue
        if len(raw) < MIN_BARS:
            print(f"  {sym}: SKIP bars={len(raw)}", flush=True)
            continue
        kw, strat, st = coarse_pass(cache, raw, sym, asset_class)
        print(
            f"  {sym:8} [{asset_class:12}] tpl={kw.get('template')} "
            f"ex24 PF={st['ex_2024']['pf']:.2f} PnL=${st['ex_2024']['pnl']:,.0f} "
            f"full ret={st['return_pct_full']:.1f}%",
            flush=True,
        )
        phase1.append(
            {
                "symbol": sym,
                "asset_class": asset_class,
                "coarse_kw": kw,
                "coarse_stats": st,
                "raw_bars": len(raw),
            }
        )

    phase1.sort(key=lambda r: r["coarse_stats"]["score_ex_2024"], reverse=True)
    top_syms = [r["symbol"] for r in phase1[: args.top_grid]]

    print(f"\n=== Phase 2: param grid on top {len(top_syms)} ===", flush=True)
    optimized: dict[str, dict[str, Any]] = {}
    for sym in top_syms:
        rec = next(r for r in phase1 if r["symbol"] == sym)
        raw = preload_raw((sym,))[sym]
        kw, strat, st = deep_grid(cache, raw, sym, rec["asset_class"], rec["coarse_kw"])
        print(
            f"  {sym:8} best lb={kw['lookback']} buf={kw['buffer_bps']:.0f} "
            f"hold={kw['hold_min']}-{kw['hold_max']} {kw['trend_mode']} | "
            f"ex24 PF={st['ex_2024']['pf']:.2f} PnL=${st['ex_2024']['pnl']:,.0f}",
            flush=True,
        )
        optimized[sym] = {"params": kw, "stats": st, "strat": strat}

    print("\n=== Phase 3: book integration + correlation ===", flush=True)
    live_rets = {}
    for s in live:
        tr, cu, _ = cache.run(raw_live[s], s, base_strats[s])
        live_rets[s] = equity_returns(cu, EX_2024)
    port_live = pd.concat(live_rets.values(), axis=1).mean(axis=1)

    final_rows: list[dict[str, Any]] = []
    for sym in top_syms:
        opt = optimized[sym]
        strat = opt["strat"]
        raw = preload_raw((sym,))[sym]
        tr, cu, _ = cache.run(raw, sym, strat)
        cand_ret = equity_returns(cu, EX_2024)
        corr = float(cand_ret.corr(port_live)) if not cand_ret.empty and not port_live.empty else float("nan")
        books = book_tests(raw_live, baseline, base_strats, base_eq, sym, strat, raw)
        st = opt["stats"]
        final_rows.append(
            {
                "symbol": sym,
                "asset_class": next(r["asset_class"] for r in phase1 if r["symbol"] == sym),
                "params": opt["params"],
                "solo": st,
                "corr_ex_2024_vs_book": corr,
                "book": books,
                "pass_count": sum(1 for k, v in books.items() if v.get("passes")),
            }
        )
        print(
            f"  {sym:8} corr={corr:.2f} passes={final_rows[-1]['pass_count']}/"
            f"{len(books)} | swapXCU={'Y' if books.get('replace_XCUUSD', {}).get('passes') else 'n'} "
            f"swapBTC={'Y' if books.get('replace_BTCUSD', {}).get('passes') else 'n'}",
            flush=True,
        )

    final_rows.sort(
        key=lambda r: (
            r["pass_count"],
            r["solo"]["ex_2024"]["pf"] if pd.notna(r["solo"]["ex_2024"]["pf"]) else 0,
            -abs(r["corr_ex_2024_vs_book"]) if pd.notna(r["corr_ex_2024_vs_book"]) else 0,
            r["solo"]["ex_2024"]["pnl"],
        ),
        reverse=True,
    )

    payload = {
        "baseline_6_sleeve": baseline,
        "universe_size": len(universe),
        "phase1_ranked": [
            {
                "symbol": r["symbol"],
                "asset_class": r["asset_class"],
                "coarse_template": r["coarse_kw"].get("template"),
                "ex_2024_pnl": r["coarse_stats"]["ex_2024"]["pnl"],
                "ex_2024_pf": r["coarse_stats"]["ex_2024"]["pf"],
                "score": r["coarse_stats"]["score_ex_2024"],
            }
            for r in phase1
        ],
        "deep_optimized_top": final_rows,
        "recommendations": [
            "Prefer pass_count>=1 on replace_XCUUSD or replace_BTCUSD with |corr|<0.5 vs book.",
            "BRENT/energy and US500/index add asset-class diversity; SOL/ADA add crypto beta.",
            "Always paper-test 2–3 months before promoting to LIVE_SYMBOLS.",
        ],
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {OUT_PATH}", flush=True)
    print("\nTOP PICKS (deep)", flush=True)
    for r in final_rows[:8]:
        ex = r["solo"]["ex_2024"]
        print(
            f"  {r['symbol']:8} ex24 PF={ex['pf']:.2f} PnL=${ex['pnl']:,.0f} "
            f"corr={r['corr_ex_2024_vs_book']:.2f} book_passes={r['pass_count']}"
        )


if __name__ == "__main__":
    main()
