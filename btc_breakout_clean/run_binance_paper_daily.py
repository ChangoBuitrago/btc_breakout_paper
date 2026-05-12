#!/usr/bin/env python3
"""
Daily runner for the Binance BTC breakout paper bot.

Designed for cron/GitHub Actions. It uses only public Binance candles, writes
paper state files, appends a compact run log, and prints the daily action.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
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
    LIVE_SYMBOLS,
    fetch_binance_daily,
    live_strategy_config,
    print_bot_report,
    write_state,
)
from btc_breakout_paper_sim import (  # noqa: E402
    SimConfig,
    TREND_MODE_CHOICES,
    add_indicators,
    fmt,
    latest_signal_report,
    simulate_account,
)


def load_previous_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def classify_event(previous: dict[str, Any], latest: dict[str, Any], trades: pd.DataFrame) -> str:
    if not previous:
        return "FIRST_RUN_SIGNAL" if latest["signal"] else "FIRST_RUN_NO_SIGNAL"

    prev_latest = previous.get("latest_signal", {})
    prev_trade = previous.get("last_trade") or {}
    last_trade = trades.tail(1).to_dict("records")[0] if not trades.empty else {}

    if prev_latest.get("signal") != latest["signal"]:
        return "SIGNAL_ON" if latest["signal"] else "SIGNAL_OFF"
    if prev_trade.get("exit_date") != last_trade.get("exit_date"):
        return "NEW_COMPLETED_TRADE"
    return "NO_CHANGE"


def append_run_log(
    path: Path,
    *,
    event: str,
    symbol: str,
    summary: dict[str, Any],
    latest: dict[str, Any],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "run_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "event": event,
        "symbol": symbol.upper(),
        "signal_date": latest["signal_date"],
        "signal": latest["signal"],
        "bull": latest["bull"],
        "close": latest["close"],
        "prior_high": latest["prior_high"],
        "breakout_bps": latest["breakout_bps"],
        "next_size_frac": latest["next_size_frac"],
        "final_equity": summary["final_equity"],
        "net_pnl": summary["net_pnl"],
        "trades": summary["trades"],
        "profit_factor": summary["profit_factor"],
        "max_drawdown_pct": summary["max_drawdown_pct"],
    }
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def summarize_signal_year(
    trades: pd.DataFrame,
    *,
    signal_date: str,
    starting_equity: float,
) -> dict[str, Any]:
    year = pd.Timestamp(signal_date).year
    if trades.empty:
        pnl = 0.0
        trade_count = 0
    else:
        t = trades.copy()
        t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
        t["net_pnl"] = pd.to_numeric(t["net_pnl"], errors="coerce").fillna(0.0)
        year_trades = t[t["entry_date"].dt.year == year]
        pnl = float(year_trades["net_pnl"].sum())
        trade_count = int(len(year_trades))
    return {
        "year": year,
        "equity": float(starting_equity) + pnl,
        "pnl": pnl,
        "trades": trade_count,
    }


def parse_symbols(args: argparse.Namespace) -> list[str]:
    raw = args.symbol or args.symbols
    symbols = [part.strip().upper() for part in raw.split(",") if part.strip()]
    if not symbols:
        raise SystemExit("No symbols configured")
    return symbols


def build_telegram_message(
    *,
    results: list[dict[str, Any]],
) -> str:
    date = results[0]["latest"]["signal_date"][:10] if results else "n/a"
    lines = [f"Crypto paper | {date}"]
    for result in results:
        latest = result["latest"]
        year_summary = result["year_summary"]
        breakout = f"{latest['breakout_bps']:.0f}bps" if latest.get("breakout_bps") is not None else "n/a"
        action = "ENTER" if latest["signal"] else "NO"
        lines.append(
            f"{result['symbol']}: {action} | close {latest['close']:,.2f} | "
            f"br {breakout} | bull {'Y' if latest['bull'] else 'N'} | "
            f"{year_summary['year']} PnL ${year_summary['pnl']:,.2f}"
        )
    return "\n".join(lines)


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with urllib.request.urlopen(url, data=payload, timeout=20) as resp:
        resp.read()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Daily Binance crypto paper runner")
    p.add_argument("--symbol", default=None, help="Run one symbol only; overrides --symbols")
    p.add_argument("--symbols", default=",".join(LIVE_SYMBOLS), help="Comma-separated symbols for portfolio tracking")
    p.add_argument("--base-url", default="https://api.binance.com")
    p.add_argument("--start", default="2018-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--trend-mode", choices=TREND_MODE_CHOICES, default=None, help="Override each symbol's configured regime filter")
    p.add_argument("--state-dir", default="btc_breakout_clean/paper_binance")
    p.add_argument("--no-write", action="store_true", help="Print only; do not write state/log files")
    p.add_argument("--telegram-token", default=os.getenv("TELEGRAM_BOT_TOKEN"))
    p.add_argument("--telegram-chat-id", default=os.getenv("TELEGRAM_CHAT_ID"))
    p.add_argument("--no-telegram", action="store_true", help="Disable Telegram notification even if configured")
    return p.parse_args()


def run_symbol(args: argparse.Namespace, symbol: str, state_dir: Path, log_path: Path) -> dict[str, Any]:
    symbol_dir = state_dir / symbol.upper()
    state_path = symbol_dir / "state.json"
    trades_path = symbol_dir / "trades.csv"
    equity_path = symbol_dir / "equity.csv"

    previous = load_previous_state(state_path)
    if not previous and symbol.upper() == "BTCUSDT":
        previous = load_previous_state(state_dir / "state.json")
    raw = fetch_binance_daily(symbol, args.start, args.end, args.base_url)
    strat_cfg = live_strategy_config(symbol)
    if args.trend_mode:
        strat_cfg = replace(strat_cfg, trend_mode=args.trend_mode)
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

    df = add_indicators(raw, strat_cfg)
    trades, curve, summary = simulate_account(df, sim_cfg=sim_cfg, strat_cfg=strat_cfg)
    latest = latest_signal_report(df, strat_cfg)
    event = classify_event(previous, latest, trades)
    year_summary = summarize_signal_year(trades, signal_date=latest["signal_date"], starting_equity=args.equity)
    state_written = not args.no_write

    if state_written:
        write_state(state_path, trades_path, equity_path, trades, curve, summary, latest)
        append_run_log(log_path, event=event, symbol=symbol, summary=summary, latest=latest)

    print_bot_report(symbol, df, trades, curve, summary, latest, strat_cfg, state_path, state_written)
    print("-" * 92)
    print(f"  Daily event: {event}")
    print(f"  Fake equity: ${fmt(summary['final_equity'])}")
    print(f"  Current-year PnL: ${fmt(year_summary['pnl'])}")
    print(f"  Next action: {'PAPER ENTER LONG' if latest['signal'] else 'NO TRADE'}")
    if state_written:
        print(f"  Run log: {log_path}")
    print("-" * 92)

    return {
        "symbol": symbol.upper(),
        "event": event,
        "summary": summary,
        "latest": latest,
        "year_summary": year_summary,
        "state_path": state_path,
    }


def main() -> None:
    args = parse_args()
    symbols = parse_symbols(args)
    state_dir = Path(args.state_dir)
    log_path = state_dir / "run_log.csv"
    results = [run_symbol(args, symbol, state_dir, log_path) for symbol in symbols]

    print("=" * 92)
    print("  MULTI-SYMBOL DAILY SUMMARY")
    print("=" * 92)
    for result in results:
        latest = result["latest"]
        year_summary = result["year_summary"]
        breakout = f"{latest['breakout_bps']:.0f}bps" if latest.get("breakout_bps") is not None else "n/a"
        print(
            f"  {result['symbol']:8} signal={'YES' if latest['signal'] else 'NO ':3} "
            f"bull={'YES' if latest['bull'] else 'NO ':3} breakout={breakout:>6} "
            f"year_pnl=${fmt(year_summary['pnl']):>10} event={result['event']}"
        )
    print("=" * 92)

    if args.no_telegram:
        print("  Telegram notification disabled (--no-telegram)")
    elif args.telegram_token and args.telegram_chat_id:
        message = build_telegram_message(results=results)
        try:
            send_telegram_message(args.telegram_token, args.telegram_chat_id, message)
            print("  Telegram notification sent")
        except Exception as exc:
            print(f"  Telegram notification failed: {type(exc).__name__}: {exc}")
    else:
        print("  Telegram not configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
    print("-" * 92)


if __name__ == "__main__":
    main()
