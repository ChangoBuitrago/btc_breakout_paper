#!/usr/bin/env python3
"""
Daily runner for the breakout paper portfolio.

Designed for cron/GitHub Actions. Writes per-sleeve paper state, appends a run
log, and optionally sends a Telegram summary. Default sleeves are equal-weight
BTC + metals (Dukascopy daily, H1 resampled) with total capital LIVE_PORTFOLIO_EQUITY.
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
    LIVE_MAX_CONCURRENT_ENTRIES,
    LIVE_SYMBOLS,
    LIVE_PORTFOLIO_EQUITY,
    LIVE_SLEEVE_EQUITY,
    fetch_binance_daily,
    live_symbol_equity,
    live_symbol_source,
    live_strategy_config,
    print_bot_report,
    write_state,
)
from strategy_validation import blocked_entries_max_concurrent  # noqa: E402
from btc_breakout_paper_sim import (  # noqa: E402
    SimConfig,
    StrategyConfig,
    TREND_MODE_CHOICES,
    add_indicators,
    default_skip_saturday_entry,
    dukascopy_cache_path,
    effective_hold_max,
    effective_hold_min,
    fetch_dukascopy_instrument,
    fmt,
    latest_signal_report,
    momentum_faded,
    simulate_account,
    uses_dynamic_hold,
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
        "cagr_pct": summary["cagr_pct"],
    }
    write_header = not path.exists()
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def summarize_signal_year(
    trades: pd.DataFrame,
    curve: pd.DataFrame,
    *,
    signal_date: str,
) -> dict[str, Any]:
    year = pd.Timestamp(signal_date).year
    if curve.empty:
        pnl = 0.0
    else:
        c = curve.copy()
        c["date"] = pd.to_datetime(c["date"], utc=True)
        c["daily_pnl"] = pd.to_numeric(c["daily_pnl"], errors="coerce").fillna(0.0)
        pnl = float(c[c["date"].dt.year == year]["daily_pnl"].sum())
    if trades.empty:
        trade_count = 0
    else:
        t = trades.copy()
        t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
        year_trades = t[t["entry_date"].dt.year == year]
        trade_count = int(len(year_trades))
    return {
        "year": year,
        "pnl": pnl,
        "trades": trade_count,
    }


def parse_symbols(args: argparse.Namespace) -> list[str]:
    raw = args.symbol or args.symbols
    symbols = [part.strip().upper() for part in raw.split(",") if part.strip()]
    if not symbols:
        raise SystemExit("No symbols configured")
    return symbols


def fmt_signed_money(value: float) -> str:
    sign = "+" if value >= 0 else "-"
    return f"{sign}${abs(value):,.0f}"


def summarize_open_position(
    curve: pd.DataFrame,
    df: pd.DataFrame,
    strat_cfg: StrategyConfig,
) -> dict[str, Any] | None:
    if curve.empty or df.empty:
        return None
    c = curve.copy()
    c["date"] = pd.to_datetime(c["date"], utc=True)
    in_pos = c["in_position"].astype(bool)
    if not bool(in_pos.iloc[-1]):
        return None

    entry_idx = 0
    for i in range(len(c) - 1, -1, -1):
        if not bool(in_pos.iloc[i]):
            entry_idx = i + 1
            break
    entry_row = c.iloc[entry_idx]
    entry_date = pd.Timestamp(entry_row["date"]).tz_convert("UTC").normalize()
    norm_idx = df.index.tz_convert("UTC").normalize()
    hits = norm_idx == entry_date
    if not hits.any():
        return None
    entry_ts = df.index[hits.argmax()]
    entry_px = float(df.loc[entry_ts, "open"])
    cur_close = float(df.iloc[-1]["close"])
    hold_min = effective_hold_min(strat_cfg)
    hold_max = effective_hold_max(strat_cfg)
    dynamic = uses_dynamic_hold(strat_cfg)
    hold_day = int(len(c.iloc[entry_idx:]))
    entry_i = int(hits.argmax())
    peak_close = float(df.iloc[entry_i : len(df)]["close"].max())
    fade_now = dynamic and hold_day >= hold_min and momentum_faded(
        df, len(df) - 1, peak_close=peak_close, giveback_pct=strat_cfg.hold_giveback_pct
    )
    max_exit_idx = entry_idx + hold_max - 1
    exit_target = (
        str(c.iloc[max_exit_idx]["date"])[:10]
        if max_exit_idx < len(c)
        else str(c.iloc[-1]["date"])[:10]
    )
    unrealized_pct = 100.0 * (cur_close / entry_px - 1.0)
    size_frac = float(entry_row.get("size_frac", 0.0) or 0.0)
    return {
        "entry_date": entry_date.strftime("%Y-%m-%d"),
        "entry_px": entry_px,
        "cur_close": cur_close,
        "hold_day": hold_day,
        "hold_min": hold_min,
        "hold_max": hold_max,
        "hold_days": hold_max if dynamic else hold_min,
        "dynamic_hold": dynamic,
        "momentum_fade": fade_now,
        "exit_target": exit_target,
        "unrealized_pct": unrealized_pct,
        "size_frac": size_frac,
    }


def summarize_pending_entry(
    curve: pd.DataFrame,
    *,
    open_position: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if open_position is not None or curve.empty:
        return None
    row = curve.iloc[-1]
    if bool(row.get("in_position")):
        return None
    if not bool(row.get("pending_entry")):
        return None
    size_frac = float(row.get("pending_size_frac") or 0.0)
    signal_date = row.get("pending_signal_date")
    if not signal_date:
        return None
    return {
        "signal_date": str(signal_date)[:10],
        "size_frac": size_frac,
    }


def signal_status(
    latest: dict[str, Any],
    strat_cfg: StrategyConfig,
    open_pos: dict[str, Any] | None = None,
    pending: dict[str, Any] | None = None,
) -> str:
    if open_pos:
        hold_label = (
            f"{open_pos['hold_day']}/{open_pos['hold_min']}-{open_pos['hold_max']}"
            if open_pos.get("dynamic_hold")
            else f"{open_pos['hold_day']}/{open_pos['hold_days']}"
        )
        exit_note = (
            "fade → exit next close"
            if open_pos.get("momentum_fade")
            else f"exit ≤{open_pos['exit_target']}"
        )
        return (
            f"LONG {hold_label} "
            f"{open_pos['unrealized_pct']:+.1f}% vs entry "
            f"({exit_note})"
        )
    if pending:
        if pending["size_frac"] > 0.0:
            return (
                f"pending → next open (SIG {pending['signal_date']}, "
                f"~{100.0 * pending['size_frac']:.0f}% size)"
            )
        return f"pending blocked size 0 (SIG {pending['signal_date']})"
    if latest["signal"]:
        if float(latest.get("next_size_frac") or 0.0) <= 0.0:
            return "signal blocked (size 0)"
        return "signal → next open"
    if not latest.get("regime_on", latest["bull"]):
        if latest.get("breakout_bps") is None:
            return "regime off"
        distance_bps = float(strat_cfg.buffer_bps) - float(latest["breakout_bps"])
        return f"regime off +{max(distance_bps, 0.0):.0f}bps"
    if latest.get("prior_high") is None or latest.get("breakout_bps") is None:
        return "warming up"
    breakout_bps = float(latest["breakout_bps"])
    if breakout_bps < float(strat_cfg.buffer_bps):
        return f"no breakout +{float(strat_cfg.buffer_bps) - breakout_bps:.0f}bps"
    if strat_cfg.max_breakout_bps is not None and breakout_bps > float(strat_cfg.max_breakout_bps):
        return f"too stretched {breakout_bps:.0f}bps"
    return "blocked"


def build_telegram_message(*, results: list[dict[str, Any]]) -> str:
    date = results[0]["latest"]["signal_date"][:10] if results else "n/a"
    n_sleeves = len(results)
    per_sleeve = float(results[0]["equity"]) if results else LIVE_SLEEVE_EQUITY
    starting_equity = sum(float(result["equity"]) for result in results)
    total_year_pnl = sum(float(result["year_summary"]["pnl"]) for result in results)
    year = results[0]["year_summary"]["year"] if results else pd.Timestamp.utcnow().year
    signal_date = pd.Timestamp(results[0]["latest"]["signal_date"]) if results else pd.Timestamp.utcnow()
    year_start = pd.Timestamp(f"{year}-01-01", tz="UTC")
    elapsed_years = max((signal_date - year_start).days / 365.25, 1e-9)
    year_return = total_year_pnl / starting_equity if starting_equity else 0.0
    annualized = 100.0 * ((1.0 + year_return) ** (1.0 / elapsed_years) - 1.0) if year_return > -1.0 else -100.0

    pending = [
        r["symbol"]
        for r in results
        if r.get("pending_entry")
        and float(r["pending_entry"].get("size_frac") or 0.0) > 0.0
        and not r.get("open_position")
    ]
    pending_rows = [
        r
        for r in results
        if r.get("pending_entry")
        and float(r["pending_entry"].get("size_frac") or 0.0) > 0.0
        and not r.get("open_position")
    ]
    open_rows = [r for r in results if r.get("open_position")]

    if pending:
        headline = f"{date} | signal → next open: {', '.join(pending)}"
    else:
        headline = f"{date} | no new signals"

    lines = [
        f"Breakout Paper Portfolio ({n_sleeves}×${per_sleeve:,.0f} = ${starting_equity:,.0f})",
        headline,
        "",
    ]
    flat_ytd = abs(total_year_pnl) < 1e-6
    for result in results:
        latest = result["latest"]
        strat_cfg = result["strat_cfg"]
        open_pos = result.get("open_position")
        pending_entry = result.get("pending_entry")
        year_summary = result["year_summary"]
        pnl = float(year_summary["pnl"])
        if flat_ytd:
            share_col = "—"
        else:
            share_col = f"{100.0 * pnl / total_year_pnl:.1f}%"
        lines.append(
            f"{result['symbol']} [${per_sleeve:,.0f}]: "
            f"{signal_status(latest, strat_cfg, open_pos, pending_entry)} | "
            f"{fmt_signed_money(pnl)} | {share_col}"
        )

    if pending_rows:
        lines.append("")
        lines.append("Pending entry (next open):")
        for result in pending_rows:
            pe = result["pending_entry"]
            lines.append(
                f"{result['symbol']}: SIG {pe['signal_date']} → enter next session | "
                f"~{100.0 * float(pe['size_frac']):.0f}% of sleeve"
            )

    if open_rows:
        lines.append("")
        lines.append("Open (paper):")
        for result in open_rows:
            op = result["open_position"]
            hold_txt = (
                f"day {op['hold_day']}/{op['hold_min']}-{op['hold_max']}"
                if op.get("dynamic_hold")
                else f"day {op['hold_day']}/{op['hold_days']}"
            )
            fade_txt = " | fade→exit" if op.get("momentum_fade") else f" | exit ≤{op['exit_target']}"
            lines.append(
                f"{result['symbol']}: entered {op['entry_date']} @ {op['entry_px']:.4g} | "
                f"now {op['cur_close']:.4g} ({op['unrealized_pct']:+.1f}%) | "
                f"{hold_txt} size {100 * op['size_frac']:.0f}%{fade_txt}"
            )

    lines.append("")
    lines.append(
        f"YTD ${starting_equity:,.0f}: {fmt_signed_money(total_year_pnl)} "
        f"({100.0 * year_return:+.1f}%, ann {annualized:+.1f}%)"
    )
    return "\n".join(lines)


def send_telegram_message(token: str, chat_id: str, text: str) -> None:
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    with urllib.request.urlopen(url, data=payload, timeout=20) as resp:
        resp.read()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Daily breakout paper portfolio runner")
    p.add_argument("--symbol", default=None, help="Run one symbol only; overrides --symbols")
    p.add_argument("--symbols", default=",".join(LIVE_SYMBOLS), help="Comma-separated symbols for portfolio tracking")
    p.add_argument("--base-url", default="https://api.binance.com")
    p.add_argument("--start", default="2018-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--equity", type=float, default=10_000.0, help="Fallback per-symbol equity for symbols without configured allocation")
    p.add_argument("--trend-mode", choices=TREND_MODE_CHOICES, default=None, help="Override each symbol's configured regime filter")
    p.add_argument("--state-dir", default="btc_breakout_clean/paper_portfolio")
    p.add_argument("--refresh-cache", action="store_true", help="Refresh Dukascopy cache before running")
    p.add_argument("--no-write", action="store_true", help="Print only; do not write state/log files")
    p.add_argument("--telegram-token", default=os.getenv("TELEGRAM_BOT_TOKEN"))
    p.add_argument("--telegram-chat-id", default=os.getenv("TELEGRAM_CHAT_ID"))
    p.add_argument("--no-telegram", action="store_true", help="Disable Telegram notification even if configured")
    p.add_argument("--quiet", action="store_true", help="Suppress per-symbol reports (portfolio summary only)")
    return p.parse_args()


def fetch_symbol_daily(args: argparse.Namespace, symbol: str, source: str) -> pd.DataFrame:
    if source == "binance":
        return fetch_binance_daily(symbol, args.start, args.end, args.base_url)
    if source == "dukascopy":
        return fetch_dukascopy_instrument(
            symbol,
            dukascopy_cache_path(symbol),
            args.start,
            args.end,
            include_current=False,
            refresh_cache=args.refresh_cache,
        )
    raise ValueError(f"Unsupported live source for {symbol}: {source}")


def source_label(source: str) -> str:
    if source == "binance":
        return "Binance public 1d klines"
    if source == "dukascopy":
        return "Dukascopy H1 resampled to daily"
    return source


def _simulate_symbol(
    args: argparse.Namespace,
    symbol: str,
    *,
    blocked_entry_dates: frozenset[pd.Timestamp] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any], StrategyConfig, float, str]:
    source = live_symbol_source(symbol)
    raw = fetch_symbol_daily(args, symbol, source)
    strat_cfg = live_strategy_config(symbol)
    if args.trend_mode:
        strat_cfg = replace(strat_cfg, trend_mode=args.trend_mode)
    equity = live_symbol_equity(symbol, args.equity)
    sim_cfg = SimConfig(
        source=source,
        data_start=args.start,
        sim_start=pd.Timestamp(args.start, tz="UTC"),
        end=args.end,
        equity=equity,
        include_current=False,
        cache_path=Path(""),
        dukascopy_path=dukascopy_cache_path(symbol) if source == "dukascopy" else Path(""),
        refresh_cache=False,
        show_trades=0,
        write_files=False,
        out_dir=Path("."),
        instrument=symbol.upper(),
        skip_saturday_entry=default_skip_saturday_entry(source),
        blocked_entry_dates=blocked_entry_dates or frozenset(),
    )
    df = add_indicators(raw, strat_cfg)
    trades, curve, summary = simulate_account(df, sim_cfg=sim_cfg, strat_cfg=strat_cfg)
    return df, trades, curve, summary, strat_cfg, equity, source


def portfolio_blocked_entry_dates(
    args: argparse.Namespace,
    symbols: list[str],
    max_concurrent: int,
) -> dict[str, frozenset[pd.Timestamp]]:
    """Pass 1 replay → concurrent-entry cap → blocked entry dates per sleeve (H10)."""
    parts: list[pd.DataFrame] = []
    for symbol in symbols:
        _, trades, _, _, _, _, _ = _simulate_symbol(args, symbol)
        if not trades.empty:
            t = trades.copy()
            t["sleeve"] = symbol.upper()
            parts.append(t)
    if not parts or max_concurrent <= 0:
        return {s.upper(): frozenset() for s in symbols}
    all_trades = pd.concat(parts, ignore_index=True)
    return blocked_entries_max_concurrent(all_trades, max_concurrent)


def run_symbol(
    args: argparse.Namespace,
    symbol: str,
    state_dir: Path,
    log_path: Path,
    *,
    blocked_entry_dates: frozenset[pd.Timestamp] | None = None,
) -> dict[str, Any]:
    symbol_dir = state_dir / symbol.upper()
    state_path = symbol_dir / "state.json"
    trades_path = symbol_dir / "trades.csv"
    equity_path = symbol_dir / "equity.csv"

    previous = load_previous_state(state_path)
    if not previous and symbol.upper() == "BTCUSDT":
        previous = load_previous_state(state_dir / "state.json")
    df, trades, curve, summary, strat_cfg, equity, source = _simulate_symbol(
        args,
        symbol,
        blocked_entry_dates=blocked_entry_dates,
    )
    latest = latest_signal_report(df, strat_cfg)
    open_position = summarize_open_position(curve, df, strat_cfg)
    pending_entry = summarize_pending_entry(curve, open_position=open_position)
    event = classify_event(previous, latest, trades)
    year_summary = summarize_signal_year(trades, curve, signal_date=latest["signal_date"])
    state_written = not args.no_write

    if state_written:
        write_state(state_path, trades_path, equity_path, trades, curve, summary, latest)
        append_run_log(log_path, event=event, symbol=symbol, summary=summary, latest=latest)

    quiet = bool(getattr(args, "quiet", False))
    if not quiet:
        print_bot_report(symbol, source_label(source), df, trades, curve, summary, latest, strat_cfg, state_path, state_written)
        print("-" * 92)
        print(f"  Daily event: {event}")
        print(f"  Fake equity: ${fmt(summary['final_equity'])}")
        print(f"  Current-year PnL: ${fmt(year_summary['pnl'])}")
        print(
            f"  Next action: {'PAPER ENTER LONG' if pending_entry and pending_entry.get('size_frac', 0) > 0 else 'IN POSITION' if open_position else 'NO TRADE'}"
        )
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
        "equity": equity,
        "strat_cfg": strat_cfg,
        "open_position": open_position,
        "pending_entry": pending_entry,
        "curve": curve,
        "trades": trades,
    }


def main() -> None:
    args = parse_args()
    symbols = parse_symbols(args)
    state_dir = Path(args.state_dir)
    log_path = state_dir / "run_log.csv"

    blocked_by_sym: dict[str, frozenset[pd.Timestamp]] = {s.upper(): frozenset() for s in symbols}
    if LIVE_MAX_CONCURRENT_ENTRIES > 0 and len(symbols) > 1:
        blocked_by_sym = portfolio_blocked_entry_dates(args, symbols, LIVE_MAX_CONCURRENT_ENTRIES)
        n_blocked = sum(len(v) for v in blocked_by_sym.values())
        if n_blocked:
            print(f"  Max {LIVE_MAX_CONCURRENT_ENTRIES} concurrent entries: {n_blocked} blocked signal day(s)")

    results = [
        run_symbol(
            args,
            symbol,
            state_dir,
            log_path,
            blocked_entry_dates=blocked_by_sym.get(symbol.upper(), frozenset()),
        )
        for symbol in symbols
    ]

    print("=" * 92)
    print("  DAILY PORTFOLIO SUMMARY")
    print("=" * 92)
    total_equity = sum(float(result["summary"]["final_equity"]) for result in results)
    total_year_pnl = sum(float(result["year_summary"]["pnl"]) for result in results)
    alloc_line = ", ".join(f"{s} ${live_symbol_equity(s, args.equity):,.0f}" for s in LIVE_SYMBOLS)
    print(f"  Configured: {len(LIVE_SYMBOLS)} sleeves = ${fmt(LIVE_PORTFOLIO_EQUITY)} ({alloc_line})")
    print(f"  Current fake equity:        ${fmt(total_equity)}")
    print(f"  Current-year fake PnL:      ${fmt(total_year_pnl)}")
    print("-" * 92)
    for result in results:
        latest = result["latest"]
        year_summary = result["year_summary"]
        breakout = f"{latest['breakout_bps']:.0f}bps" if latest.get("breakout_bps") is not None else "n/a"
        print(
            f"  {result['symbol']:8} alloc=${fmt(result['equity']):>9} signal={'YES' if latest['signal'] else 'NO ':3} "
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
