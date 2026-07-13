#!/usr/bin/env python3
"""
IBKR-vs-proxy basis study.

Quantifies how much the live IBKR feed differs from the proxy feeds
(Binance / Dukascopy / yfinance) the strategy was tuned on — BEFORE changing
any parameters. Answers: "does trading on proxy data mislead the breakout
strategy, and by how much?"

For each live sleeve it reports:
  1. OHLC basis        — median & p95 |IBKR/proxy − 1| in bps, per field
  2. Return tracking   — daily-return correlation + median |Δret| in bps
  3. SIGNAL divergence — how often the actual entry trigger (add_indicators
                         'signal', incl. regime + exhaustion cap) FLIPS between
                         feeds. This is the metric that matters for the algo.
  4. Entry-fill basis  — on IBKR entry days, next-open price diff in bps
                         (the slippage you'd misjudge by sizing on proxy)

Requires TWS / IB Gateway running (ibkr_data.fetch_ibkr_daily). If IBKR is
unreachable the script explains how to connect and exits without error.

Usage:
  python ibkr_basis_study.py                 # all live sleeves, from 2021-01-01
  python ibkr_basis_study.py BTCUSD XAUUSD   # subset
  python ibkr_basis_study.py --start 2022-01-01
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import (  # noqa: E402
    LIVE_SYMBOLS,
    fetch_binance_daily,
    live_binance_symbol,
    live_strategy_config,
    live_symbol_fallback_source,
    live_yfinance_ticker,
)
from btc_breakout_paper_sim import (  # noqa: E402
    add_indicators,
    dukascopy_cache_path,
    fetch_dukascopy_instrument,
    fetch_yfinance_daily,
    normalize_ohlc,
    yfinance_cache_path,
)


def fetch_proxy(symbol: str, source: str, start: str, end: str | None) -> pd.DataFrame:
    """Fetch the proxy feed directly (bypasses the ibkr source path)."""
    if source == "binance":
        return normalize_ohlc(fetch_binance_daily(live_binance_symbol(symbol), start, end))
    if source == "dukascopy":
        return fetch_dukascopy_instrument(
            symbol, dukascopy_cache_path(symbol), start, end,
            include_current=False, refresh_cache=False,
        )
    if source == "yfinance":
        tkr = live_yfinance_ticker(symbol)
        return fetch_yfinance_daily(tkr, start, end, False, yfinance_cache_path(tkr), False)
    raise ValueError(f"Unknown proxy source: {source}")


def _bps(a: pd.Series, b: pd.Series) -> pd.Series:
    """Absolute relative difference of a vs b, in basis points."""
    return (a / b - 1.0).abs() * 10_000.0


def study_symbol(symbol: str, start: str, end: str | None) -> dict | None:
    from ibkr_data import fetch_ibkr_daily

    fallback = live_symbol_fallback_source(symbol)
    try:
        ibkr = fetch_ibkr_daily(symbol, start, end, include_current=False, refresh_cache=False)
    except Exception as exc:
        print(f"  {symbol}: IBKR fetch failed — {exc}")
        return None

    try:
        proxy = fetch_proxy(symbol, fallback, start, end)
    except Exception as exc:
        print(f"  {symbol}: proxy ({fallback}) fetch failed — {exc}")
        return None

    # Normalise both to UTC-midnight index, intersect on common days.
    ibkr.index = pd.to_datetime(ibkr.index, utc=True).normalize()
    proxy.index = pd.to_datetime(proxy.index, utc=True).normalize()
    common = ibkr.index.intersection(proxy.index)
    if len(common) < 60:
        print(f"  {symbol}: only {len(common)} overlapping days — skipping")
        return None

    ib = ibkr.loc[common]
    px = proxy.loc[common]

    # 1. OHLC basis
    ohlc_basis = {f: (_bps(ib[f], px[f])) for f in ("open", "high", "low", "close")}

    # 2. Return tracking
    ib_ret = ib["close"].pct_change()
    px_ret = px["close"].pct_change()
    ret_corr = float(ib_ret.corr(px_ret))
    ret_diff_bps = float((ib_ret - px_ret).abs().median() * 10_000.0)

    # 3. Signal divergence — compute the REAL entry trigger on each feed.
    cfg = live_strategy_config(symbol)
    sig_ib = add_indicators(ibkr, cfg)["signal"].reindex(common).fillna(False)
    sig_px = add_indicators(proxy, cfg)["signal"].reindex(common).fillna(False)
    agree = float((sig_ib == sig_px).mean())
    entries_ib = int(sig_ib.sum())
    entries_px = int(sig_px.sum())
    only_ib = int((sig_ib & ~sig_px).sum())   # entries IBKR would take, proxy misses
    only_px = int((~sig_ib & sig_px).sum())   # phantom entries the proxy invents

    # 4. Entry-fill basis on IBKR entry days (next-open difference)
    next_open_ib = ib["open"].shift(-1)
    next_open_px = px["open"].shift(-1)
    fill_bps_series = _bps(next_open_ib, next_open_px)
    entry_days = sig_ib[sig_ib].index
    fill_on_entries = fill_bps_series.reindex(entry_days).dropna()
    fill_med = float(fill_on_entries.median()) if len(fill_on_entries) else float("nan")
    fill_p95 = float(fill_on_entries.quantile(0.95)) if len(fill_on_entries) else float("nan")

    return {
        "symbol": symbol,
        "fallback": fallback,
        "days": len(common),
        "range": f"{common.min().date()}→{common.max().date()}",
        "open_med": float(ohlc_basis["open"].median()),
        "high_med": float(ohlc_basis["high"].median()),
        "low_med": float(ohlc_basis["low"].median()),
        "close_med": float(ohlc_basis["close"].median()),
        "close_p95": float(ohlc_basis["close"].quantile(0.95)),
        "ret_corr": ret_corr,
        "ret_diff_bps": ret_diff_bps,
        "sig_agree": agree,
        "entries_ib": entries_ib,
        "entries_px": entries_px,
        "only_ib": only_ib,
        "only_px": only_px,
        "fill_med_bps": fill_med,
        "fill_p95_bps": fill_p95,
        "fee_bps": float(cfg.fee_bps),
    }


def main() -> None:
    p = argparse.ArgumentParser(description="IBKR vs proxy basis study")
    p.add_argument("symbols", nargs="*", default=list(LIVE_SYMBOLS))
    p.add_argument("--start", default="2021-01-01")
    p.add_argument("--end", default=None)
    args = p.parse_args()

    from ibkr_data import ibkr_available

    if not ibkr_available():
        print(
            "\nIBKR TWS / Gateway is not reachable — cannot run the basis study.\n"
            "Connect first, then re-run:\n"
            "  1. Open TWS (paper) or IB Gateway and log in\n"
            "  2. Config → API → Settings → enable 'ActiveX and Socket Clients'\n"
            "     (paper port 7497 for TWS, 4002 for Gateway)\n"
            "  3. python ibkr_basis_study.py\n"
            "\nUntil then the paper book runs on proxy feeds (Binance/Dukascopy/yfinance).\n"
        )
        return

    symbols = args.symbols or list(LIVE_SYMBOLS)
    print(f"\nIBKR-vs-proxy basis study  ({args.start} → {args.end or 'today'})\n")

    rows = []
    for sym in symbols:
        print(f"Fetching {sym}…")
        r = study_symbol(sym, args.start, args.end)
        if r:
            rows.append(r)

    if not rows:
        print("\nNo sleeves produced results.")
        return

    df = pd.DataFrame(rows).set_index("symbol")

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)

    print("\n── Price basis (median |IBKR/proxy − 1|, bps) ──")
    print(df[["fallback", "days", "open_med", "high_med", "low_med", "close_med", "close_p95"]].round(1).to_string())

    print("\n── Return tracking ──")
    print(df[["ret_corr", "ret_diff_bps"]].round(3).to_string())

    print("\n── SIGNAL divergence (the metric that matters) ──")
    print(df[["sig_agree", "entries_ib", "entries_px", "only_ib", "only_px"]].round(4).to_string())

    print("\n── Entry-fill basis on IBKR entry days (bps) vs round-trip fee ──")
    print(df[["fill_med_bps", "fill_p95_bps", "fee_bps"]].round(1).to_string())

    print("\nReading the results:")
    print("  • close_med ≫ a few bps  → the feed you tuned on differs materially; re-fit params on IBKR.")
    print("  • only_px > 0            → proxy invents entries IBKR would never trigger (false edge in backtest).")
    print("  • only_ib > 0            → proxy misses real IBKR entries (understated trade count).")
    print("  • fill_med_bps vs fee    → if fill basis approaches round-trip fee, proxy P&L is optimistic.")


if __name__ == "__main__":
    main()
