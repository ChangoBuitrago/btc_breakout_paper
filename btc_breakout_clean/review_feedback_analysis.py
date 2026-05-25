#!/usr/bin/env python3
"""
Analyses requested in external technical review (May 2026).

Run: python3 btc_breakout_clean/review_feedback_analysis.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import (  # noqa: E402
    LIVE_MAX_CONCURRENT_ENTRIES,
    LIVE_SYMBOLS,
    fetch_binance_daily,
    live_strategy_config,
)
from btc_breakout_paper_sim import add_indicators, max_drawdown, profit_factor  # noqa: E402
from strategy_validation import (  # noqa: E402
    DATA_START,
    annualized_vol_and_sharpe,
    cagr,
    portfolio_equity_series,
    portfolio_metrics,
    preload_raw,
    run_full_book_live,
)

OUT_PATH = HERE / "review_feedback_analysis_results.json"
CAP_BPS = 225.0
HOLDOUT_START = pd.Timestamp("2022-01-01", tz="UTC")


def bootstrap_pf_ci(pnls: pd.Series, n_boot: int = 5000, seed: int = 42) -> dict[str, float]:
    """Bootstrap 95% CI for profit factor (resample trades with replacement)."""
    p = pnls.dropna().astype(float)
    if len(p) < 5:
        return {"n": int(len(p)), "pf": float("nan"), "ci_low": float("nan"), "ci_high": float("nan")}
    rng = np.random.default_rng(seed)
    pfs: list[float] = []
    arr = p.values
    for _ in range(n_boot):
        samp = rng.choice(arr, size=len(arr), replace=True)
        wins = float(samp[samp > 0].sum())
        losses = float(samp[samp < 0].sum())
        if losses < 0:
            pfs.append(wins / abs(losses))
    pfs_arr = np.array(pfs)
    return {
        "n": int(len(p)),
        "pf": float(profit_factor(p)),
        "ci_low": float(np.percentile(pfs_arr, 2.5)),
        "ci_high": float(np.percentile(pfs_arr, 97.5)),
    }


def exhaustion_cap_cohort(raw: dict[str, pd.DataFrame]) -> dict[str, Any]:
    """Raw breakout + regime but breakout_bps > cap — trades we skip."""
    rows: list[dict[str, Any]] = []
    for sym in LIVE_SYMBOLS:
        strat = live_strategy_config(sym)
        df = add_indicators(raw[sym], strat)
        raw_br = df["close"] > df["prior_high"] * (1.0 + strat.buffer_bps / 10_000.0)
        regime = df["regime_on"] if "regime_on" in df.columns else df["signal"]
        stretched = df["breakout_bps"] > CAP_BPS
        would_signal = raw_br & regime.fillna(False) & stretched.fillna(False)
        n_stretched = int(would_signal.sum())
        if n_stretched == 0:
            rows.append({"symbol": sym, "n_stretched_signals": 0})
            continue
        sub = df.loc[would_signal]
        fwd1 = df["close"].shift(-1) / df["close"] - 1.0
        fwd5 = df["close"].shift(-5) / df["close"] - 1.0
        fwd10 = df["close"].shift(-10) / df["close"] - 1.0
        f5 = fwd5.loc[would_signal].dropna()
        trim = f5
        if len(f5) >= 20:
            cutoff = float(f5.quantile(0.95))
            trim = f5[f5 <= cutoff]
        rows.append(
            {
                "symbol": sym,
                "n_stretched_signals": n_stretched,
                "median_breakout_bps": float(sub["breakout_bps"].median()),
                "mean_fwd1_pct": float(fwd1.loc[would_signal].mean() * 100),
                "mean_fwd5_pct": float(f5.mean() * 100) if len(f5) else float("nan"),
                "median_fwd5_pct": float(f5.median() * 100) if len(f5) else float("nan"),
                "mean_fwd5_pct_trim_top5pct": float(trim.mean() * 100) if len(trim) else float("nan"),
                "mean_fwd10_pct": float(fwd10.loc[would_signal].mean() * 100),
                "pct_fwd5_positive": float((f5 > 0).mean() * 100) if len(f5) else float("nan"),
            }
        )
    return {
        "per_symbol": rows,
        "note": (
            "Preview only: forward close-to-close from signal bar. "
            "Not trade PnL (no stops, holds, fees, next-open entry). "
            "Full cap sweep required before changing live cap."
        ),
    }


def utilization_stats(
    curves: dict[str, pd.DataFrame],
    trades: pd.DataFrame,
    initial_total: float,
) -> dict[str, Any]:
    """Deployed notional proxy: sum of open position entry_notional over time."""
    if trades.empty:
        return {}
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
    t["entry_notional"] = pd.to_numeric(t["entry_notional"], errors="coerce")

    sleeve_eq = {s: float(initial_total / len(curves)) for s in curves}
    port = portfolio_equity_series(curves, sleeve_eq)
    if port.empty:
        return {}

    deployed = pd.Series(0.0, index=port.index)
    open_sleeves = pd.Series(0.0, index=port.index)
    for row in t.itertuples(index=False):
        mask = (deployed.index >= row.entry_date) & (deployed.index <= row.exit_date)
        deployed.loc[mask] += float(row.entry_notional)
        open_sleeves.loc[mask] += 1.0

    util = deployed / port.replace(0, np.nan)
    in_market = (deployed > 0).astype(float)
    return {
        "mean_deployed_usd": float(deployed.mean()),
        "max_deployed_usd": float(deployed.max()),
        "mean_utilization_vs_book_equity": float(util.mean()),
        "max_utilization_vs_book_equity": float(util.max()),
        "pct_days_with_any_position": float(in_market.mean() * 100),
        "mean_concurrent_open_sleeves": float(open_sleeves[open_sleeves > 0].mean()) if (open_sleeves > 0).any() else 0.0,
        "max_concurrent_open_sleeves": float(open_sleeves.max()),
        "pct_days_at_max_concurrent_cap": float((open_sleeves >= LIVE_MAX_CONCURRENT_ENTRIES).mean() * 100),
        "initial_book_usd": initial_total,
        "note": "Behaves as serial single-sleeve trader most days; max-4 cap rarely binds.",
    }


def btc_signal_drift(raw_duk: pd.DataFrame) -> dict[str, Any]:
    """Compare signal dates: Dukascopy BTCUSD vs Binance BTCUSDT."""
    try:
        raw_bin = fetch_binance_daily("BTCUSDT", DATA_START, None)
    except Exception as exc:
        return {"error": str(exc)}
    strat = live_strategy_config("BTCUSD")
    d_duk = add_indicators(raw_duk, strat)
    d_bin = add_indicators(raw_bin, strat)
    sig_duk = set(d_duk.index[d_duk["signal"].fillna(False)])
    sig_bin = set(d_bin.index[d_bin["signal"].fillna(False)])
    only_duk = sig_duk - sig_bin
    only_bin = sig_bin - sig_duk
    both = sig_duk & sig_bin
    # ±1 day fuzzy match
    matched_fuzzy = 0
    for d in only_duk:
        for off in (-1, 1):
            neighbor = d + pd.Timedelta(days=off)
            if neighbor in sig_bin:
                matched_fuzzy += 1
                break
    return {
        "dukascopy_signals": len(sig_duk),
        "binance_signals": len(sig_bin),
        "exact_match": len(both),
        "only_dukascopy": len(only_duk),
        "only_binance": len(only_bin),
        "only_duk_with_neighbor_on_binance": matched_fuzzy,
        "pct_exact_agreement": 100.0 * len(both) / max(len(sig_duk | sig_bin), 1),
    }


def calendar_year_pnl(trades: pd.DataFrame) -> dict[str, Any]:
    """Net PnL by entry calendar year (USD)."""
    if trades.empty:
        return {}
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    t["net_pnl"] = pd.to_numeric(t["net_pnl"], errors="coerce")
    t["year"] = t["entry_date"].dt.year
    by_sym = (
        t.groupby(["year", "sleeve"])["net_pnl"]
        .sum()
        .unstack(fill_value=0.0)
        .round(2)
    )
    book = t.groupby("year")["net_pnl"].sum().round(2)
    counts = t.groupby("year").size()
    rows = []
    for yr in sorted(book.index):
        row: dict[str, Any] = {"year": int(yr), "trades": int(counts[yr]), "book_net_pnl_usd": float(book[yr])}
        for sym in LIVE_SYMBOLS:
            if sym in by_sym.columns:
                row[sym] = float(by_sym.loc[yr, sym])
        rows.append(row)
    return {
        "note": "PnL attributed to entry year; live replay max 4 concurrent.",
        "by_year": rows,
    }


def holdout_2022(
    curves: dict[str, pd.DataFrame],
    trades: pd.DataFrame,
    equities: dict[str, float],
) -> dict[str, Any]:
    """Equity-path holdout from 2022-01-01 using the same book aggregation as validation."""
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
    ho = t.loc[t["entry_date"] >= HOLDOUT_START]
    pnls = pd.to_numeric(ho["net_pnl"], errors="coerce")

    port = portfolio_equity_series(curves, equities)
    if port.empty:
        return {"error": "empty portfolio equity series"}

    port_full_dd = 100.0 * max_drawdown(port)
    port_ho = port.loc[port.index >= HOLDOUT_START].copy()
    if len(port_ho) < 2:
        return {"error": "insufficient equity path in holdout window"}

    start_eq = float(port_ho.iloc[0])
    end_eq = float(port_ho.iloc[-1])
    ret = end_eq / start_eq - 1.0
    dd_ho = 100.0 * max_drawdown(port_ho)
    years = max((port_ho.index[-1] - port_ho.index[0]).days / 365.25, 1e-9)
    cagr_ho = 100.0 * cagr(ret, port_ho.index[0], port_ho.index[-1])
    ann_vol, sharpe = annualized_vol_and_sharpe(port_ho)

    # Trade PnL in window vs equity at holdout start (sanity check)
    pnl_sum = float(pnls.sum())
    ret_from_trades_vs_start = 100.0 * pnl_sum / start_eq if start_eq else float("nan")

    per_sleeve: dict[str, Any] = {}
    for sym in LIVE_SYMBOLS:
        sub = ho.loc[ho["sleeve"] == sym] if "sleeve" in ho.columns else ho
        per_sleeve[sym] = bootstrap_pf_ci(pd.to_numeric(sub["net_pnl"], errors="coerce"))

    return {
        "window": "2022-01-01 onward (calendar holdout; params frozen)",
        "equity_at_holdout_start_usd": start_eq,
        "equity_at_end_usd": end_eq,
        "trades_entered_in_window": int(len(pnls)),
        "return_pct_equity_path": 100.0 * ret,
        "return_pct_trade_pnl_over_start_equity": ret_from_trades_vs_start,
        "cagr_pct": cagr_ho,
        "max_drawdown_pct_holdout_window": dd_ho,
        "max_drawdown_pct_full_sample_book": port_full_dd,
        "profit_factor_trades_in_window": float(profit_factor(pnls)) if len(pnls) else float("nan"),
        "sharpe_ratio_holdout_equity": sharpe,
        "annualized_vol_pct_holdout": 100.0 * ann_vol if np.isfinite(ann_vol) else float("nan"),
        "per_sleeve_pf_bootstrap": per_sleeve,
        "caveats": [
            "2022–present is mostly bull regime for crypto/gold — not a flat/down stress window.",
            "Holdout DD is measured from 2022-01-01 book equity peak within the window, not from $100k inception.",
            "Regime overlap with tuning era remains; this is calendar separation, not regime-pure OOS.",
        ],
    }


def main() -> None:
    print("Review feedback analyses", flush=True)
    raw = preload_raw(tuple(LIVE_SYMBOLS))
    strats = {s: live_strategy_config(s) for s in LIVE_SYMBOLS}
    curves, _, trades, equities = run_full_book_live(raw, tuple(LIVE_SYMBOLS), strats)
    initial = sum(equities.values())
    full_metrics = portfolio_metrics(curves, trades, initial)

    stretched = exhaustion_cap_cohort(raw)
    util = utilization_stats(curves, trades, initial)
    holdout = holdout_2022(curves, trades, equities)
    cal_year = calendar_year_pnl(trades)

    # PF CI full sample per sleeve + book
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    pf_ci: dict[str, Any] = {"book": bootstrap_pf_ci(pd.to_numeric(t["net_pnl"], errors="coerce"))}
    ex24 = t.loc[t["entry_date"] >= pd.Timestamp("2024-01-01", tz="UTC")]
    pf_ci["book_ex_2024"] = bootstrap_pf_ci(pd.to_numeric(ex24["net_pnl"], errors="coerce"))
    for sym in LIVE_SYMBOLS:
        sub = t.loc[t["sleeve"] == sym] if "sleeve" in t.columns else pd.DataFrame()
        pf_ci[sym] = bootstrap_pf_ci(pd.to_numeric(sub["net_pnl"], errors="coerce"))
        sub_ex = ex24.loc[ex24["sleeve"] == sym] if "sleeve" in ex24.columns else pd.DataFrame()
        pf_ci[f"{sym}_ex_2024"] = bootstrap_pf_ci(pd.to_numeric(sub_ex["net_pnl"], errors="coerce"))

    btc_drift = btc_signal_drift(raw["BTCUSD"])

    # Funding stress: assume 0.01% per 8h on deployed crypto notional (conservative low bull)
    FUNDING_LOW = 0.0001
    FUNDING_BULL = 0.0005  # modal stress when long-only wins in bull regime
    crypto_syms = {"ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT"}
    t2 = trades.copy()
    t2["entry_date"] = pd.to_datetime(t2["entry_date"], utc=True)
    t2["exit_date"] = pd.to_datetime(t2["exit_date"], utc=True)
    def funding_drag(rate: float) -> float:
        drag = 0.0
        for row in t2.itertuples(index=False):
            if str(row.sleeve) not in crypto_syms:
                continue
            days = max((row.exit_date - row.entry_date).days, 1)
            drag += float(row.entry_notional) * rate * days * 3
        return drag

    drag_low = funding_drag(FUNDING_LOW)
    drag_bull = funding_drag(FUNDING_BULL)
    net_pnl = float(t2["net_pnl"].sum())

    payload = {
        "full_book_metrics": full_metrics,
        "exhaustion_cap_cohort": stretched,
        "utilization": util,
        "holdout_2022": holdout,
        "calendar_year_pnl": cal_year,
        "profit_factor_bootstrap_ci": pf_ci,
        "btc_dukascopy_vs_binance": btc_drift,
        "funding_stress_spot_assumption": {
            "note": "Paper uses spot daily bars; stress IF perps on crypto entry_notional over hold",
            "low_bull_rate_per_8h": FUNDING_LOW,
            "modal_bull_rate_per_8h": FUNDING_BULL,
            "drag_usd_low": drag_low,
            "drag_usd_modal_bull": drag_bull,
            "drag_pct_net_pnl_low": 100.0 * drag_low / net_pnl if net_pnl else float("nan"),
            "drag_pct_net_pnl_modal_bull": 100.0 * drag_bull / net_pnl if net_pnl else float("nan"),
        },
        "execution_venue": "spot_daily_klines_paper_only_not_perps",
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps(payload, indent=2, default=str))
    print(f"\nWrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
