#!/usr/bin/env python3
"""
IBKR connectivity and per-sleeve data health report.

Prints a compact table: contract resolution, local cache, and a short live
historical probe (default 30 days). Use before/after enabling TWS or fixing
permissions so you don't parse ib_async log noise.

Usage:
  python ibkr_health_check.py
  python ibkr_health_check.py DOGEUSDT XAUUSD
  python ibkr_health_check.py --cache-only    # no TWS probe (offline)
  python ibkr_health_check.py --strict        # exit 1 if any sleeve not OK

Requires ib_async when probing live (pip install ib_async).
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import (  # noqa: E402
    LIVE_SYMBOLS,
    live_symbol_fallback_source,
)
from ibkr_data import (  # noqa: E402
    _ibkr_contract,
    _next_client_id,
    _what_to_show,
    _use_rth,
    ibkr_available,
    ibkr_cache_path,
)

_LOGGERS = ("ib_async", "ib_async.wrapper", "ib_async.client", "ib_async.ib", "ib_async.util")
_PROBE_DAYS = 30


@dataclass
class SleeveHealth:
    symbol: str
    contract: str
    cache: str
    live: str
    status: str
    note: str
    fallback: str


def _contract_label(contract: Any) -> str:
    sec = getattr(contract, "secType", "?")
    sym = getattr(contract, "symbol", "?")
    exch = getattr(contract, "exchange", "") or "?"
    return f"{sec} {sym}@{exch}"


def _cache_summary(symbol: str) -> tuple[str, int]:
    path = ibkr_cache_path(symbol)
    if not path.exists():
        return "—", 0
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index, utc=True)
        n = len(df)
        if n == 0:
            return "empty", 0
        return f"{n} rows {df.index[0].date()}→{df.index[-1].date()}", n
    except Exception as exc:
        return f"read err ({exc})", 0


def _classify_note(errors: list[tuple[int, str]], bars: int) -> str:
    if bars > 0:
        return ""
    for code, msg in reversed(errors):
        m = msg.lower()
        if code == 162 and "zerohash" in m:
            return "ZEROHASH API historical data — not a Client Portal checkbox; contact IBKR support"
        if code == 162 and "no data" in m:
            return "HMDS returned no data for window"
        if code == 200:
            return "No security definition — wrong contract or product not enabled"
        if code == 10299:
            return "Use AGGTRADES for crypto (already set in ibkr_data)"
    return "0 bars — check trading permissions or contract mapping"


def _probe_live(ib: Any, symbol: str, probe_days: int) -> tuple[int, list[tuple[int, str]]]:
    errors: list[tuple[int, str]] = []

    def _on_error(req_id: int, code: int, msg: str, *args: Any) -> None:
        if code not in (2104, 2106, 2158):  # connection OK notices
            errors.append((code, msg))

    ib.errorEvent += _on_error
    try:
        contract = _ibkr_contract(symbol)
        details = ib.reqContractDetails(contract)
        if not details:
            errors.append((200, "No contract details returned"))
            return 0, errors
        c = details[0].contract
        bars = ib.reqHistoricalData(
            c,
            endDateTime="",
            durationStr=f"{probe_days} D",
            barSizeSetting="1 day",
            whatToShow=_what_to_show(symbol),
            useRTH=_use_rth(symbol),
            formatDate=2,
            timeout=60,
        )
        return len(bars or []), errors
    finally:
        ib.errorEvent -= _on_error


def _status(bars: int, cache_rows: int, tws_up: bool, errors: list[tuple[int, str]]) -> str:
    if bars > 0:
        return "OK"
    if not tws_up:
        return "TWS_DOWN" if cache_rows > 0 else "OFFLINE"
    for code, msg in errors:
        if code == 162 and "permission" in msg.lower():
            return "NO_PERMS"
    if cache_rows > 0:
        return "CACHE_ONLY"
    for code, _ in errors:
        if code == 200:
            return "NO_CONTRACT"
    return "NO_DATA"


def check_sleeves(
    symbols: list[str],
    *,
    probe_days: int = _PROBE_DAYS,
    cache_only: bool = False,
) -> tuple[list[SleeveHealth], bool]:
    tws_up = False if cache_only else ibkr_available()
    ib: Any = None
    if tws_up:
        from ib_async import IB

        import io
        import os

        for name in _LOGGERS:
            logging.getLogger(name).setLevel(logging.CRITICAL)

        host = os.environ.get("IBKR_HOST", "127.0.0.1")
        port = int(os.environ.get("IBKR_PORT", "7497" if os.environ.get("IBKR_MODE", "paper") != "live" else "7496"))
        ib = IB()
        import sys as _sys

        _stderr, _sys.stderr = _sys.stderr, io.StringIO()
        try:
            ib.connect(host, port, clientId=_next_client_id(90), timeout=8, readonly=True)
        except Exception:
            tws_up = False
        finally:
            _sys.stderr = _stderr

    rows: list[SleeveHealth] = []
    try:
        for sym in symbols:
            sym = sym.upper()
            try:
                contract = _contract_label(_ibkr_contract(sym))
            except Exception as exc:
                rows.append(
                    SleeveHealth(
                        sym, "—", "—", "—", "CONFIG",
                        str(exc), live_symbol_fallback_source(sym),
                    )
                )
                continue

            cache_str, cache_rows = _cache_summary(sym)
            bars = 0
            errors: list[tuple[int, str]] = []
            if tws_up and ib is not None and ib.isConnected():
                bars, errors = _probe_live(ib, sym, probe_days)
            live_str = f"{bars}d" if tws_up else "—"
            note = _classify_note(errors, bars) if tws_up else (
                "TWS/Gateway not reachable — using cache/fallback in daily run"
            )
            st = _status(bars, cache_rows, tws_up, errors)
            rows.append(
                SleeveHealth(
                    sym,
                    contract,
                    cache_str,
                    live_str,
                    st,
                    note,
                    live_symbol_fallback_source(sym),
                )
            )
    finally:
        if ib is not None and ib.isConnected():
            ib.disconnect()

    return rows, tws_up


def _print_report(rows: list[SleeveHealth], *, tws_hint: bool) -> None:
    import os

    mode = os.environ.get("IBKR_MODE", "paper")
    port = os.environ.get("IBKR_PORT", "7497" if mode != "live" else "7496")
    host = os.environ.get("IBKR_HOST", "127.0.0.1")

    print()
    print("=" * 88)
    print(f"  IBKR HEALTH  [{mode}]  {host}:{port}")
    print("=" * 88)
    print(f"  {'SYMBOL':<10} {'STATUS':<12} {'LIVE':<6} {'CACHE':<28} {'CONTRACT':<22}")
    print("-" * 88)
    for r in rows:
        print(f"  {r.symbol:<10} {r.status:<12} {r.live:<6} {r.cache:<28} {r.contract:<22}")
        if r.note:
            print(f"             └─ {r.note}")
        if r.status != "OK":
            print(f"             └─ fallback: {r.fallback}")
    print("-" * 88)
    ok = sum(1 for r in rows if r.status == "OK")
    print(f"  {ok}/{len(rows)} sleeves OK on IBKR live probe")
    if tws_hint:
        print("  TWS down — start TWS/Gateway with API enabled (paper 7497 / live 7496)")
    print("=" * 88)
    print()


def main() -> int:
    p = argparse.ArgumentParser(description="IBKR per-sleeve health table")
    p.add_argument("symbols", nargs="*", help="Subset of sleeves (default: all LIVE_SYMBOLS)")
    p.add_argument("--probe-days", type=int, default=_PROBE_DAYS, help="Days for live historical probe")
    p.add_argument("--cache-only", action="store_true", help="Skip TWS connection; report cache only")
    p.add_argument("--strict", action="store_true", help="Exit 1 unless every sleeve status is OK")
    args = p.parse_args()

    symbols = [s.upper() for s in (args.symbols or LIVE_SYMBOLS)]
    rows, tws_up = check_sleeves(symbols, probe_days=args.probe_days, cache_only=args.cache_only)
    _print_report(rows, tws_hint=not args.cache_only and not tws_up)

    if args.strict and any(r.status != "OK" for r in rows):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
