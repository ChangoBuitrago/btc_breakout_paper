#!/usr/bin/env python3
"""
Offline proxy-vs-proxy basis study.

No TWS / IBKR connection needed.

For each live sleeve, compares the *current proxy feed* (what the algo runs on)
against a *reference feed* (the closest alternative available without IBKR).
This tells you how much inter-feed divergence exists right now — establishing a
baseline before IBKR data is available.

Proxy map (current → reference):
  BTC   Binance BTCUSDT   → yfinance BTC-USD
  ETH   Binance ETHUSDT   → yfinance ETH-USD
  SOL   Binance SOLUSDT   → yfinance SOL-USD
  DOGE  Binance DOGEUSDT  → yfinance DOGE-USD
  XAU   Dukascopy XAUUSD  → yfinance GC=F  (gold front-month futures)
  XAG   Dukascopy XAGUSD  → yfinance SI=F  (silver front-month futures)
  BNO   yfinance BNO      → yfinance COIL  (comparable Brent ETF)

Output (per sleeve):
  • Price basis   — median & p95 |A/B − 1| bps per OHLC field
  • Return corr   — daily-return correlation + median |Δret| bps
  • Signal diff   — entry trigger agreement rate + phantom/missed trades
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import (          # noqa: E402
    LIVE_SYMBOLS,
    fetch_binance_daily,
    live_binance_symbol,
    live_strategy_config,
    live_symbol_fallback_source,
    live_yfinance_ticker,
)
from btc_breakout_paper_sim import (                   # noqa: E402
    add_indicators,
    dukascopy_cache_path,
    fetch_dukascopy_instrument,
    fetch_yfinance_daily,
    normalize_ohlc,
    yfinance_cache_path,
)

START = "2021-01-01"

# What to compare each sleeve against
REFERENCE_FEEDS: dict[str, tuple[str, str]] = {
    "BTCUSD":   ("yfinance", "BTC-USD"),
    "ETHUSDT":  ("yfinance", "ETH-USD"),
    "SOLUSDT":  ("yfinance", "SOL-USD"),
    "DOGEUSDT": ("yfinance", "DOGE-USD"),
    "XAUUSD":   ("yfinance", "GC=F"),
    "XAGUSD":   ("yfinance", "SI=F"),
    "BNO":      ("yfinance", "COIL"),
}


def fetch_proxy(symbol: str) -> pd.DataFrame:
    source = live_symbol_fallback_source(symbol)
    if source == "binance":
        pair = live_binance_symbol(symbol)
        return normalize_ohlc(fetch_binance_daily(pair, START, None))
    if source == "dukascopy":
        return fetch_dukascopy_instrument(
            symbol, dukascopy_cache_path(symbol), START, None,
            include_current=False, refresh_cache=False,
        )
    if source == "yfinance":
        tkr = live_yfinance_ticker(symbol)
        return fetch_yfinance_daily(tkr, START, None, False, yfinance_cache_path(tkr), False)
    raise ValueError(f"Unknown source: {source}")


def fetch_ref(ref_source: str, ref_ticker: str) -> pd.DataFrame:
    if ref_source == "yfinance":
        return fetch_yfinance_daily(
            ref_ticker, START, None, False,
            yfinance_cache_path(ref_ticker), False,
        )
    raise ValueError(f"Unknown ref source: {ref_source}")


def bps(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a / b - 1.0).abs() * 10_000.0


def study(symbol: str) -> dict | None:
    ref_source, ref_ticker = REFERENCE_FEEDS[symbol]
    proxy_source = live_symbol_fallback_source(symbol)
    proxy_label = f"{proxy_source}:{live_binance_symbol(symbol) if proxy_source == 'binance' else live_yfinance_ticker(symbol) if proxy_source == 'yfinance' else symbol}"

    try:
        a = fetch_proxy(symbol)
    except Exception as exc:
        print(f"  {symbol}: proxy fetch failed — {exc}")
        return None
    try:
        b = fetch_ref(ref_source, ref_ticker)
    except Exception as exc:
        print(f"  {symbol}: ref fetch failed — {exc}")
        return None

    a.index = pd.to_datetime(a.index, utc=True).normalize()
    b.index = pd.to_datetime(b.index, utc=True).normalize()
    common = a.index.intersection(b.index)
    if len(common) < 60:
        print(f"  {symbol}: only {len(common)} common days — skipping")
        return None

    ai, bi = a.loc[common], b.loc[common]

    close_basis = bps(ai["close"], bi["close"])
    open_basis  = bps(ai["open"],  bi["open"])
    high_basis  = bps(ai["high"],  bi["high"])
    low_basis   = bps(ai["low"],   bi["low"])

    ar, br = ai["close"].pct_change(), bi["close"].pct_change()
    ret_corr = float(ar.corr(br))
    ret_diff = float((ar - br).abs().median() * 10_000)

    cfg = live_strategy_config(symbol)
    sig_a = add_indicators(a, cfg)["signal"].reindex(common).fillna(False)
    sig_b = add_indicators(b, cfg)["signal"].reindex(common).fillna(False)
    agree   = float((sig_a == sig_b).mean())
    only_a  = int((sig_a & ~sig_b).sum())
    only_b  = int((~sig_a & sig_b).sum())
    entries_a = int(sig_a.sum())
    entries_b = int(sig_b.sum())

    return {
        "symbol":    symbol,
        "proxy":     proxy_label,
        "ref":       f"yf:{ref_ticker}",
        "days":      len(common),
        "open_med":  round(float(open_basis.median()), 1),
        "high_med":  round(float(high_basis.median()), 1),
        "low_med":   round(float(low_basis.median()), 1),
        "close_med": round(float(close_basis.median()), 1),
        "close_p95": round(float(close_basis.quantile(0.95)), 1),
        "ret_corr":  round(ret_corr, 4),
        "ret_diff":  round(ret_diff, 2),
        "agree":     round(agree, 4),
        "entries_a": entries_a,
        "entries_b": entries_b,
        "only_a":    only_a,
        "only_b":    only_b,
        "fee_rt":    round(cfg.fee_bps * 2, 1),
    }


def main() -> None:
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)

    print(f"\nOffline proxy-vs-proxy basis study  (from {START})\n")
    rows = []
    for sym in LIVE_SYMBOLS:
        print(f"  {sym}…", end=" ", flush=True)
        r = study(sym)
        if r:
            rows.append(r)
            print("done")
        else:
            print("skipped")

    if not rows:
        print("No results.")
        return

    df = pd.DataFrame(rows).set_index("symbol")

    print("\n── 1. Price basis  (median |proxy/ref − 1|, bps) ──────────────────────────────────")
    print(df[["proxy", "ref", "days", "open_med", "high_med", "low_med", "close_med", "close_p95"]].to_string())

    print("\n── 2. Return tracking ──────────────────────────────────────────────────────────────")
    print(df[["ret_corr", "ret_diff"]].rename(columns={"ret_diff": "ret_diff_bps"}).to_string())

    print("\n── 3. SIGNAL divergence (entry trigger with live params) ────────────────────────────")
    print(df[["agree", "entries_a", "entries_b", "only_a", "only_b", "fee_rt"]].rename(
        columns={"entries_a": "entries(proxy)", "entries_b": "entries(ref)",
                 "only_a": "proxy_only", "only_b": "ref_only", "fee_rt": "fee_rt_bps"}
    ).to_string())

    print()
    print("Key:")
    print("  proxy_only = entries proxy sees, ref misses  (proxy may be over-triggering)")
    print("  ref_only   = entries ref sees, proxy misses  (proxy may be under-triggering)")
    print("  fee_rt_bps = round-trip fee — fill basis approaching this means P&L is distorted")


if __name__ == "__main__":
    main()
