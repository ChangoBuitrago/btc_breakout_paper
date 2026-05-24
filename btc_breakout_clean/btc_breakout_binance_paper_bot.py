#!/usr/bin/env python3
"""
Breakout paper bot helpers.

Fetches public daily candles (Binance or Dukascopy-backed) and replays the
configured breakout rule in a fake account. Never places real orders.

Default live portfolio sleeves are defined in LIVE_SYMBOLS and
LIVE_STRATEGY_PARAMS. Per-symbol rules include lookback, buffer, exhaustion cap,
regime filter, hold, fees, and compounding.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_paper_sim import (  # noqa: E402
    SimConfig,
    StrategyConfig,
    TREND_MODE_CHOICES,
    add_indicators,
    default_skip_saturday_entry,
    effective_hold_max,
    effective_hold_min,
    fmt,
    fmt_pf,
    latest_signal_report,
    simulate_account,
    uses_dynamic_hold,
)

DEFAULT_BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_BASE_URL_FALLBACKS = (
    "https://api.binance.com",
    "https://data-api.binance.vision",
)
# Live book (May 2026): $100k equal split across sleeves; max 4 concurrent.
LIVE_SYMBOLS = (
    "BTCUSD",
    "ETHUSDT",
    "BNBUSDT",
    "SOLUSDT",
    "DOGEUSDT",
    "XAUUSD",
    "XAGUSD",
    "BRENT",
)
LIVE_PORTFOLIO_NOTIONAL = 100_000.0
LIVE_SLEEVE_EQUITY = LIVE_PORTFOLIO_NOTIONAL / len(LIVE_SYMBOLS)  # $12,500 each
LIVE_MAX_CONCURRENT_ENTRIES = 4
LIVE_STRATEGY_PARAMS: dict[str, dict[str, float | int | str | bool]] = {
    "BTCUSD": {
        "source": "dukascopy",
        "equity": LIVE_SLEEVE_EQUITY,
        "lookback": 15,
        "buffer_bps": 125.0,
        "max_breakout_bps": 225.0,
        "trend_mode": "bull_only",
        "hold_days": 5,
        "hold_min": 5,
        "hold_max": 10,
        "dynamic_hold": True,
        "fee_bps": 10.0,
        "compound": True,
    },
    "ETHUSDT": {
        "source": "binance",
        "equity": LIVE_SLEEVE_EQUITY,
        "lookback": 10,
        "buffer_bps": 150.0,
        "max_breakout_bps": 225.0,
        "trend_mode": "bull_only",
        "hold_days": 10,
        "hold_min": 10,
        "hold_max": 13,
        "dynamic_hold": True,
        "fee_bps": 10.0,
        "compound": True,
    },
    "BNBUSDT": {
        "source": "binance",
        "equity": LIVE_SLEEVE_EQUITY,
        "lookback": 15,
        "buffer_bps": 125.0,
        "max_breakout_bps": 225.0,
        "trend_mode": "bull_only",
        "hold_days": 6,
        "hold_min": 6,
        "hold_max": 10,
        "dynamic_hold": True,
        "fee_bps": 10.0,
        "compound": True,
    },
    "SOLUSDT": {
        "source": "binance",
        "equity": LIVE_SLEEVE_EQUITY,
        "lookback": 20,
        "buffer_bps": 75.0,
        "max_breakout_bps": 225.0,
        "trend_mode": "sma200_95",
        "hold_days": 9,
        "hold_min": 9,
        "hold_max": 15,
        "dynamic_hold": True,
        "fee_bps": 10.0,
        "compound": True,
    },
    "XAUUSD": {
        "source": "dukascopy",
        "equity": LIVE_SLEEVE_EQUITY,
        "lookback": 30,
        "buffer_bps": 100.0,
        "max_breakout_bps": 225.0,
        "trend_mode": "sma200_95",
        "hold_days": 9,
        "hold_min": 9,
        "hold_max": 15,
        "dynamic_hold": True,
        "fee_bps": 2.0,
        "compound": True,
    },
    "XAGUSD": {
        "source": "dukascopy",
        "equity": LIVE_SLEEVE_EQUITY,
        "lookback": 30,
        "buffer_bps": 100.0,
        "max_breakout_bps": 225.0,
        "trend_mode": "bull_only",
        "hold_days": 13,
        "hold_min": 13,
        "hold_max": 15,
        "dynamic_hold": True,
        "fee_bps": 2.0,
        "compound": True,
    },
    "XCUUSD": {
        "source": "dukascopy",
        "equity": LIVE_SLEEVE_EQUITY,
        "lookback": 15,
        "buffer_bps": 100.0,
        "max_breakout_bps": 225.0,
        "trend_mode": "bull_only",
        "hold_days": 4,
        "hold_min": 4,
        "hold_max": 5,
        "dynamic_hold": True,
        "fee_bps": 10.0,
        "compound": True,
    },
    "DOGEUSDT": {
        "source": "binance",
        "equity": LIVE_SLEEVE_EQUITY,
        "lookback": 30,
        "buffer_bps": 75.0,
        "max_breakout_bps": 225.0,
        "trend_mode": "sma200_95",
        "hold_days": 9,
        "hold_min": 9,
        "hold_max": 15,
        "dynamic_hold": True,
        "fee_bps": 10.0,
        "compound": True,
    },
    "BRENT": {
        "source": "dukascopy",
        "equity": LIVE_SLEEVE_EQUITY,
        "lookback": 30,
        "buffer_bps": 75.0,
        "max_breakout_bps": 225.0,
        "trend_mode": "sma200_95",
        "hold_days": 9,
        "hold_min": 9,
        "hold_max": 15,
        "dynamic_hold": True,
        "fee_bps": 5.0,
        "compound": True,
    },
    # Aliases for manual single-symbol checks.
    "BTCUSDT": {"source": "binance", "lookback": 15, "buffer_bps": 125.0, "max_breakout_bps": 225.0, "hold_days": 5},
    "ETCUSDT": {"source": "binance", "lookback": 30, "buffer_bps": 100.0, "max_breakout_bps": 400.0, "hold_days": 5},
}

LIVE_PORTFOLIO_EQUITY = sum(float(LIVE_STRATEGY_PARAMS[s]["equity"]) for s in LIVE_SYMBOLS)


def live_symbol_params(symbol: str) -> dict[str, float | int | str | bool]:
    return LIVE_STRATEGY_PARAMS.get(symbol.upper(), LIVE_STRATEGY_PARAMS["BTCUSD"])


def live_symbol_source(symbol: str) -> str:
    return str(live_symbol_params(symbol).get("source", "binance"))


def live_symbol_equity(symbol: str, fallback: float) -> float:
    return float(live_symbol_params(symbol).get("equity", fallback))


def live_strategy_config(symbol: str = "BTCUSD") -> StrategyConfig:
    params = live_symbol_params(symbol)
    hold_min = int(params.get("hold_min", params["hold_days"]))
    hold_max = int(params.get("hold_max", hold_min))
    dynamic_hold = bool(params.get("dynamic_hold", hold_max > hold_min))
    return StrategyConfig(
        lookback=int(params["lookback"]),
        buffer_bps=float(params["buffer_bps"]),
        max_breakout_bps=float(params["max_breakout_bps"]),
        trend_mode=str(params.get("trend_mode", "bull_only")),
        hold_days=hold_min,
        trail_atr=0.0,
        fee_bps=float(params.get("fee_bps", 10.0)),
        vol_target=0.015,
        max_alloc=0.75,
        compound=bool(params.get("compound", False)),
        hold_min=hold_min,
        hold_max=hold_max,
        dynamic_hold=dynamic_hold,
        hold_giveback_pct=float(params.get("hold_giveback_pct", 0.03)),
    )


def _fetch_binance_daily_from_base(symbol: str, start: str, end: str | None, base_url: str) -> pd.DataFrame:
    start_ms = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    end_ms = int(pd.Timestamp(end, tz="UTC").timestamp() * 1000) if end else None
    rows: list[list[Any]] = []

    while True:
        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "interval": "1d",
            "limit": 1000,
            "startTime": start_ms,
        }
        if end_ms is not None:
            params["endTime"] = end_ms

        klines_url = f"{base_url.rstrip('/')}/api/v3/klines"
        url = f"{klines_url}?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=20) as resp:
            chunk = json.loads(resp.read().decode("utf-8"))

        if not chunk:
            break
        rows.extend(chunk)

        next_start = int(chunk[-1][0]) + 24 * 60 * 60 * 1000
        if next_start <= start_ms:
            break
        start_ms = next_start
        if len(chunk) < 1000:
            break

    if not rows:
        raise RuntimeError(f"Binance returned no daily candles for {symbol}")

    now_ms = int(pd.Timestamp.now(tz="UTC").timestamp() * 1000)
    closed = [r for r in rows if int(r[6]) < now_ms]
    df = pd.DataFrame(
        {
            "date": pd.to_datetime([int(r[0]) for r in closed], unit="ms", utc=True),
            "open": [float(r[1]) for r in closed],
            "high": [float(r[2]) for r in closed],
            "low": [float(r[3]) for r in closed],
            "close": [float(r[4]) for r in closed],
            "volume": [float(r[5]) for r in closed],
        }
    )
    return df.set_index("date").sort_index()


def fetch_binance_daily(symbol: str, start: str, end: str | None, base_url: str = DEFAULT_BINANCE_BASE_URL) -> pd.DataFrame:
    base_urls = [base_url]
    base_urls.extend(url for url in BINANCE_BASE_URL_FALLBACKS if url not in base_urls)
    errors: list[str] = []
    for candidate in base_urls:
        try:
            return _fetch_binance_daily_from_base(symbol, start, end, candidate)
        except urllib.error.HTTPError as exc:
            errors.append(f"{candidate} -> HTTP {exc.code}")
            if exc.code not in {451, 403, 429}:
                raise
        except Exception as exc:
            errors.append(f"{candidate} -> {type(exc).__name__}: {exc}")
    raise RuntimeError(f"All Binance endpoints failed: {errors}")


def write_state(
    state_path: Path,
    trades_path: Path,
    equity_path: Path,
    trades: pd.DataFrame,
    curve: pd.DataFrame,
    summary: dict[str, Any],
    latest: dict[str, Any],
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    trades.to_csv(trades_path, index=False)
    curve.to_csv(equity_path, index=False)
    payload = {
        "summary": summary,
        "latest_signal": latest,
        "last_run_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "last_equity_date": curve["date"].iloc[-1] if not curve.empty else None,
        "last_trade": trades.tail(1).to_dict("records")[0] if not trades.empty else None,
    }
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def print_bot_report(
    symbol: str,
    source_label: str,
    df: pd.DataFrame,
    trades: pd.DataFrame,
    curve: pd.DataFrame,
    summary: dict[str, Any],
    latest: dict[str, Any],
    strat_cfg: StrategyConfig,
    state_path: Path,
    state_written: bool,
) -> None:
    print("=" * 92)
    print("  BREAKOUT PAPER BOT")
    print("=" * 92)
    print(f"  Symbol: {symbol.upper()}  |  Source: {source_label}")
    print(f"  Data:   {df.index[0].date()} -> {df.index[-1].date()} rows={len(df):,}")
    hmin = effective_hold_min(strat_cfg)
    hmax = effective_hold_max(strat_cfg)
    hold_txt = f"hold {hmin}-{hmax} dyn" if uses_dynamic_hold(strat_cfg) else f"hold={hmin}"
    print(
        f"  Rule:   {strat_cfg.trend_mode}, {strat_cfg.lookback}d breakout "
        f"+ {strat_cfg.buffer_bps:.0f}bps, max breakout {strat_cfg.max_breakout_bps:.0f}bps, "
        f"{hold_txt}"
    )
    print("  Orders: PAPER ONLY - no real exchange orders are sent")
    print("-" * 92)
    print(f"  Final fake equity: ${fmt(summary['final_equity'])}")
    print(f"  Net fake PnL:      ${fmt(summary['net_pnl'])}")
    print(f"  CAGR:              {summary['cagr_pct']:.2f}%")
    print(f"  Max DD:            {summary['max_drawdown_pct']:.2f}%")
    print(f"  Trades:            {summary['trades']}")
    print(f"  Profit factor:     {fmt_pf(summary['profit_factor'])}")
    print("-" * 92)
    print(f"  Latest closed candle: {latest['signal_date'][:10]} close={latest['close']:,.2f}")
    print(f"  Prior high: {fmt(latest['prior_high']) if latest['prior_high'] is not None else 'n/a'}")
    print(f"  Breakout size: {latest['breakout_bps']:.0f}bps" if latest.get("breakout_bps") is not None else "  Breakout size: n/a")
    print(f"  SMA200: {fmt(latest['sma200']) if latest['sma200'] is not None else 'n/a'}")
    print(f"  Bull regime: {'YES' if latest['bull'] else 'NO'}")
    if latest.get("trend_mode") not in {"bull_only", "sma200"}:
        print(f"  Active regime ({latest['trend_mode']}): {'YES' if latest.get('regime_on') else 'NO'}")
    print(f"  Signal for next UTC day: {'YES' if latest['signal'] else 'NO'}")
    if latest["signal"]:
        print(f"  Next fake position size: {latest['next_size_frac']:.2%} of initial equity")
    print("-" * 92)
    if trades.empty:
        print("  No completed fake trades yet.")
    else:
        recent = trades.tail(5)
        print("  Recent completed fake trades:")
        print(f"  {'entry':10} {'exit':10} {'hold':>4} {'size':>7} {'pnl':>11} {'equity':>12}")
        for _, row in recent.iterrows():
            print(
                f"  {str(row['entry_date'])[:10]:10} {str(row['exit_date'])[:10]:10} "
                f"{int(row['hold_days']):>4} {float(row['size_frac']) * 100:>6.2f}% "
                f"${fmt(float(row['net_pnl'])):>10} ${fmt(float(row['equity_after'])):>11}"
            )
    print("-" * 92)
    if state_written:
        print(f"  State written to: {state_path}")
    else:
        print("  State not written (--no-write)")
    print("=" * 92)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Single-symbol breakout paper bot (Binance daily candles)")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--base-url", default=DEFAULT_BINANCE_BASE_URL)
    p.add_argument("--start", default="2018-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--trend-mode", choices=TREND_MODE_CHOICES, default=None, help="Override the symbol's configured regime filter")
    p.add_argument("--state-path", default="btc_breakout_clean/paper_binance/state.json")
    p.add_argument("--trades-path", default="btc_breakout_clean/paper_binance/trades.csv")
    p.add_argument("--equity-path", default="btc_breakout_clean/paper_binance/equity.csv")
    p.add_argument("--no-write", action="store_true", help="Print only; do not write local paper state")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    raw = fetch_binance_daily(args.symbol, args.start, args.end, args.base_url)
    sim_cfg = SimConfig(
        source="binance",
        data_start=args.start,
        sim_start=pd.Timestamp(args.start, tz="UTC"),
        end=args.end,
        equity=args.equity,
        include_current=False,
        cache_path=Path(""),
        dukascopy_path=Path(""),
        refresh_cache=False,
        show_trades=0,
        write_files=False,
        out_dir=Path("."),
    )
    strat_cfg = live_strategy_config(args.symbol)
    if args.trend_mode:
        strat_cfg = replace(strat_cfg, trend_mode=args.trend_mode)
    df = add_indicators(raw, strat_cfg)
    trades, curve, summary = simulate_account(df, sim_cfg=sim_cfg, strat_cfg=strat_cfg)
    latest = latest_signal_report(df, strat_cfg)

    state_path = Path(args.state_path)
    state_written = not args.no_write
    if state_written:
        write_state(state_path, Path(args.trades_path), Path(args.equity_path), trades, curve, summary, latest)
    print_bot_report(
        args.symbol,
        "Binance public 1d klines",
        df,
        trades,
        curve,
        summary,
        latest,
        strat_cfg,
        state_path,
        state_written,
    )


if __name__ == "__main__":
    main()
