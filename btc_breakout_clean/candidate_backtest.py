#!/usr/bin/env python3
"""
Backtest top candidate sleeves vs live 7-sleeve book (fair capital scenarios).

Uses optimized params from candidate_assets_deep_screen_results.json when present.

  python3 btc_breakout_clean/candidate_backtest.py
  python3 btc_breakout_clean/candidate_backtest.py --symbols US500,SOLUSDT

Writes: btc_breakout_clean/candidate_backtest_results.json
"""

from __future__ import annotations

import argparse
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
    live_symbol_equity,
    live_strategy_config,
)
from btc_breakout_paper_sim import (  # noqa: E402
    SimConfig,
    StrategyConfig,
    add_indicators,
    default_skip_saturday_entry,
    dukascopy_cache_path,
    fetch_source_data,
    max_drawdown,
    profit_factor,
    simulate_account,
)
from strategy_validation import (  # noqa: E402
    DATA_START,
    beats_baseline,
    blocked_entries_max_concurrent,
    drawdown_recovery_days,
    portfolio_equity_series,
    preload_raw as preload_live_raw,
    run_full_book,
    run_sleeve,
    sleeve_window_metrics,
)

OUT_PATH = HERE / "candidate_backtest_results.json"
DEEP_PATH = HERE / "candidate_assets_deep_screen_results.json"
EXTENSIVE_DEFAULT_PARAMS: dict[str, dict[str, Any]] = {
    "DOLLAR": {"source": "dukascopy", "lookback": 20, "buffer_bps": 75.0, "hold_min": 9, "hold_max": 15, "trend_mode": "sma200_95", "fee_bps": 2.0, "equity": 10_000.0},
    "COFFEE": {"source": "dukascopy", "lookback": 30, "buffer_bps": 100.0, "hold_min": 9, "hold_max": 15, "trend_mode": "sma200_95", "fee_bps": 5.0, "equity": 10_000.0},
    "XPTUSD": {"source": "dukascopy", "lookback": 30, "buffer_bps": 100.0, "hold_min": 9, "hold_max": 15, "trend_mode": "sma200_95", "fee_bps": 2.0, "equity": 10_000.0},
}
EX_2024 = pd.Timestamp("2024-01-01", tz="UTC")

# Fallback params if deep screen JSON missing
DEFAULT_CANDIDATE_PARAMS: dict[str, dict[str, Any]] = {
    "US500": {
        "source": "dukascopy",
        "lookback": 15,
        "buffer_bps": 100.0,
        "max_breakout_bps": 225.0,
        "hold_min": 9,
        "hold_max": 15,
        "trend_mode": "sma200_95",
        "fee_bps": 2.0,
        "equity": 10_000.0,
    },
    "NAS100": {
        "source": "dukascopy",
        "lookback": 10,
        "buffer_bps": 150.0,
        "max_breakout_bps": 225.0,
        "hold_min": 4,
        "hold_max": 5,
        "trend_mode": "bull_only",
        "fee_bps": 2.0,
        "equity": 10_000.0,
    },
    "SOLUSDT": {
        "source": "binance",
        "lookback": 15,
        "buffer_bps": 100.0,
        "max_breakout_bps": 225.0,
        "hold_min": 9,
        "hold_max": 15,
        "trend_mode": "sma200_95",
        "fee_bps": 10.0,
        "equity": 10_000.0,
    },
    "LINKUSDT": {
        "source": "binance",
        "lookback": 20,
        "buffer_bps": 150.0,
        "max_breakout_bps": 225.0,
        "hold_min": 6,
        "hold_max": 10,
        "trend_mode": "sma200_95",
        "fee_bps": 10.0,
        "equity": 10_000.0,
    },
}

WINDOWS: list[tuple[str, pd.Timestamp | None, pd.Timestamp | None]] = [
    ("full", None, None),
    ("ex_2024", EX_2024, None),
    ("y2024", pd.Timestamp("2024-01-01", tz="UTC"), pd.Timestamp("2025-01-01", tz="UTC")),
    ("y2025", pd.Timestamp("2025-01-01", tz="UTC"), pd.Timestamp("2026-01-01", tz="UTC")),
]


def _sim_cfg(symbol: str, equity: float, source: str) -> SimConfig:
    sym = symbol.upper()
    return SimConfig(
        source=source,
        data_start=DATA_START,
        sim_start=pd.Timestamp(DATA_START, tz="UTC"),
        end=None,
        equity=equity,
        include_current=False,
        cache_path=Path(""),
        dukascopy_path=dukascopy_cache_path(sym) if source == "dukascopy" else Path(""),
        refresh_cache=False,
        show_trades=0,
        write_files=False,
        out_dir=Path("."),
        instrument=sym,
        skip_saturday_entry=default_skip_saturday_entry(source),
    )


def load_candidate_params() -> dict[str, dict[str, Any]]:
    out = {k: dict(v) for k, v in DEFAULT_CANDIDATE_PARAMS.items()}
    for k, v in EXTENSIVE_DEFAULT_PARAMS.items():
        out.setdefault(k, dict(v))
    if not DEEP_PATH.exists():
        return out
    deep = json.loads(DEEP_PATH.read_text(encoding="utf-8"))
    for row in deep.get("deep_optimized_top", []):
        sym = str(row["symbol"]).upper()
        p = row.get("params", {})
        src = "binance" if sym.endswith("USDT") else "dukascopy"
        out[sym] = {
            "source": src,
            "lookback": int(p["lookback"]),
            "buffer_bps": float(p["buffer_bps"]),
            "max_breakout_bps": float(p.get("max_breakout_bps", 225.0)),
            "hold_min": int(p["hold_min"]),
            "hold_max": int(p["hold_max"]),
            "trend_mode": str(p["trend_mode"]),
            "fee_bps": float(p.get("fee_bps", 2.0 if src == "dukascopy" else 10.0)),
            "equity": 10_000.0,
        }
    return out


def strategy_from_params(params: dict[str, Any]) -> StrategyConfig:
    base = live_strategy_config("ETHUSDT")
    hold_min = int(params["hold_min"])
    hold_max = int(params["hold_max"])
    return replace(
        base,
        lookback=int(params["lookback"]),
        buffer_bps=float(params["buffer_bps"]),
        max_breakout_bps=float(params["max_breakout_bps"]),
        trend_mode=str(params["trend_mode"]),
        hold_days=hold_min,
        hold_min=hold_min,
        hold_max=hold_max,
        dynamic_hold=hold_max > hold_min,
        fee_bps=float(params["fee_bps"]),
        trail_atr=0.0,
        compound=True,
    )


def preload_candidate(symbol: str, params: dict[str, Any]) -> pd.DataFrame:
    sym = symbol.upper()
    if params["source"] == "binance":
        return fetch_binance_daily(sym, DATA_START, None)
    return fetch_source_data(_sim_cfg(sym, float(params["equity"]), "dukascopy"))


def portfolio_metrics(
    curves: dict[str, pd.DataFrame],
    trades: pd.DataFrame,
    equities: dict[str, float],
) -> dict[str, Any]:
    initial = sum(equities.values())
    port = portfolio_equity_series(curves, equities)
    if port.empty:
        return {
            "return_pct": 0.0,
            "max_drawdown_pct": float("nan"),
            "profit_factor": float("nan"),
            "trades": 0,
            "initial_equity": initial,
            "final_equity": initial,
            "net_profit": 0.0,
        }
    final = float(port.iloc[-1])
    pnls = pd.to_numeric(trades["net_pnl"], errors="coerce") if not trades.empty else pd.Series(dtype=float)
    pf = profit_factor(pnls) if len(pnls) else float("nan")
    return {
        "return_pct": 100.0 * (final / initial - 1.0),
        "max_drawdown_pct": 100.0 * max_drawdown(port),
        "profit_factor": float(pf) if pd.notna(pf) else float("nan"),
        "trades": int(len(pnls)),
        "initial_equity": initial,
        "final_equity": final,
        "net_profit": final - initial,
    }


def solo_backtest(
    raw: pd.DataFrame,
    symbol: str,
    strat: StrategyConfig,
    equity: float,
    source: str,
) -> dict[str, Any]:
    tr, cu, summary = run_sleeve(raw, symbol, strat, equity, None)
    port_eq = (
        cu.set_index(pd.to_datetime(cu["date"], utc=True))["equity"].astype(float) if not cu.empty else pd.Series(dtype=float)
    )
    windows = {
        label: sleeve_window_metrics(tr, equity, start, end) for label, start, end in WINDOWS
    }
    return {
        "symbol": symbol,
        "equity": equity,
        "source": source,
        "params": {
            "lookback": strat.lookback,
            "buffer_bps": strat.buffer_bps,
            "hold_min": strat.hold_min,
            "hold_max": strat.hold_max,
            "trend_mode": strat.trend_mode,
            "fee_bps": strat.fee_bps,
        },
        "full_summary": summary,
        "max_drawdown_pct": float(summary.get("max_drawdown_pct", float("nan"))),
        "dd_recovery_days": drawdown_recovery_days(port_eq),
        "windows": windows,
        "trade_count": int(len(tr)),
    }


def run_book_scenario(
    raw_cache: dict[str, pd.DataFrame],
    symbols: tuple[str, ...],
    strategies: dict[str, StrategyConfig],
    equities: dict[str, float],
    *,
    label: str,
    max_concurrent: int | None = 4,
    raw_for_blocked: dict[str, pd.DataFrame] | None = None,
) -> dict[str, Any]:
    sim_ov: dict[str, dict[str, Any]] | None = None
    if max_concurrent is not None and max_concurrent > 0:
        blocked_raw = raw_for_blocked or raw_cache
        strats_live = {s: live_strategy_config(s) for s in LIVE_SYMBOLS}
        _, _, base_trades, _ = run_full_book(blocked_raw, LIVE_SYMBOLS, strats_live)
        blocked = blocked_entries_max_concurrent(base_trades, max_concurrent)
        # extend blocked map for non-live symbols
        sim_ov = {s: {"blocked_entry_dates": blocked.get(s, frozenset())} for s in symbols}

    curves, _, trades, _ = run_full_book(raw_cache, symbols, strategies, sim_ov)
    m = portfolio_metrics(curves, trades, equities)
    return {"label": label, "symbols": list(symbols), "equities": equities, **m}


def book_scenarios_for_candidate(
    raw_live: dict[str, pd.DataFrame],
    baseline_metrics: dict[str, Any],
    symbol: str,
    strat: StrategyConfig,
    raw_cand: pd.DataFrame,
    cand_equity: float,
) -> list[dict[str, Any]]:
    live = tuple(LIVE_SYMBOLS)
    base_strats = {s: live_strategy_config(s) for s in live}
    base_eq = {s: live_symbol_equity(s, 10_000.0) for s in live}
    raw_all = {**raw_live, symbol: raw_cand}
    btc_eq = live_symbol_equity("BTCUSD", 10_000.0)
    doge_eq = live_symbol_equity("DOGEUSDT", 10_000.0)

    scenarios: list[dict[str, Any]] = []

    def tag(row: dict[str, Any]) -> dict[str, Any]:
        row["passes_baseline"] = beats_baseline(baseline_metrics, row)
        row["delta_return_pp"] = row["return_pct"] - baseline_metrics["return_pct"]
        row["delta_profit_usd"] = row["net_profit"] - baseline_metrics["net_profit"]
        row["delta_dd_pp"] = row["max_drawdown_pct"] - baseline_metrics["max_drawdown_pct"]
        return row

    # 8th sleeve +$10k capital
    sym8 = live + (symbol,)
    eq8 = {**base_eq, symbol: cand_equity}
    st8 = {**base_strats, symbol: strat}
    scenarios.append(
        tag(
            run_book_scenario(
                raw_all,
                sym8,
                st8,
                eq8,
                label=f"add_{symbol}_10k",
                raw_for_blocked=raw_live,
            )
        )
    )

    # Fair swap: BTC $5k -> candidate $5k (same $65k book)
    sym_fair = tuple(s for s in live if s != "BTCUSD") + (symbol,)
    eq_fair = {k: v for k, v in base_eq.items() if k != "BTCUSD"}
    eq_fair[symbol] = btc_eq
    st_fair = {k: v for k, v in base_strats.items() if k != "BTCUSD"}
    st_fair[symbol] = strat
    raw_fair = {k: v for k, v in raw_live.items() if k != "BTCUSD"}
    raw_fair[symbol] = raw_cand
    scenarios.append(
        tag(
            run_book_scenario(
                raw_fair,
                sym_fair,
                st_fair,
                eq_fair,
                label=f"replace_BTCUSD_fair_{int(btc_eq // 1000)}k",
                raw_for_blocked=raw_live,
            )
        )
    )

    # Deep-screen style: BTC out, candidate $10k ($70k book)
    sym_10 = tuple(s for s in live if s != "BTCUSD") + (symbol,)
    eq_10 = {k: v for k, v in base_eq.items() if k != "BTCUSD"}
    eq_10[symbol] = cand_equity
    st_10 = {k: v for k, v in base_strats.items() if k != "BTCUSD"}
    st_10[symbol] = strat
    scenarios.append(
        tag(
            run_book_scenario(
                raw_fair | {symbol: raw_cand},
                sym_10,
                st_10,
                eq_10,
                label=f"replace_BTCUSD_{int(cand_equity)}k",
                raw_for_blocked=raw_live,
            )
        )
    )

    # Replace DOGE $10k for $10k
    sym_d = tuple(s for s in live if s != "DOGEUSDT") + (symbol,)
    eq_d = {k: v for k, v in base_eq.items() if k != "DOGEUSDT"}
    eq_d[symbol] = doge_eq
    st_d = {k: v for k, v in base_strats.items() if k != "DOGEUSDT"}
    st_d[symbol] = strat
    raw_d = {k: v for k, v in raw_live.items() if k != "DOGEUSDT"}
    raw_d[symbol] = raw_cand
    scenarios.append(
        tag(
            run_book_scenario(
                raw_d,
                sym_d,
                st_d,
                eq_d,
                label=f"replace_DOGEUSDT_{int(doge_eq)}k",
                raw_for_blocked=raw_live,
            )
        )
    )

    return scenarios


def symbols_from_deep_screen() -> list[str]:
    if not DEEP_PATH.exists():
        return []
    deep = json.loads(DEEP_PATH.read_text(encoding="utf-8"))
    syms: list[str] = []
    for row in deep.get("deep_optimized_top", []):
        sym = str(row.get("symbol", "")).upper()
        if sym and sym not in syms:
            syms.append(sym)
    return syms


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--symbols",
        default="",
        help="Comma-separated candidate symbols (default: from deep screen JSON)",
    )
    ap.add_argument(
        "--from-deep",
        action="store_true",
        help="Use all symbols from candidate_assets_deep_screen_results.json",
    )
    args = ap.parse_args()
    if args.from_deep or not args.symbols.strip():
        candidates = symbols_from_deep_screen()
        if not candidates:
            candidates = [s.strip().upper() for s in "US500,SOLUSDT,NAS100,LINKUSDT".split(",")]
    else:
        candidates = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    cand_params = load_candidate_params()

    print("Loading live book…", flush=True)
    raw_live = preload_live_raw(tuple(LIVE_SYMBOLS))
    live_strats = {s: live_strategy_config(s) for s in LIVE_SYMBOLS}
    live_eq = {s: live_symbol_equity(s, 10_000.0) for s in LIVE_SYMBOLS}

    baseline = run_book_scenario(
        raw_live,
        LIVE_SYMBOLS,
        live_strats,
        live_eq,
        label="baseline_7_live",
    )
    print(
        f"Baseline: ret={baseline['return_pct']:.2f}% profit=${baseline['net_profit']:,.0f} "
        f"DD={baseline['max_drawdown_pct']:.2f}% PF={baseline['profit_factor']:.3f} trades={baseline['trades']}",
        flush=True,
    )

    solo_rows: list[dict[str, Any]] = []
    book_by_candidate: dict[str, Any] = {}

    for sym in candidates:
        if sym not in cand_params:
            print(f"SKIP {sym}: no params", flush=True)
            continue
        params = cand_params[sym]
        print(f"\n=== {sym} solo ===", flush=True)
        raw = preload_candidate(sym, params)
        strat = strategy_from_params(params)
        eq = float(params["equity"])
        solo = solo_backtest(raw, sym, strat, eq, str(params["source"]))
        ex = solo["windows"]["ex_2024"]
        print(
            f"  full ret={solo['full_summary'].get('return_pct', 0):.1f}% DD={solo['max_drawdown_pct']:.1f}% "
            f"| ex-2024 PF={ex.get('profit_factor', float('nan')):.2f} trades={ex.get('trades', 0)} "
            f"PnL=${ex.get('net_pnl', 0):,.0f}",
            flush=True,
        )
        solo_rows.append(solo)

        print(f"=== {sym} book scenarios (max 4 concurrent) ===", flush=True)
        scenarios = book_scenarios_for_candidate(raw_live, baseline, sym, strat, raw, eq)
        for sc in scenarios:
            print(
                f"  {sc['label']:32} ret={sc['return_pct']:6.2f}% Δret={sc['delta_return_pp']:+5.2f}pp "
                f"profit=${sc['net_profit']:,.0f} Δ$={sc['delta_profit_usd']:+,.0f} "
                f"DD={sc['max_drawdown_pct']:.2f}% pass={sc['passes_baseline']}",
                flush=True,
            )
        book_by_candidate[sym] = {"scenarios": scenarios}

    payload = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "baseline": baseline,
        "solo": solo_rows,
        "book": book_by_candidate,
        "notes": [
            "replace_BTCUSD_fair_5k: same $65k as baseline (swap $5k BTC for $5k candidate).",
            "replace_BTCUSD_10k: $70k book (deep-screen convention).",
            "add_*_10k: $75k book. Compare profit $ and fair-swap return %.",
        ],
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
