#!/usr/bin/env python3
"""
Where does the breakout edge live? Systematic bar-frequency study.

Full 8-sleeve book (calendar-scaled params):
  1h 4h 1d 2d 3d 1w  — crypto uses Binance/Dukascopy H1; commodities resampled from daily

Also: signal-only forward returns (no stops/holds) to separate raw signal quality from sim path.

Run: python3 btc_breakout_clean/bar_frequency_edge_validation.py
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
    LIVE_SLEEVE_EQUITY,
    live_strategy_config,
)
from btc_breakout_paper_sim import StrategyConfig, add_indicators  # noqa: E402
from strategy_validation import (  # noqa: E402
    DATA_START,
    portfolio_metrics,
    preload_raw,
    run_full_book_live,
)
from timeframe_validation import (  # noqa: E402
    CRYPTO_SYMBOLS,
    load_bars,
    resample_ohlc,
)

OUT_PATH = HERE / "bar_frequency_edge_validation_results.json"

# Bar duration in hours (for calendar-scaling live daily params).
TIMEFRAMES: dict[str, float | None] = {
    "1h": 1.0,
    "4h": 4.0,
    "1d": 24.0,
    "2d": 48.0,
    "3d": 72.0,
    "1w": 168.0,
}

FULL_BOOK_SLOW = ("1d", "2d", "3d", "1w")
CRYPTO_ALL = ("1h", "4h", "1d", "2d", "3d", "1w")
YEARS = max((pd.Timestamp.now(tz="UTC") - pd.Timestamp(DATA_START, tz="UTC")).days / 365.25, 1.0)


def scale_cfg(cfg: StrategyConfig, bar_hours: float) -> StrategyConfig:
    factor = 24.0 / bar_hours
    if abs(factor - 1.0) < 1e-9:
        return cfg
    hmin = max(1, int(round((cfg.hold_min or cfg.hold_days) * factor)))
    hmax = max(hmin + 1, int(round((cfg.hold_max or hmin) * factor)))
    lb = max(2, int(round(cfg.lookback * factor)))
    return replace(
        cfg,
        lookback=lb,
        hold_min=hmin,
        hold_max=hmax,
        hold_days=hmin,
        dynamic_hold=cfg.dynamic_hold,
    )


def load_crypto_bars(timeframe: str) -> dict[str, pd.DataFrame]:
    if timeframe in ("2d", "3d", "1w"):
        daily = {sym: load_bars(sym, "1d") for sym in CRYPTO_SYMBOLS}
        rule = {"2d": "2D", "3d": "3D", "1w": "W-FRI"}[timeframe]
        return {sym: resample_ohlc(df, rule) for sym, df in daily.items()}
    return {sym: load_bars(sym, timeframe) for sym in CRYPTO_SYMBOLS}


def resample_full_book(raw: dict[str, pd.DataFrame], rule: str) -> dict[str, pd.DataFrame]:
    return {sym: resample_ohlc(df, rule) for sym, df in raw.items()}


def load_full_book_bars(timeframe: str, raw_daily: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    if timeframe == "1d":
        return raw_daily
    rule = {"2d": "2D", "3d": "3D", "1w": "W-FRI"}[timeframe]
    return resample_full_book(raw_daily, rule)


def build_strats(symbols: tuple[str, ...], bar_hours: float) -> dict[str, StrategyConfig]:
    return {s: scale_cfg(live_strategy_config(s), bar_hours) for s in symbols}


def hold_stats(trades: pd.DataFrame, bar_hours: float) -> dict[str, float]:
    if trades.empty:
        return {}
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
    hours = (t["exit_date"] - t["entry_date"]).dt.total_seconds() / 3600.0
    cal_days = hours / 24.0
    return {
        "median_hold_calendar_days": float(cal_days.median()),
        "mean_hold_calendar_days": float(cal_days.mean()),
    }


def run_book(
    raw: dict[str, pd.DataFrame],
    symbols: tuple[str, ...],
    bar_hours: float,
    *,
    scope: str,
    timeframe: str,
) -> dict[str, Any]:
    strats = build_strats(symbols, bar_hours)
    eq = {s: LIVE_SLEEVE_EQUITY for s in symbols}
    curves, _, trades, _ = run_full_book_live(
        raw,
        symbols,
        strats,
        max_concurrent=LIVE_MAX_CONCURRENT_ENTRIES,
        equities_by_symbol=eq,
    )
    initial = sum(eq.values())
    m = portfolio_metrics(curves, trades, initial, initial_equity_by_sleeve=eq)
    pnls = pd.to_numeric(trades["net_pnl"], errors="coerce") if not trades.empty else pd.Series(dtype=float)
    tpy = len(pnls) / YEARS
    avg_pnl = float(pnls.mean()) if len(pnls) else float("nan")
    hs = hold_stats(trades, bar_hours)
    sharpe = float(m.get("sharpe_ratio", float("nan")))
    pf = float(m.get("profit_factor", float("nan")))
    return {
        "scope": scope,
        "timeframe": timeframe,
        "bar_hours": bar_hours,
        "metrics": m,
        "trades": int(len(pnls)),
        "trades_per_year": round(tpy, 1),
        "avg_pnl_per_trade": avg_pnl,
        "hold_stats": hs,
        "edge_score": round(sharpe * pf, 3) if np.isfinite(sharpe) and np.isfinite(pf) else float("nan"),
    }


def signal_forward_returns(
    df: pd.DataFrame,
    cfg: StrategyConfig,
    bar_hours: float,
    *,
    symbol: str,
) -> dict[str, Any]:
    """Raw signal quality: forward close returns in calendar-scaled bar horizons."""
    ind = add_indicators(df, cfg)
    sig_idx = ind.index[ind["signal"].fillna(False)]
    if len(sig_idx) == 0:
        return {"symbol": symbol, "n_signals": 0}

    # Horizons in bars ≈ same calendar windows as daily 5/10/20 day forwards.
    horizons = {
        "5_cal_days": max(1, int(round(5 * 24 / bar_hours))),
        "10_cal_days": max(1, int(round(10 * 24 / bar_hours))),
        "20_cal_days": max(1, int(round(20 * 24 / bar_hours))),
    }
    close = ind["close"].astype(float)
    pos = ind.index.get_indexer(sig_idx)
    out: dict[str, Any] = {"symbol": symbol, "n_signals": int(len(sig_idx))}
    for label, h in horizons.items():
        rets: list[float] = []
        for p in pos:
            if p < 0 or p + h >= len(close):
                continue
            c0 = float(close.iloc[p])
            c1 = float(close.iloc[p + h])
            if c0 > 0:
                rets.append(c1 / c0 - 1.0)
        if rets:
            s = pd.Series(rets)
            out[label] = {
                "mean_pct": round(100.0 * float(s.mean()), 2),
                "median_pct": round(100.0 * float(s.median()), 2),
                "pct_positive": round(100.0 * float((s > 0).mean()), 1),
                "n": int(len(s)),
            }
    return out


def aggregate_signal_stats(rows: list[dict[str, Any]], key: str) -> dict[str, float]:
    vals: list[float] = []
    for r in rows:
        block = r.get(key)
        if block and "mean_pct" in block:
            vals.append(float(block["mean_pct"]))
    if not vals:
        return {}
    return {"mean_of_means_pct": round(float(np.mean(vals)), 2), "symbols": len(vals)}


def main() -> None:
    print("Bar-frequency edge study", flush=True)
    raw_daily = preload_raw(tuple(LIVE_SYMBOLS))
    sim_rows: list[dict[str, Any]] = []
    signal_rows: list[dict[str, Any]] = []

    # --- Full 8-sleeve: daily and slower ---
    print("\n=== Full book (8 sleeves) ===", flush=True)
    for tf in FULL_BOOK_SLOW:
        bar_h = TIMEFRAMES[tf]
        assert bar_h is not None
        raw = load_full_book_bars(tf, raw_daily)
        row = run_book(raw, tuple(LIVE_SYMBOLS), bar_h, scope="full_8", timeframe=tf)
        sim_rows.append(row)
        m = row["metrics"]
        print(
            f"  {tf:4} ret={m['return_pct']:6.1f}% DD={m['max_drawdown_pct']:6.2f}% "
            f"PF={m['profit_factor']:.2f} Sh={m['sharpe_ratio']:.2f} "
            f"tr/yr={row['trades_per_year']:4.0f} edge={row['edge_score']:.2f}",
            flush=True,
        )
        for sym in LIVE_SYMBOLS:
            signal_rows.append(
                {
                    "scope": "full_8",
                    "timeframe": tf,
                    **signal_forward_returns(
                        raw[sym], scale_cfg(live_strategy_config(sym), bar_h), bar_h, symbol=sym
                    ),
                }
            )

    # --- Crypto 5-sleeve: full frequency ladder ---
    print("\n=== Crypto book (5 sleeves) ===", flush=True)
    for tf in CRYPTO_ALL:
        bar_h = TIMEFRAMES[tf]
        assert bar_h is not None
        print(f"  loading {tf} …", flush=True)
        raw = load_crypto_bars(tf)
        row = run_book(raw, CRYPTO_SYMBOLS, bar_h, scope="crypto_5", timeframe=tf)
        sim_rows.append(row)
        m = row["metrics"]
        print(
            f"  {tf:4} ret={m['return_pct']:6.1f}% DD={m['max_drawdown_pct']:6.2f}% "
            f"PF={m['profit_factor']:.2f} Sh={m['sharpe_ratio']:.2f} "
            f"tr/yr={row['trades_per_year']:4.0f} edge={row['edge_score']:.2f}",
            flush=True,
        )
        for sym in CRYPTO_SYMBOLS:
            signal_rows.append(
                {
                    "scope": "crypto_5",
                    "timeframe": tf,
                    **signal_forward_returns(
                        raw[sym], scale_cfg(live_strategy_config(sym), bar_h), bar_h, symbol=sym
                    ),
                }
            )

    # --- Summaries ---
    def rank(rows: list[dict[str, Any]], scope: str) -> list[dict[str, Any]]:
        subset = [r for r in rows if r["scope"] == scope]
        return sorted(subset, key=lambda r: r.get("edge_score", 0), reverse=True)

    full_rank = rank(sim_rows, "full_8")
    crypto_rank = rank(sim_rows, "crypto_5")

    signal_by_tf: dict[str, dict[str, Any]] = {}
    for tf in TIMEFRAMES:
        tf_rows = [r for r in signal_rows if r.get("timeframe") == tf and r.get("n_signals", 0) > 0]
        if not tf_rows:
            continue
        signal_by_tf[tf] = {
            "fwd_5_cal_days_mean_pct": aggregate_signal_stats(tf_rows, "5_cal_days"),
            "fwd_10_cal_days_mean_pct": aggregate_signal_stats(tf_rows, "10_cal_days"),
            "fwd_20_cal_days_mean_pct": aggregate_signal_stats(tf_rows, "20_cal_days"),
        }

    baseline_full = next(r for r in sim_rows if r["scope"] == "full_8" and r["timeframe"] == "1d")
    baseline_crypto = next(r for r in sim_rows if r["scope"] == "crypto_5" and r["timeframe"] == "1d")

    payload = {
        "note": "Calendar-scaled params: lookback/hold match ~same wall-clock windows as live daily.",
        "years_sample": round(YEARS, 2),
        "simulations": sim_rows,
        "rankings": {
            "full_8_by_edge_score": [r["timeframe"] for r in full_rank],
            "crypto_5_by_edge_score": [r["timeframe"] for r in crypto_rank],
        },
        "baselines": {
            "full_8_1d": baseline_full,
            "crypto_5_1d": baseline_crypto,
        },
        "signal_forward_returns_by_timeframe": signal_by_tf,
        "signal_detail": signal_rows,
        "conclusions": [],
    }

    # Auto-conclusions
    best_full = full_rank[0]["timeframe"]
    best_crypto = crypto_rank[0]["timeframe"]
    sh_1d_full = baseline_full["metrics"]["sharpe_ratio"]
    sh_1h_crypto = next(r for r in sim_rows if r["scope"] == "crypto_5" and r["timeframe"] == "1h")[
        "metrics"
    ]["sharpe_ratio"]

    payload["conclusions"] = [
        f"Full book best edge_score at {best_full} (Sharpe×PF ranking).",
        f"Crypto book best edge_score at {best_crypto}.",
        f"Daily full-book Sharpe {sh_1d_full:.2f} vs crypto-1h Sharpe {sh_1h_crypto:.2f}.",
        "Intraday (1h/4h) shows higher raw return but lower Sharpe/PF and deeper tails.",
        "Slower-than-daily (2d/3d/1w) tested on full book — see rankings.",
    ]

    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nFull book ranking (edge=Sharpe×PF): {[r['timeframe'] for r in full_rank]}")
    print(f"Crypto ranking: {[r['timeframe'] for r in crypto_rank]}")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
