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
import sys
from pathlib import Path
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import (  # noqa: E402
    fetch_binance_daily,
    print_bot_report,
    write_state,
)
from btc_breakout_paper_sim import (  # noqa: E402
    SimConfig,
    StrategyConfig,
    add_indicators,
    fmt,
    simulate_account,
)


def latest_signal(df: pd.DataFrame, strat_cfg: StrategyConfig) -> dict[str, Any]:
    last = df.iloc[-1]
    latest = {
        "signal_date": df.index[-1].isoformat(),
        "close": float(last["close"]),
        "prior_high": float(last["prior_high"]) if pd.notna(last["prior_high"]) else None,
        "breakout_bps": float(last["breakout_bps"]) if pd.notna(last["breakout_bps"]) else None,
        "sma200": float(last["sma200"]) if pd.notna(last["sma200"]) else None,
        "bull": bool(last["bull"]),
        "signal": bool(last["signal"]),
        "next_size_frac": 0.0,
    }
    if latest["signal"]:
        rv = float(last["vol20"])
        latest["next_size_frac"] = min(strat_cfg.max_alloc, strat_cfg.vol_target / rv) if rv > 0 else 0.0
    return latest


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


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Daily Binance BTC paper runner")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--base-url", default="https://api.binance.com")
    p.add_argument("--start", default="2018-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--state-dir", default="btc_breakout_clean/paper_binance")
    p.add_argument("--no-write", action="store_true", help="Print only; do not write state/log files")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    state_dir = Path(args.state_dir)
    state_path = state_dir / "state.json"
    trades_path = state_dir / "trades.csv"
    equity_path = state_dir / "equity.csv"
    log_path = state_dir / "run_log.csv"

    previous = load_previous_state(state_path)
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
    strat_cfg = StrategyConfig(
        lookback=15,
        buffer_bps=100.0,
        max_breakout_bps=225.0,
        trend_mode="bull_only",
        hold_days=5,
        trail_atr=0.0,
        fee_bps=10.0,
        vol_target=0.015,
        max_alloc=0.75,
        compound=False,
    )

    df = add_indicators(raw, strat_cfg)
    trades, curve, summary = simulate_account(df, sim_cfg=sim_cfg, strat_cfg=strat_cfg)
    latest = latest_signal(df, strat_cfg)
    event = classify_event(previous, latest, trades)
    state_written = not args.no_write

    if state_written:
        write_state(state_path, trades_path, equity_path, trades, curve, summary, latest)
        append_run_log(log_path, event=event, symbol=args.symbol, summary=summary, latest=latest)

    print_bot_report(args.symbol, df, trades, curve, summary, latest, state_path, state_written)
    print("-" * 92)
    print(f"  Daily event: {event}")
    print(f"  Fake equity: ${fmt(summary['final_equity'])}")
    print(f"  Next action: {'PAPER ENTER LONG' if latest['signal'] else 'NO TRADE'}")
    if state_written:
        print(f"  Run log: {log_path}")
    print("-" * 92)


if __name__ == "__main__":
    main()
