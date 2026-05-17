#!/usr/bin/env python3
"""
Screen new sleeves for the paper book: solo stats + add-to-6-sleeve portfolio gate.

Run from repo root:
  python3 btc_breakout_clean/candidate_assets_screen.py

Requires Dukascopy cache under btc_breakout_clean/cache/ (or will download).
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
    fetch_binance_daily,
    live_symbol_source,
    live_strategy_config,
)
from btc_breakout_paper_sim import (  # noqa: E402
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

OUT_PATH = HERE / "candidate_assets_screen_results.json"
DATA_START = "2018-01-01"
EX_2024 = pd.Timestamp("2024-01-01", tz="UTC")

# symbol -> (asset class, data source hint)
CANDIDATES: dict[str, tuple[str, str]] = {
    "US500": ("equity_index", "dukascopy"),
    "NAS100": ("equity_index", "dukascopy"),
    "BRENT": ("energy", "dukascopy"),
    "CL": ("energy", "dukascopy"),
    "SOLUSDT": ("crypto_alt", "binance"),
    "LINKUSDT": ("crypto_alt", "binance"),
}

# Param templates to try per candidate (name -> StrategyConfig kwargs)
TEMPLATES: dict[str, dict[str, Any]] = {
    "crypto": {
        "lookback": 15,
        "buffer_bps": 125.0,
        "max_breakout_bps": 225.0,
        "hold_days": 6,
        "hold_min": 6,
        "hold_max": 10,
        "dynamic_hold": True,
        "trend_mode": "bull_only",
        "fee_bps": 10.0,
    },
    "metal": {
        "lookback": 30,
        "buffer_bps": 100.0,
        "max_breakout_bps": 225.0,
        "hold_days": 9,
        "hold_min": 9,
        "hold_max": 15,
        "dynamic_hold": True,
        "trend_mode": "bull_only",
        "fee_bps": 2.0,
    },
    "gold": {
        "lookback": 30,
        "buffer_bps": 100.0,
        "max_breakout_bps": 225.0,
        "hold_days": 9,
        "hold_min": 9,
        "hold_max": 15,
        "dynamic_hold": True,
        "trend_mode": "sma200_95",
        "fee_bps": 2.0,
    },
    "short": {
        "lookback": 15,
        "buffer_bps": 100.0,
        "max_breakout_bps": 225.0,
        "hold_days": 4,
        "hold_min": 4,
        "hold_max": 5,
        "dynamic_hold": True,
        "trend_mode": "bull_only",
        "fee_bps": 10.0,
    },
}

TEMPLATE_BY_CLASS = {
    "equity_index": ("metal", "gold", "crypto"),
    "energy": ("short", "metal", "crypto"),
    "crypto_alt": ("crypto", "metal"),
}


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


def make_strategy(template_name: str) -> StrategyConfig:
    base = live_strategy_config("ETHUSDT")
    t = TEMPLATES[template_name]
    return replace(
        base,
        lookback=int(t["lookback"]),
        buffer_bps=float(t["buffer_bps"]),
        max_breakout_bps=float(t["max_breakout_bps"]),
        hold_days=int(t["hold_days"]),
        hold_min=int(t["hold_min"]),
        hold_max=int(t["hold_max"]),
        dynamic_hold=bool(t["dynamic_hold"]),
        trend_mode=str(t["trend_mode"]),
        fee_bps=float(t["fee_bps"]),
        trail_atr=0.0,
        compound=True,
    )


def preload_raw(symbols: tuple[str, ...]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        if live_symbol_source(sym) == "binance" or sym.upper().endswith("USDT"):
            out[sym] = fetch_binance_daily(sym, DATA_START, None)
        else:
            out[sym] = fetch_source_data(_sim_cfg(sym))
    return out


def load_raw(symbol: str) -> pd.DataFrame:
    return preload_raw((symbol.upper(),))[symbol.upper()]


def solo_metrics(trades: pd.DataFrame, equity: float) -> dict[str, Any]:
    if trades.empty:
        return {
            "trades": 0,
            "trades_per_year": 0.0,
            "pf_full": float("nan"),
            "pf_ex_2024": float("nan"),
            "pnl_ex_2024": 0.0,
            "return_pct_ex_2024": 0.0,
        }
    t = trades.copy()
    t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
    pnls = pd.to_numeric(t["net_pnl"], errors="coerce")
    pf_full = float(profit_factor(pnls))
    ex = t[t["exit_date"] >= EX_2024]
    pnls_ex = pd.to_numeric(ex["net_pnl"], errors="coerce")
    pf_ex = float(profit_factor(pnls_ex)) if len(pnls_ex) else float("nan")
    years = max((t["exit_date"].max() - t["exit_date"].min()).days / 365.25, 1e-9)
    return {
        "trades": int(len(t)),
        "trades_per_year": round(len(t) / years, 2),
        "pf_full": pf_full,
        "pf_ex_2024": pf_ex,
        "pnl_ex_2024": float(pnls_ex.sum()) if len(pnls_ex) else 0.0,
        "return_pct_ex_2024": 100.0 * float(pnls_ex.sum()) / equity if len(pnls_ex) else 0.0,
    }


def run_solo(raw: pd.DataFrame, symbol: str, strat: StrategyConfig) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    sim = _sim_cfg(symbol)
    df = add_indicators(raw, strat)
    trades, curve, summary = simulate_account(df, sim_cfg=sim, strat_cfg=strat)
    m = solo_metrics(trades, sim.equity)
    m["return_pct_full"] = float(summary.get("return_pct", 0.0))
    m["max_dd_pct"] = float(summary.get("max_drawdown_pct", float("nan")))
    m["cagr_pct"] = float(summary.get("cagr_pct", 0.0))
    return m, trades, curve


def best_template_for_symbol(raw: pd.DataFrame, symbol: str, asset_class: str) -> tuple[str, dict[str, Any], StrategyConfig]:
    names = TEMPLATE_BY_CLASS.get(asset_class, ("metal", "crypto"))
    best_name = names[0]
    best_m: dict[str, Any] | None = None
    best_strat = make_strategy(best_name)
    for name in names:
        strat = make_strategy(name)
        m, _, _ = run_solo(raw, symbol, strat)
        score = (
            m.get("pf_ex_2024") if pd.notna(m.get("pf_ex_2024")) else 0.0,
            m.get("pnl_ex_2024", 0.0),
            m.get("return_pct_full", 0.0),
        )
        if best_m is None or score > (
            best_m.get("pf_ex_2024") if pd.notna(best_m.get("pf_ex_2024")) else 0.0,
            best_m.get("pnl_ex_2024", 0.0),
            best_m.get("return_pct_full", 0.0),
        ):
            best_m, best_name, best_strat = m, name, strat
    assert best_m is not None
    return best_name, best_m, best_strat


def main() -> None:
    live = tuple(LIVE_SYMBOLS)
    print(f"Loading live book ({len(live)} sleeves)…", flush=True)
    raw_live = preload_raw(live)
    base_strats = {s: live_strategy_config(s) for s in live}
    base_eq = {s: float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in live}
    baseline = run_portfolio(raw_live, live, base_strats, base_eq)
    print(f"Baseline 6-sleeve: ret={baseline['return_pct']:.1f}% PF={baseline['profit_factor']:.2f} DD={baseline['max_drawdown_pct']:.2f}%")

    results: list[dict[str, Any]] = []
    for sym, (asset_class, _src) in CANDIDATES.items():
        print(f"\n--- {sym} ({asset_class}) ---", flush=True)
        try:
            raw = load_raw(sym)
        except Exception as exc:
            print(f"  SKIP: data load failed: {exc}")
            results.append({"symbol": sym, "asset_class": asset_class, "error": str(exc)})
            continue
        if len(raw) < 400:
            print(f"  SKIP: only {len(raw)} bars")
            results.append({"symbol": sym, "asset_class": asset_class, "error": "insufficient_bars"})
            continue

        tpl_name, solo, strat = best_template_for_symbol(raw, sym, asset_class)
        print(
            f"  best template={tpl_name} | full ret={solo['return_pct_full']:.1f}% "
            f"PF={solo['pf_full']:.2f} | ex-2024 PF={solo['pf_ex_2024']:.2f} PnL=${solo['pnl_ex_2024']:,.0f} "
            f"trades/yr={solo['trades_per_year']}"
        )

        # Add 7th sleeve ($10k)
        sym7 = live + (sym,)
        eq7 = {**base_eq, sym: 10_000.0}
        st7 = {**base_strats, sym: strat}
        raw7 = {**raw_live, sym: raw}
        add7 = run_portfolio(raw7, sym7, st7, eq7)
        passes_add = beats_or_ties_baseline(baseline, add7)

        # Replace XCUUSD (weakest ex-2024 in validation)
        sym_swap = tuple(s for s in live if s != "XCUUSD") + (sym,)
        eq_swap = {k: v for k, v in base_eq.items() if k != "XCUUSD"}
        eq_swap[sym] = 10_000.0
        st_swap = {k: v for k, v in base_strats.items() if k != "XCUUSD"}
        st_swap[sym] = strat
        raw_swap = {k: v for k, v in raw_live.items() if k != "XCUUSD"}
        raw_swap[sym] = raw
        swap = run_portfolio(raw_swap, sym_swap, st_swap, eq_swap)
        passes_swap = beats_or_ties_baseline(baseline, swap)

        results.append(
            {
                "symbol": sym,
                "asset_class": asset_class,
                "template": tpl_name,
                "solo": solo,
                "strategy": {
                    "lookback": strat.lookback,
                    "buffer_bps": strat.buffer_bps,
                    "hold_min": strat.hold_min,
                    "hold_max": strat.hold_max,
                    "trend_mode": strat.trend_mode,
                    "dynamic_hold": strat.dynamic_hold,
                },
                "add_7th_sleeve": {**add7, "passes_baseline": passes_add},
                "replace_xcu": {**swap, "passes_baseline": passes_swap},
            }
        )

    # Rank: prefer passes_swap or passes_add, high ex-2024 PF, then solo pnl
    def rank_key(r: dict[str, Any]) -> tuple:
        if "error" in r:
            return (-1, 0, 0, 0)
        solo = r["solo"]
        pf = solo.get("pf_ex_2024")
        pf_v = float(pf) if pd.notna(pf) else 0.0
        return (
            int(r["replace_xcu"]["passes_baseline"]) + int(r["add_7th_sleeve"]["passes_baseline"]),
            pf_v,
            float(solo.get("pnl_ex_2024", 0)),
        )

    ranked = sorted(results, key=rank_key, reverse=True)
    payload = {
        "baseline_6_sleeve": baseline,
        "candidates": ranked,
        "recommendation_notes": [
            "Prioritize candidates with passes_baseline on replace_xcu (swap weak copper).",
            "Solo ex-2024 PF >= 1.5 and positive ex-2024 PnL before paper testing.",
            "Indices (US500/NAS100) diversify vs crypto/metals; energy/crypto_alts often correlate.",
        ],
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print("\n" + "=" * 72)
    print("RANKED CANDIDATES (best first)")
    print("=" * 72)
    for r in ranked:
        if "error" in r:
            print(f"  {r['symbol']}: ERROR — {r['error']}")
            continue
        s = r["solo"]
        print(
            f"  {r['symbol']:8} [{r['asset_class']}] tpl={r['template']} | "
            f"ex24 PF={s['pf_ex_2024']:.2f} PnL=${s['pnl_ex_2024']:,.0f} | "
            f"add7={'PASS' if r['add_7th_sleeve']['passes_baseline'] else 'fail'} "
            f"swapXCU={'PASS' if r['replace_xcu']['passes_baseline'] else 'fail'}"
        )
    print(f"\nWrote {OUT_PATH}")


if __name__ == "__main__":
    main()
