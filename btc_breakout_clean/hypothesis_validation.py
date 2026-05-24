#!/usr/bin/env python3
"""
Run the full hypothesis test queue from strategy research (May 2026).

  python3 btc_breakout_clean/hypothesis_validation.py

Writes: btc_breakout_clean/hypothesis_validation_results.json
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
    LIVE_STRATEGY_PARAMS,
    LIVE_SYMBOLS,
    live_symbol_equity,
    live_symbol_source,
    live_strategy_config,
)
from btc_breakout_paper_sim import StrategyConfig, add_indicators  # noqa: E402
from signal_forecast import flat_signal_horizon_stats, load_daily_bars  # noqa: E402
from strategy_validation import (  # noqa: E402
    beats_baseline,
    blocked_entries_max_concurrent,
    portfolio_metrics,
    preload_raw,
    run_full_book,
    run_full_book_live,
    run_sleeve,
    sleeve_window_metrics,
)

OUT_PATH = HERE / "hypothesis_validation_results.json"
EX_2024 = pd.Timestamp("2024-01-01", tz="UTC")
CRYPTO = ("BTCUSD", "ETHUSDT", "BNBUSDT", "DOGEUSDT")


def _default_equities(symbols: tuple[str, ...]) -> dict[str, float]:
    return {s: live_symbol_equity(s, 10_000.0) for s in symbols}


def run_book(
    raw: dict[str, pd.DataFrame],
    symbols: tuple[str, ...],
    strategies: dict[str, StrategyConfig] | None = None,
    equities: dict[str, float] | None = None,
    sim_overrides: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    syms = tuple(symbols)
    strats = strategies or {s: live_strategy_config(s) for s in syms}
    eq = equities or _default_equities(syms)
    curves, _, trades, _ = run_full_book_live(raw, syms, strats, sim_overrides)
    initial = sum(eq.values())
    metrics = portfolio_metrics(curves, trades, initial)
    ex = sleeve_window_metrics(trades, initial, EX_2024, None) if not trades.empty else {}
    return {
        "symbols": list(syms),
        "initial_equity": initial,
        "metrics": metrics,
        "ex_2024": ex,
        "per_sleeve_trades": {
            s: int((trades["sleeve"] == s).sum()) if not trades.empty and "sleeve" in trades.columns else 0
            for s in syms
        },
    }


def tag(result: dict[str, Any], baseline: dict[str, float]) -> dict[str, Any]:
    m = result["metrics"]
    result["passes_baseline"] = beats_baseline(baseline, m)
    return result


def h1_book_composition(raw: dict[str, pd.DataFrame], baseline_m: dict[str, float]) -> dict[str, Any]:
    live = tuple(LIVE_SYMBOLS)
    rows = {
        "H1_baseline_8": run_book(raw, live),
        "H1_drop_xcu": run_book(raw, tuple(s for s in live if s != "XCUUSD")),
        "H1_drop_doge": run_book(raw, tuple(s for s in live if s != "DOGEUSDT")),
        "H1_drop_brent": run_book(raw, tuple(s for s in live if s != "BRENT")),
        "H1_metals_crypto_no_xcu": run_book(
            raw,
            tuple(s for s in live if s not in ("XCUUSD",)),
        ),
    }
    for k, v in rows.items():
        tag(v, baseline_m)
    return rows


def h2_btc_weight(raw: dict[str, pd.DataFrame], baseline_m: dict[str, float]) -> dict[str, Any]:
    live = tuple(LIVE_SYMBOLS)
    eq_full = _default_equities(live)
    eq_half = {**eq_full, "BTCUSD": 5_000.0}
    no_btc = tuple(s for s in live if s != "BTCUSD")
    return {
        "H2_btc_full": tag(run_book(raw, live, equities=eq_full), baseline_m),
        "H2_btc_half_5k": tag(run_book(raw, live, equities=eq_half), baseline_m),
        "H2_btc_removed": tag(run_book(raw, no_btc), baseline_m),
    }


def h4_crypto_cap_175(raw: dict[str, pd.DataFrame], baseline_m: dict[str, float]) -> dict[str, Any]:
    live = tuple(LIVE_SYMBOLS)
    strats = {s: live_strategy_config(s) for s in live}
    for s in CRYPTO:
        if s in strats:
            strats[s] = replace(strats[s], max_breakout_bps=175.0)
    return {"H4_crypto_cap_175bps": tag(run_book(raw, live, strategies=strats), baseline_m)}


def h5_xcu_shorter_hold(raw: dict[str, pd.DataFrame], baseline_m: dict[str, float]) -> dict[str, Any]:
    if "XCUUSD" not in LIVE_SYMBOLS:
        return {"H5_xcu_hold_3d_fixed": {"skipped": True, "reason": "XCUUSD not in LIVE_SYMBOLS"}}
    live = tuple(LIVE_SYMBOLS)
    strats = {s: live_strategy_config(s) for s in live}
    strats["XCUUSD"] = replace(
        strats["XCUUSD"],
        hold_days=3,
        hold_min=3,
        hold_max=3,
        dynamic_hold=False,
    )
    return {"H5_xcu_hold_3d_fixed": tag(run_book(raw, live, strategies=strats), baseline_m)}


def h6_brent_regime(raw: dict[str, pd.DataFrame]) -> dict[str, Any]:
    sym = "BRENT"
    eq = live_symbol_equity(sym, 10_000.0)
    rows: dict[str, Any] = {}
    for mode in ("sma200_95", "sma200_slope_up", "bull_only"):
        strat = replace(live_strategy_config(sym), trend_mode=mode)
        tr, _, summary = run_sleeve(raw[sym], sym, strat, eq)
        ex = sleeve_window_metrics(tr, eq, EX_2024, None)
        rows[f"H6_brent_{mode}"] = {
            "trend_mode": mode,
            "full_summary": summary,
            "ex_2024": ex,
            "trades": int(len(tr)),
        }
    return rows


def h10_max4_concurrent(raw: dict[str, pd.DataFrame], baseline_m: dict[str, float]) -> dict[str, Any]:
    live = tuple(LIVE_SYMBOLS)
    strats = {s: live_strategy_config(s) for s in live}
    _, _, base_trades, _ = run_full_book(raw, live, strats)
    blocked = blocked_entries_max_concurrent(base_trades, 4)
    overrides = {s: {"blocked_entry_dates": blocked[s]} for s in live}
    result = run_book(raw, live, strategies=strats, sim_overrides=overrides)
    result["blocked_entry_events"] = sum(len(v) for v in blocked.values())
    return {"H10_max_4_concurrent": tag(result, baseline_m)}


def h11_hwm_btc_xcu_only(raw: dict[str, pd.DataFrame], baseline_m: dict[str, float]) -> dict[str, Any]:
    live = tuple(LIVE_SYMBOLS)
    strats = {s: live_strategy_config(s) for s in live}
    overrides = {s: {} for s in live}
    for s in ("BTCUSD", "XCUUSD"):
        overrides[s] = {"hwm_pause_pct": 12.0}
    return {"H11_hwm_12pct_btc_xcu_only": tag(run_book(raw, live, strategies=strats, sim_overrides=overrides), baseline_m)}


def h12_paper_vs_replay(raw: dict[str, pd.DataFrame]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for sym in LIVE_SYMBOLS:
        paper_dir = HERE / "paper_portfolio" / sym
        paper_path = paper_dir / "trades.csv"
        if not paper_path.exists():
            rows[sym] = {"status": "no_paper_trades"}
            continue
        paper = pd.read_csv(paper_path)
        eq = live_symbol_equity(sym, 10_000.0)
        strat = live_strategy_config(sym)
        replay_tr, _, _ = run_sleeve(raw[sym], sym, strat, eq)
        if paper.empty or replay_tr.empty:
            rows[sym] = {"status": "empty", "paper_n": len(paper), "replay_n": len(replay_tr)}
            continue
        p = paper.copy()
        r = replay_tr.copy()
        p["entry_date"] = pd.to_datetime(p["entry_date"], utc=True).dt.normalize()
        r["entry_date"] = pd.to_datetime(r["entry_date"], utc=True).dt.normalize()
        p_entries = set(p["entry_date"].astype(str))
        r_entries = set(r["entry_date"].astype(str))
        overlap = len(p_entries & r_entries)
        rows[sym] = {
            "paper_trades": len(p),
            "replay_trades": len(r),
            "entry_overlap": overlap,
            "entry_match_pct": 100.0 * overlap / max(len(r_entries), 1),
            "paper_only": len(p_entries - r_entries),
            "replay_only": len(r_entries - p_entries),
        }
    return {"H12_paper_vs_replay": rows}


def h13_p7d_predictive_power(raw: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Bucket historical flat setups by eventual days-to-signal; check next-trade win rate."""
    buckets: dict[str, list[float]] = {f"signal_within_{d}d": [] for d in (3, 7, 14, 30)}
    sleeve_rows: dict[str, Any] = {}

    for sym in LIVE_SYMBOLS:
        strat = live_strategy_config(sym)
        df = add_indicators(raw[sym], strat)
        tr, _, _ = run_sleeve(raw[sym], sym, strat, live_symbol_equity(sym, 10_000.0))
        if tr.empty:
            sleeve_rows[sym] = {"status": "no_trades"}
            continue
        t = tr.copy()
        t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
        wins: list[float] = []
        for _, trade in t.iterrows():
            entry = trade["entry_date"]
            idx = df.index.get_indexer([entry], method="nearest")[0]
            if idx < 0 or idx >= len(df):
                continue
            gap = float(strat.buffer_bps) - float(df["breakout_bps"].iloc[idx])
            if not np.isfinite(gap) or gap < 0:
                gap = 50.0
            h = flat_signal_horizon_stats(df, strat, gap_bps=gap)
            p7 = h.get("prob_7d")
            if p7 is not None:
                wins.append((p7, 1.0 if float(trade["net_pnl"]) > 0 else 0.0))
        if len(wins) < 5:
            sleeve_rows[sym] = {"status": "low_sample", "n": len(wins)}
            continue
        arr = np.array(wins)
        hi = arr[arr[:, 0] >= np.median(arr[:, 0])]
        lo = arr[arr[:, 0] < np.median(arr[:, 0])]
        sleeve_rows[sym] = {
            "n": len(wins),
            "win_rate_high_p7_median": float(hi[:, 1].mean()) if len(hi) else None,
            "win_rate_low_p7_median": float(lo[:, 1].mean()) if len(lo) else None,
            "spread_pp": 100.0 * (float(hi[:, 1].mean()) - float(lo[:, 1].mean())) if len(hi) and len(lo) else None,
        }

    return {"H13_p7_vs_trade_outcome": sleeve_rows}


def summarize_passes(results: dict[str, Any], baseline_m: dict[str, float]) -> list[dict[str, Any]]:
    promoted: list[dict[str, Any]] = []
    for hid, block in results.items():
        if not hid.startswith("H"):
            continue
        if isinstance(block, dict) and "metrics" in block:
            items = [(hid, block)]
        else:
            items = [(f"{hid}/{k}", v) for k, v in block.items() if isinstance(v, dict)]
        for label, row in items:
            if "metrics" not in row:
                continue
            m = row["metrics"]
            promoted.append(
                {
                    "id": label,
                    "return_pct": m["return_pct"],
                    "pf": m["profit_factor"],
                    "max_dd_pct": m["max_drawdown_pct"],
                    "trades": m["trades"],
                    "ex_2024_pf": row.get("ex_2024", {}).get("profit_factor"),
                    "passes": row.get("passes_baseline", False),
                }
            )
    promoted.sort(key=lambda x: (not x["passes"], -x["return_pct"]))
    return promoted


def main() -> None:
    symbols = tuple(LIVE_SYMBOLS)
    print(f"Preloading {len(symbols)} sleeves…", flush=True)
    raw = preload_raw(symbols)
    print("Running baseline book…", flush=True)
    baseline = run_book(raw, symbols)
    baseline_m = baseline["metrics"]

    results: dict[str, Any] = {
        "generated_at": pd.Timestamp.utcnow().isoformat(),
        "baseline": baseline,
        "hypotheses": {},
    }

    print("H1 book composition…", flush=True)
    results["hypotheses"]["H1"] = h1_book_composition(raw, baseline_m)

    print("H2 BTC weight…", flush=True)
    results["hypotheses"]["H2"] = h2_btc_weight(raw, baseline_m)

    print("H4 crypto cap 175…", flush=True)
    results["hypotheses"]["H4"] = h4_crypto_cap_175(raw, baseline_m)

    print("H5 XCU shorter hold…", flush=True)
    results["hypotheses"]["H5"] = h5_xcu_shorter_hold(raw, baseline_m)

    print("H6 BRENT regimes…", flush=True)
    results["hypotheses"]["H6"] = h6_brent_regime(raw)

    print("H10 max 4 concurrent…", flush=True)
    results["hypotheses"]["H10"] = h10_max4_concurrent(raw, baseline_m)

    print("H11 HWM BTC+XCU…", flush=True)
    results["hypotheses"]["H11"] = h11_hwm_btc_xcu_only(raw, baseline_m)

    print("H12 paper vs replay…", flush=True)
    results["hypotheses"]["H12"] = h12_paper_vs_replay(raw)

    print("H13 P(7d) vs outcomes…", flush=True)
    results["hypotheses"]["H13"] = h13_p7d_predictive_power(raw)

    results["ranking"] = summarize_passes(results["hypotheses"], baseline_m)
    results["recommendations"] = _recommendations(results, baseline_m)

    OUT_PATH.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    _print_summary(results, baseline_m)
    print(f"\nFull report: {OUT_PATH}")


def _recommendations(results: dict[str, Any], baseline_m: dict[str, float]) -> list[str]:
    recs: list[str] = []
    h1 = results["hypotheses"].get("H1", {})
    if h1.get("H1_drop_xcu", {}).get("passes_baseline"):
        recs.append("PROMOTE: Drop XCUUSD — passes return/PF/DD gate vs baseline.")
    elif h1.get("H1_drop_xcu", {}).get("metrics", {}).get("profit_factor", 0) > baseline_m["profit_factor"]:
        recs.append("CONSIDER: Drop XCU improves PF; check ex-2024 before promoting.")

    h2 = results["hypotheses"].get("H2", {})
    if h2.get("H2_btc_removed", {}).get("passes_baseline"):
        recs.append("PROMOTE: Remove BTC sleeve — book stats improve with gate.")
    if h2.get("H2_btc_half_5k", {}).get("passes_baseline"):
        recs.append("PROMOTE: Half-size BTC ($5k) — acceptable compromise.")

    if results["hypotheses"].get("H4", {}).get("H4_crypto_cap_175bps", {}).get("passes_baseline"):
        recs.append("PROMOTE: Tighten crypto max_breakout_bps to 175.")

    h6 = results["hypotheses"].get("H6", {})
    best_brent = max(
        ((k, v) for k, v in h6.items() if "ex_2024" in v),
        key=lambda kv: float(kv[1]["ex_2024"].get("profit_factor") or 0),
        default=("none", {}),
    )
    if best_brent[0] != "H6_brent_sma200_95":
        recs.append(f"TEST LIVE: BRENT regime {best_brent[0]} beats sma200_95 on ex-2024 PF.")

    h13 = results["hypotheses"].get("H13", {}).get("H13_p7_vs_trade_outcome", {})
    spreads = [v.get("spread_pp") for v in h13.values() if isinstance(v, dict) and v.get("spread_pp") is not None]
    if spreads and np.nanmean(spreads) < 10:
        recs.append("HOLD: P(7d) forecast not predictive for sizing — dashboard display only.")

    if not recs:
        recs.append("No hypothesis passed full gate; keep current 8-sleeve book; continue paper tracking.")
    recs.append("Run: python3 btc_breakout_clean/dynamic_hold_validation.py for H8 (dynamic hold).")
    return recs


def _print_summary(results: dict[str, Any], baseline_m: dict[str, float]) -> None:
    print("\n=== BASELINE (8 sleeves) ===")
    print(
        f"  ret={baseline_m['return_pct']:.1f}%  PF={baseline_m['profit_factor']:.2f}  "
        f"DD={baseline_m['max_drawdown_pct']:.2f}%  trades={baseline_m['trades']}"
    )
    print("\n=== PASS GATE (return/PF/DD) ===")
    for row in results["ranking"]:
        if row["passes"]:
            print(
                f"  ✓ {row['id']}: ret={row['return_pct']:.1f}% PF={row['pf']:.2f} "
                f"DD={row['max_dd_pct']:.2f}%"
            )
    print("\n=== RECOMMENDATIONS ===")
    for line in results["recommendations"]:
        print(f"  • {line}")


if __name__ == "__main__":
    main()
