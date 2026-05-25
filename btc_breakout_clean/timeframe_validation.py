#!/usr/bin/env python3
"""
Bar-frequency pilot: same engine on 1d vs 4h vs 1h bars (crypto book).

Two param modes per timeframe:
  - calendar_scaled: lookback/hold scaled so windows match ~same calendar time as live daily
  - bar_native: same integer lookback/hold as live (15 bars = 15h on 1h data)

Pilot scope: 5 crypto sleeves, max 4 concurrent. Metals/oil omitted (no unified intraday feed).

Run: python3 btc_breakout_clean/timeframe_validation.py
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import (  # noqa: E402
    BINANCE_BASE_URL_FALLBACKS,
    DEFAULT_BINANCE_BASE_URL,
    LIVE_CRYPTO_SYMBOLS,
    LIVE_MAX_CONCURRENT_ENTRIES,
    LIVE_SLEEVE_EQUITY,
    fetch_binance_daily,
    live_strategy_config,
)
from btc_breakout_paper_sim import (  # noqa: E402
    StrategyConfig,
    download_dukascopy_h1,
    dukascopy_cache_path,
    normalize_ohlc,
    resample_h1_to_daily,
)
from strategy_validation import (  # noqa: E402
    DATA_START,
    beats_baseline,
    portfolio_metrics,
    run_full_book_live,
)

OUT_PATH = HERE / "timeframe_validation_results.json"
CRYPTO_SYMBOLS = tuple(LIVE_CRYPTO_SYMBOLS)
BAR_HOURS = {"1d": 24, "4h": 4, "1h": 1}
BINANCE_MAP = {
    "BTCUSD": "BTCUSDT",
    "ETHUSDT": "ETHUSDT",
    "BNBUSDT": "BNBUSDT",
    "SOLUSDT": "SOLUSDT",
    "DOGEUSDT": "DOGEUSDT",
}


def _fetch_binance_klines(symbol: str, interval: str, start: str, end: str | None) -> pd.DataFrame:
    ms_step = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[interval]
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000) if end else None
    rows: list[list[Any]] = []
    base_urls = [DEFAULT_BINANCE_BASE_URL, *BINANCE_BASE_URL_FALLBACKS]

    for base in base_urls:
        rows.clear()
        cur = start_ms
        try:
            while True:
                params: dict[str, Any] = {
                    "symbol": symbol.upper(),
                    "interval": interval,
                    "limit": 1000,
                    "startTime": cur,
                }
                if end_ms is not None:
                    params["endTime"] = end_ms
                url = f"{base.rstrip('/')}/api/v3/klines?{urllib.parse.urlencode(params)}"
                with urllib.request.urlopen(url, timeout=30) as resp:
                    chunk = json.loads(resp.read().decode("utf-8"))
                if not chunk:
                    break
                rows.extend(chunk)
                nxt = int(chunk[-1][0]) + ms_step
                if nxt <= cur or len(chunk) < 1000:
                    break
                cur = nxt
            if rows:
                break
        except Exception:
            continue

    if not rows:
        raise RuntimeError(f"No Binance {interval} data for {symbol}")

    now_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    closed = [r for r in rows if int(r[6]) < now_ms]
    df = pd.DataFrame(
        {
            "open": [float(r[1]) for r in closed],
            "high": [float(r[2]) for r in closed],
            "low": [float(r[3]) for r in closed],
            "close": [float(r[4]) for r in closed],
            "volume": [float(r[5]) for r in closed],
        },
        index=pd.to_datetime([int(r[0]) for r in closed], unit="ms", utc=True),
    )
    return normalize_ohlc(df.sort_index())


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    work = df.copy()
    if "volume" not in work.columns:
        work["volume"] = 0.0
    agg = work.resample(rule).agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return normalize_ohlc(agg.dropna(subset=["close"]))


def load_h1(symbol: str) -> pd.DataFrame:
    if symbol == "BTCUSD":
        path = dukascopy_cache_path("BTCUSD")
        if path.exists():
            h1 = normalize_ohlc(pd.read_csv(path, index_col=0, parse_dates=True))
        else:
            h1 = download_dukascopy_h1("BTCUSD", DATA_START, None)
        return h1.loc[h1.index >= pd.Timestamp(DATA_START, tz="UTC")]
    pair = BINANCE_MAP[symbol]
    return _fetch_binance_klines(pair, "1h", DATA_START, None)


def load_bars(symbol: str, timeframe: str) -> pd.DataFrame:
    if timeframe == "1d":
        if symbol == "BTCUSD":
            return resample_h1_to_daily(load_h1(symbol))
        return fetch_binance_daily(BINANCE_MAP[symbol], DATA_START, None).rename(columns=str.lower)
    h1 = load_h1(symbol)
    if timeframe == "1h":
        return h1
    if timeframe == "4h":
        return resample_ohlc(h1, "4h")
    raise ValueError(timeframe)


def scale_for_timeframe(cfg: StrategyConfig, timeframe: str, mode: str) -> StrategyConfig:
    if mode == "bar_native" or timeframe == "1d":
        return cfg
    factor = 24 / BAR_HOURS[timeframe]
    hmin = max(1, int(round((cfg.hold_min or cfg.hold_days) * factor)))
    hmax = max(hmin + 1, int(round((cfg.hold_max or hmin) * factor)))
    lb = max(3, int(round(cfg.lookback * factor)))
    return replace(
        cfg,
        lookback=lb,
        hold_min=hmin,
        hold_max=hmax,
        hold_days=hmin,
        dynamic_hold=cfg.dynamic_hold,
    )


def build_strats(timeframe: str, mode: str) -> dict[str, StrategyConfig]:
    return {
        s: scale_for_timeframe(live_strategy_config(s), timeframe, mode) for s in CRYPTO_SYMBOLS
    }


def preload_crypto(timeframe: str) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for sym in CRYPTO_SYMBOLS:
        print(f"  load {sym} {timeframe} …", flush=True)
        out[sym] = load_bars(sym, timeframe)
    return out


def run_book(raw: dict[str, pd.DataFrame], label: str, strats: dict[str, StrategyConfig], note: str) -> dict[str, Any]:
    eq = {s: LIVE_SLEEVE_EQUITY for s in CRYPTO_SYMBOLS}
    curves, _, trades, _ = run_full_book_live(
        raw,
        CRYPTO_SYMBOLS,
        strats,
        max_concurrent=LIVE_MAX_CONCURRENT_ENTRIES,
        equities_by_symbol=eq,
    )
    initial = sum(eq.values())
    m = portfolio_metrics(curves, trades, initial, initial_equity_by_sleeve=eq)
    hold_stats: dict[str, Any] = {}
    if not trades.empty:
        t = trades.copy()
        t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
        t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
        t["hold_hours"] = (t["exit_date"] - t["entry_date"]).dt.total_seconds() / 3600.0
        hold_stats = {
            "median_hold_hours": float(t["hold_hours"].median()),
            "mean_hold_hours": float(t["hold_hours"].mean()),
            "trades": int(len(t)),
        }
    return {"label": label, "note": note, "metrics": m, "trades": int(m.get("trades", 0)), "hold_stats": hold_stats}


def main() -> None:
    print("Timeframe pilot — crypto 5-sleeve book", flush=True)
    rows: list[dict[str, Any]] = []

    for tf in ("1d", "4h", "1h"):
        for mode in ("calendar_scaled", "bar_native"):
            if tf == "1d" and mode == "bar_native":
                continue  # same as calendar on daily
            label = f"{tf}_{mode}"
            note = f"{tf} bars, {mode.replace('_', ' ')}"
            print(f"\n=== {label} ===", flush=True)
            raw = preload_crypto(tf)
            strats = build_strats(tf, mode)
            row = run_book(raw, label, strats, note)
            rows.append(row)
            m = row["metrics"]
            hs = row.get("hold_stats", {})
            print(
                f"  ret={m['return_pct']:.1f}% DD={m['max_drawdown_pct']:.2f}% "
                f"PF={m['profit_factor']:.2f} Sh={m['sharpe_ratio']:.2f} tr={row['trades']} "
                f"med_h={hs.get('median_hold_hours', float('nan')):.0f}h",
                flush=True,
            )

    baseline = next(r for r in rows if r["label"] == "1d_calendar_scaled")
    bm = baseline["metrics"]
    for r in rows:
        r["passes_baseline"] = r["label"] == baseline["label"] or beats_baseline(bm, r["metrics"])

    passing = [r["label"] for r in rows if r.get("passes_baseline") and r["label"] != baseline["label"]]
    payload = {
        "scope": "crypto_5_sleeve",
        "baseline_label": "1d_calendar_scaled",
        "baseline_metrics": bm,
        "variants": rows,
        "passing_labels": passing,
        "interpretation": {
            "calendar_scaled": "Same ~calendar lookback/hold as live daily params",
            "bar_native": "Same integer bar counts as live (e.g. lookback=15 bars)",
            "minutes": "Not tested — engine is bar-agnostic; need data feed + bar count design",
        },
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nPassing vs 1d baseline: {passing or ['(none)']}")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
