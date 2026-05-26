#!/usr/bin/env python3
"""
Sleeve trade-flow auditor — step checklist for manual validation (research / paper).

Mirrors live rules from btc_breakout_paper_sim without placing orders.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from btc_breakout_binance_paper_bot import (
    BINANCE_BASE_URL_FALLBACKS,
    LIVE_CRYPTO_SYMBOLS,
    LIVE_MAX_CONCURRENT_ENTRIES,
    LIVE_SLEEVE_EQUITY,
    LIVE_SYMBOLS,
    live_strategy_config,
    live_symbol_source,
)
from btc_breakout_paper_sim import StrategyConfig, add_indicators, default_skip_saturday_entry
from signal_forecast import _regime_margin_bps, load_daily_bars

HERE = Path(__file__).resolve().parent

BINANCE_PAIR = {
    "BTCUSD": "BTCUSDT",
    "ETHUSDT": "ETHUSDT",
    "BNBUSDT": "BNBUSDT",
    "SOLUSDT": "SOLUSDT",
    "DOGEUSDT": "DOGEUSDT",
}

TV_CHART_HINT = {
    "BTCUSD": "OANDA:BTCUSD (approx) or Dukascopy feed",
    "ETHUSDT": "BINANCE:ETHUSDT",
    "BNBUSDT": "BINANCE:BNBUSDT",
    "SOLUSDT": "BINANCE:SOLUSDT",
    "DOGEUSDT": "BINANCE:DOGEUSDT",
    "XAUUSD": "OANDA:XAUUSD",
    "XAGUSD": "OANDA:XAGUSD",
    "BRENT": "TVC:UKOIL",
}


def journal_path(symbol: str) -> Path:
    return HERE / "paper_portfolio" / symbol.upper() / "flow_journal.jsonl"


def append_journal(symbol: str, *, event: str, note: str, expected: str = "", actual: str = "") -> None:
    path = journal_path(symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts_utc": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "expected": expected,
        "actual": actual,
        "note": note,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def load_journal(symbol: str, limit: int = 30) -> list[dict[str, Any]]:
    path = journal_path(symbol)
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    out: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(out))


def fetch_binance_ticker(pair: str) -> float | None:
    for base in BINANCE_BASE_URL_FALLBACKS:
        try:
            url = f"{base.rstrip('/')}/api/v3/ticker/price?{urllib.parse.urlencode({'symbol': pair.upper()})}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return float(data["price"])
        except Exception:
            continue
    return None


def fetch_binance_intraday_daily(pair: str) -> dict[str, float] | None:
    """Today's developing daily candle from Binance 1d kline."""
    for base in BINANCE_BASE_URL_FALLBACKS:
        try:
            url = f"{base.rstrip('/')}/api/v3/klines?{urllib.parse.urlencode({'symbol': pair.upper(), 'interval': '1d', 'limit': 1})}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                chunk = json.loads(resp.read().decode("utf-8"))
            if not chunk:
                return None
            r = chunk[-1]
            return {
                "open": float(r[1]),
                "high": float(r[2]),
                "low": float(r[3]),
                "close": float(r[4]),
                "volume": float(r[5]),
            }
        except Exception:
            continue
    return None


def _row_from_latest(latest: dict[str, Any]) -> pd.Series:
    """Official close row from daily bot state (no market replay)."""
    return pd.Series(
        {
            "close": latest.get("close"),
            "prior_high": latest.get("prior_high"),
            "breakout_bps": latest.get("breakout_bps"),
            "regime_on": latest.get("regime_on", latest.get("bull")),
            "bull": latest.get("bull"),
            "signal": latest.get("signal"),
            "sma200": latest.get("sma200"),
            "vol20": latest.get("vol20"),
        }
    )


def build_provisional_df(symbol: str, strat_cfg: StrategyConfig) -> tuple[pd.DataFrame, str]:
    """Append/update today's bar with live Binance 1d kline (crypto only)."""
    sym = symbol.upper()
    if sym not in LIVE_CRYPTO_SYMBOLS and sym != "BTCUSD":
        return pd.DataFrame(), "provisional: official daily feed only (not Binance)"

    pair = BINANCE_PAIR.get(sym, sym if sym.endswith("USDT") else None)
    if not pair:
        return pd.DataFrame(), "provisional: no Binance pair"
    raw = load_daily_bars(pair)
    if raw.empty:
        return raw, "no data"
    live = fetch_binance_intraday_daily(pair)
    if live is None:
        px = fetch_binance_ticker(pair)
        if px is None:
            return raw, "provisional: could not fetch Binance price"
        last = raw.iloc[-1]
        live = {
            "open": float(last["open"]),
            "high": max(float(last["high"]), px),
            "low": min(float(last["low"]), px),
            "close": px,
            "volume": float(last.get("volume", 0.0)),
        }

    now = pd.Timestamp.now(tz="UTC").normalize()
    work = raw.copy()
    if len(work) and work.index[-1].normalize() >= now:
        work.iloc[-1, work.columns.get_loc("close")] = live["close"]
        work.iloc[-1, work.columns.get_loc("high")] = live["high"]
        work.iloc[-1, work.columns.get_loc("low")] = live["low"]
        if "volume" in work.columns:
            work.iloc[-1, work.columns.get_loc("volume")] = live["volume"]
    else:
        row = pd.DataFrame([live], index=pd.DatetimeIndex([now], tz="UTC"))
        work = pd.concat([work, row])
    df = add_indicators(work, strat_cfg)
    return df, f"provisional: Binance 1d developing bar @ {live['close']:,.4g}"


def _step(
    step_id: str,
    label: str,
    *,
    passed: bool,
    expected: str,
    actual: str,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "id": step_id,
        "label": label,
        "pass": passed,
        "expected": expected,
        "actual": actual,
        "detail": detail,
    }


def _raw_breakout_row(row: pd.Series, strat_cfg: StrategyConfig) -> bool:
    ph = float(row["prior_high"]) if np.isfinite(row.get("prior_high", np.nan)) else np.nan
    if not np.isfinite(ph) or ph <= 0:
        return False
    level = ph * (1.0 + strat_cfg.buffer_bps / 10_000.0)
    return float(row["close"]) > level


def build_flow_steps(
    row: pd.Series,
    strat_cfg: StrategyConfig,
    symbol: str,
    *,
    pending_entry: dict[str, Any] | None,
    open_position: dict[str, Any] | None,
    blocked_tomorrow: bool,
    mode: str,
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    regime_on = bool(row.get("regime_on", row.get("bull", False)))
    bps = float(row["breakout_bps"]) if np.isfinite(row.get("breakout_bps", np.nan)) else None
    raw_bo = _raw_breakout_row(row, strat_cfg)
    exhausted = (
        strat_cfg.max_breakout_bps is not None
        and bps is not None
        and bps > float(strat_cfg.max_breakout_bps)
    )
    signal = bool(row.get("signal", False))
    margin = _regime_margin_bps(row, strat_cfg.trend_mode)

    steps.append(
        _step(
            "regime",
            "Regime filter",
            passed=regime_on,
            expected=f"{strat_cfg.trend_mode} ON",
            actual="ON" if regime_on else "OFF",
            detail=f"margin {margin:+.0f} bps" if margin is not None else "",
        )
    )
    steps.append(
        _step(
            "breakout",
            "Close above prior high + buffer",
            passed=raw_bo,
            expected=f"> {strat_cfg.buffer_bps:.0f} bps buffer",
            actual=f"{bps:.0f} bps" if bps is not None else "n/a",
            detail=f"need +{max(0.0, strat_cfg.buffer_bps - (bps or 0)):.0f} bps" if bps is not None and not raw_bo else "",
        )
    )
    if strat_cfg.max_breakout_bps is not None:
        steps.append(
            _step(
                "exhaustion",
                "Exhaustion cap",
                passed=not exhausted,
                expected=f"≤ {strat_cfg.max_breakout_bps:.0f} bps",
                actual=f"{bps:.0f} bps" if bps is not None else "n/a",
            )
        )
    steps.append(
        _step(
            "signal",
            "Combined signal (official close)",
            passed=signal,
            expected="ON at daily close" if mode == "official" else "provisional",
            actual="YES" if signal else "NO",
        )
    )

    if open_position:
        op = open_position
        steps.append(
            _step(
                "position",
                "In position",
                passed=True,
                expected="LONG",
                actual=f"day {op.get('hold_day')}/{op.get('hold_max', op.get('hold_days'))}",
                detail=f"unreal {op.get('unrealized_pct', 0):+.1f}%",
            )
        )
        if op.get("momentum_fade"):
            steps.append(
                _step(
                    "fade",
                    "Momentum fade",
                    passed=True,
                    expected="exit next close",
                    actual="TRIGGERED",
                )
            )
        if op.get("stop_px"):
            steps.append(
                _step(
                    "stop",
                    "Hard stop (crypto)",
                    passed=True,
                    expected=f"floor {op['stop_px']:,.2f}",
                    actual=f"cushion {op.get('stop_dist_pct', 0):+.1f}%",
                )
            )
    elif pending_entry:
        pe = pending_entry
        steps.append(
            _step(
                "armed",
                "SIG armed → next open",
                passed=pe.get("size_frac", 0) > 0,
                expected=f"enter ~{100*float(pe.get('size_frac',0)):.0f}% size",
                actual=f"SIG {pe.get('signal_date')}",
            )
        )
        steps.append(
            _step(
                "cap",
                "Portfolio max-4 cap",
                passed=not blocked_tomorrow,
                expected="slot available",
                actual="BLOCKED tomorrow" if blocked_tomorrow else "OK",
            )
        )
        tomorrow = pd.Timestamp.now(tz="UTC").normalize() + pd.Timedelta(days=1)
        skip_sat = default_skip_saturday_entry(live_symbol_source(symbol)) and tomorrow.dayofweek == 5
        steps.append(
            _step(
                "saturday",
                "Saturday entry skip",
                passed=not skip_sat,
                expected="no Sat entry (Dukascopy sleeves)" if default_skip_saturday_entry(live_symbol_source(symbol)) else "n/a",
                actual="BLOCKED Sat" if skip_sat else "OK",
            )
        )
    else:
        gap = (strat_cfg.buffer_bps - bps) if bps is not None and bps < strat_cfg.buffer_bps else 0.0
        steps.append(
            _step(
                "flat",
                "Flat — monitoring",
                passed=not signal,
                expected="wait for SIG",
                actual=f"+{gap:.0f} bps to buffer" if gap > 0 else "at/above buffer",
            )
        )

    return steps


def infer_flow_state(
    *,
    open_position: dict[str, Any] | None,
    pending_entry: dict[str, Any] | None,
    latest: dict[str, Any],
    blocked_tomorrow: bool,
    gap_bps: float | None,
) -> str:
    if open_position:
        if open_position.get("momentum_fade"):
            return "LONG — exit likely next close"
        return "LONG"
    if pending_entry:
        if float(pending_entry.get("size_frac") or 0) <= 0:
            return "BLOCKED (size 0)"
        if blocked_tomorrow:
            return "ARMED — cap blocks entry"
        return "ENTER at next open"
    if latest.get("signal"):
        return "SIG today (pending arm)"
    if gap_bps is not None and gap_bps <= 25 and latest.get("regime_on", latest.get("bull")):
        return "APPROACHING"
    return "FLAT"


def audit_sleeve(
    symbol: str,
    *,
    latest: dict[str, Any],
    strat_cfg: StrategyConfig,
    pending_entry: dict[str, Any] | None = None,
    open_position: dict[str, Any] | None = None,
    blocked_dates: frozenset[pd.Timestamp] | None = None,
    include_provisional: bool = True,
) -> dict[str, Any]:
    sym = symbol.upper()
    official_bar_date = "—"
    if latest.get("prior_high") is not None:
        row_off = _row_from_latest(latest)
        official_bar_date = str(latest.get("signal_date") or "")[:10] or "—"
    else:
        raw = load_daily_bars(sym)
        df_official = add_indicators(raw, strat_cfg)
        row_off = df_official.iloc[-1] if not df_official.empty else pd.Series(dtype=float)
        if not df_official.empty:
            official_bar_date = str(df_official.index[-1].date())

    today = pd.Timestamp.now(tz="UTC").normalize()
    tomorrow = today + pd.Timedelta(days=1)
    blocked = blocked_dates or frozenset()
    blocked_tomorrow = tomorrow in blocked or any(
        pd.to_datetime(d, utc=True).normalize() == tomorrow for d in blocked
    )

    steps_off = build_flow_steps(
        row_off,
        strat_cfg,
        sym,
        pending_entry=pending_entry,
        open_position=open_position,
        blocked_tomorrow=blocked_tomorrow,
        mode="official",
    )

    prov_note = ""
    prov_signal = None
    prov_bps = None
    if include_provisional:
        df_prov, prov_note = build_provisional_df(sym, strat_cfg)
        if not df_prov.empty:
            row_p = df_prov.iloc[-1]
            prov_signal = bool(row_p.get("signal", False))
            prov_bps = float(row_p["breakout_bps"]) if np.isfinite(row_p.get("breakout_bps", np.nan)) else None

    gap = None
    bps = latest.get("breakout_bps")
    if bps is not None:
        gap = max(0.0, float(strat_cfg.buffer_bps) - float(bps))

    state = infer_flow_state(
        open_position=open_position,
        pending_entry=pending_entry,
        latest=latest,
        blocked_tomorrow=blocked_tomorrow,
        gap_bps=gap,
    )

    notional = LIVE_SLEEVE_EQUITY * float(
        (pending_entry or {}).get("size_frac")
        or (open_position or {}).get("size_frac")
        or latest.get("next_size_frac")
        or 0.0
    )

    return {
        "symbol": sym,
        "state": state,
        "data_source": live_symbol_source(sym),
        "tv_chart": TV_CHART_HINT.get(sym, "—"),
        "official_bar_date": official_bar_date,
        "steps": steps_off,
        "provisional_note": prov_note,
        "provisional_signal": prov_signal,
        "provisional_breakout_bps": prov_bps,
        "gap_to_buffer_bps": gap,
        "blocked_tomorrow": blocked_tomorrow,
        "expected_notional": notional,
        "as_of_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def sleeve_brief_state(
    *,
    latest: dict[str, Any],
    strat_cfg: StrategyConfig,
    pending_entry: dict[str, Any] | None = None,
    open_position: dict[str, Any] | None = None,
    blocked_dates: frozenset[pd.Timestamp] | None = None,
) -> str:
    """One-line state from saved paper state (no market fetch)."""
    tomorrow = pd.Timestamp.now(tz="UTC").normalize() + pd.Timedelta(days=1)
    blocked = blocked_dates or frozenset()
    blocked_tomorrow = tomorrow in blocked or any(
        pd.to_datetime(d, utc=True).normalize() == tomorrow for d in blocked
    )
    gap = None
    bps = latest.get("breakout_bps")
    if bps is not None:
        gap = max(0.0, float(strat_cfg.buffer_bps) - float(bps))
    return infer_flow_state(
        open_position=open_position,
        pending_entry=pending_entry,
        latest=latest,
        blocked_tomorrow=blocked_tomorrow,
        gap_bps=gap,
    )


_ACTION_PRIORITY = {
    "EXIT": 0,
    "ENTER": 1,
    "ARMED": 2,
    "CAP_BLOCK": 3,
    "APPROACH": 4,
    "FLAT": 5,
    "MISSING": 6,
    "ERROR": 7,
}


def _blocked_tomorrow(blocked_dates: frozenset[pd.Timestamp] | None) -> bool:
    tomorrow = pd.Timestamp.now(tz="UTC").normalize() + pd.Timedelta(days=1)
    blocked = blocked_dates or frozenset()
    return tomorrow in blocked or any(pd.to_datetime(d, utc=True).normalize() == tomorrow for d in blocked)


def classify_sleeve_action(
    symbol: str,
    r: dict[str, Any] | None,
    *,
    blocked_dates: frozenset[pd.Timestamp] | None,
) -> dict[str, Any]:
    sym = symbol.upper()
    if r is None:
        return {
            "symbol": sym,
            "action": "MISSING",
            "priority": _ACTION_PRIORITY["MISSING"],
            "headline": "No paper state — run daily bot",
            "detail": "",
        }
    if r.get("load_error"):
        return {
            "symbol": sym,
            "action": "ERROR",
            "priority": _ACTION_PRIORITY["ERROR"],
            "headline": "Failed to load",
            "detail": str(r.get("load_error", ""))[:120],
        }

    latest = r.get("latest") or {}
    pending = r.get("pending_entry")
    open_pos = r.get("open_position")
    strat_cfg = r["strat_cfg"]
    blocked_tomorrow = _blocked_tomorrow(blocked_dates)
    state = sleeve_brief_state(
        latest=latest,
        strat_cfg=strat_cfg,
        pending_entry=pending,
        open_position=open_pos,
        blocked_dates=blocked_dates,
    )

    if open_pos and open_pos.get("momentum_fade"):
        return {
            "symbol": sym,
            "action": "EXIT",
            "priority": _ACTION_PRIORITY["EXIT"],
            "headline": "Exit likely next close (momentum fade)",
            "detail": state,
        }
    if open_pos:
        hold = f"{open_pos.get('hold_day')}/{open_pos.get('hold_max', open_pos.get('hold_days'))}"
        return {
            "symbol": sym,
            "action": "EXIT",
            "priority": _ACTION_PRIORITY["EXIT"] + 0.5,
            "headline": f"Holding LONG — day {hold}",
            "detail": state,
        }
    if pending and float(pending.get("size_frac") or 0) > 0:
        sig_d = pending.get("signal_date", "?")
        if blocked_tomorrow:
            return {
                "symbol": sym,
                "action": "CAP_BLOCK",
                "priority": _ACTION_PRIORITY["CAP_BLOCK"],
                "headline": f"SIG {sig_d} — portfolio cap blocks entry",
                "detail": state,
            }
        return {
            "symbol": sym,
            "action": "ENTER",
            "priority": _ACTION_PRIORITY["ENTER"],
            "headline": f"Enter at next session open (SIG {sig_d})",
            "detail": state,
        }
    if latest.get("signal"):
        return {
            "symbol": sym,
            "action": "ARMED",
            "priority": _ACTION_PRIORITY["ARMED"],
            "headline": "Signal today — pending arm",
            "detail": state,
        }
    bps = latest.get("breakout_bps")
    gap = max(0.0, float(strat_cfg.buffer_bps) - float(bps)) if bps is not None else None
    if gap is not None and gap <= 25 and latest.get("regime_on", latest.get("bull")):
        return {
            "symbol": sym,
            "action": "APPROACH",
            "priority": _ACTION_PRIORITY["APPROACH"],
            "headline": f"Approaching buffer (+{gap:.0f} bps)",
            "detail": state,
        }
    return {
        "symbol": sym,
        "action": "FLAT",
        "priority": _ACTION_PRIORITY["FLAT"],
        "headline": "Flat — no action",
        "detail": state,
    }


def build_action_queue(
    results: list[dict[str, Any]],
    blocked_by_sym: dict[str, frozenset[pd.Timestamp]],
) -> list[dict[str, Any]]:
    sym_map = {r["symbol"]: r for r in results}
    queue = [
        classify_sleeve_action(
            sym,
            sym_map.get(sym),
            blocked_dates=blocked_by_sym.get(sym.upper(), frozenset()),
        )
        for sym in LIVE_SYMBOLS
    ]
    return sorted(queue, key=lambda x: (x["priority"], x["symbol"]))


def book_flow_context(book: dict[str, Any], queue: list[dict[str, Any]]) -> dict[str, str]:
    n_enter = sum(1 for q in queue if q["action"] == "ENTER")
    n_cap = sum(1 for q in queue if q["action"] == "CAP_BLOCK")
    n_exit = sum(1 for q in queue if q["action"] == "EXIT" and "fade" in q["headline"].lower())
    n_hold = sum(1 for q in queue if q["action"] == "EXIT" and "Holding" in q["headline"])

    if n_enter:
        return {
            "phase": "ENTRY WINDOW",
            "instruction": f"{n_enter} sleeve(s) enter at the next session open. Confirm book cap, size, then log fills.",
        }
    if n_cap:
        return {
            "phase": "CAP BLOCKED",
            "instruction": f"{n_cap} sleeve(s) have a signal but max-{book['max_concurrent']} cap blocks entry. No orders.",
        }
    if n_exit:
        return {
            "phase": "EXIT WATCH",
            "instruction": "Momentum fade triggered — verify exit at next close on affected sleeves.",
        }
    if n_hold:
        return {
            "phase": "IN TRADE",
            "instruction": f"{book['open_count']} open — monitor holds and stops; no new entries unless slots free.",
        }
    if book["open_count"] >= book["max_concurrent"]:
        return {
            "phase": "BOOK FULL",
            "instruction": "All entry slots used. Scan for exits only.",
        }
    return {
        "phase": "SCAN",
        "instruction": "No entries today. Scan approaching setups; official SIG only at daily close.",
    }


def enrich_pipeline(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark each rule step as done | current (first fail) | pending."""
    out: list[dict[str, Any]] = []
    found_current = False
    for step in steps:
        row = dict(step)
        if found_current:
            row["status"] = "pending"
        elif not step["pass"] and step["id"] not in ("flat",):
            row["status"] = "current"
            found_current = True
        else:
            row["status"] = "done"
        out.append(row)
    if not found_current and out:
        out[-1]["status"] = "current"
    return out


def primary_instruction(audit: dict[str, Any], action: dict[str, Any]) -> str:
    act = action["action"]
    if act == "ENTER":
        n = audit.get("expected_notional") or 0
        return f"Place paper/long order ~${n:,.0f} at next session open. Log fill below when done."
    if act == "CAP_BLOCK":
        return "Do not enter — portfolio already at max concurrent positions for this signal date."
    if act == "EXIT" and "fade" in action["headline"].lower():
        return "Prepare to exit at next daily close unless fade clears. Log exit when closed."
    if act == "EXIT":
        return "Hold — watch hold-day count and hard stop (crypto). Log exit on close."
    if act == "APPROACH":
        gap = audit.get("gap_to_buffer_bps")
        g = f"{gap:.0f} bps" if gap is not None else "—"
        return f"Not actionable yet — needs +{g} to buffer with regime on."
    if act == "ARMED":
        return "Signal fired today; bot should arm entry for next open on next daily run."
    return "No trade action. Wait for official daily close."


def book_snapshot(results: list[dict[str, Any]]) -> dict[str, Any]:
    in_trade = [r["symbol"] for r in results if r.get("open_position")]
    pending = [
        r["symbol"]
        for r in results
        if r.get("pending_entry") and float(r["pending_entry"].get("size_frac") or 0) > 0
    ]
    return {
        "max_concurrent": LIVE_MAX_CONCURRENT_ENTRIES,
        "open_count": len(in_trade),
        "open_symbols": in_trade,
        "pending_count": len(pending),
        "pending_symbols": pending,
        "slots_free": max(0, LIVE_MAX_CONCURRENT_ENTRIES - len(in_trade)),
    }
