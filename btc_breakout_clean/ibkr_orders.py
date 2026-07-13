#!/usr/bin/env python3
"""
IBKR order execution layer for the Simona breakout book.

Paper ↔ live is a single environment variable:

    IBKR_MODE=paper   (default) → TWS paper port 7497, orders go to paper account
    IBKR_MODE=live              → TWS live  port 7496, real money

No other code changes are needed to go live.

Workflow called once per daily run (after signal computation):

    results = [run_symbol(...) for symbol in LIVE_SYMBOLS]
    execute_daily_signals(results, dry_run=False)

For each sleeve the function:
  1. Checks IBKR actual positions (reconcile paper state vs real account)
  2. If signal → pending_entry and not already in IBKR → submit market BUY at next open
  3. If simulation exited but IBKR still holds → submit market SELL

Order type: MKT, tif=OPG (at-the-open auction for BNO/equity)
            MKT, tif=DAY  for crypto and metals (24h markets)

Sizing:
    qty = floor(size_frac × sleeve_equity / last_close)   # BNO: whole shares
    qty = round(size_frac × sleeve_equity / last_close, 8) # Crypto: fractional
    qty in oz for metals (IBKR IDEALPRO minimum = 1 oz)

Safety gates:
  • If IBKR_MODE != "live"  → dry_run is forced True (never places real orders unless explicit)
  • Minimum order value $50 (prevents dust trades from vol-targeting tiny fracs)
  • Max single order notional = sleeve_equity (sanity cap)
  • All orders logged to ibkr_orders_log.csv regardless of mode

Usage:
    from ibkr_orders import execute_daily_signals
    execute_daily_signals(results, dry_run=os.environ.get("IBKR_MODE") != "live")
"""

from __future__ import annotations

import csv
import logging
import math
import os
from pathlib import Path
from typing import Any

import pandas as pd

from ibkr_data import (
    _DEFAULT_HOST,
    _DEFAULT_PORT,
    _ibkr_contract,
    ibkr_available,
)

logger = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
ORDER_LOG = HERE / "ibkr_orders_log.csv"

# ── Mode helpers ──────────────────────────────────────────────────────────────

def _live_mode() -> bool:
    return os.environ.get("IBKR_MODE", "paper").lower() == "live"


def _tws_port() -> int:
    explicit = os.environ.get("IBKR_PORT")
    if explicit:
        return int(explicit)
    return 7496 if _live_mode() else 7497


def _tws_host() -> str:
    return os.environ.get("IBKR_HOST", _DEFAULT_HOST)


# ── Quantity sizing ───────────────────────────────────────────────────────────

_FRACTIONAL_CRYPTO = {"BTCUSD", "ETHUSDT", "SOLUSDT"}
_METALS = {"XAUUSD", "XAGUSD"}
_MIN_ORDER_VALUE = 50.0   # USD — skip dust positions


def _qty(symbol: str, size_frac: float, sleeve_equity: float, last_close: float) -> float:
    if last_close <= 0 or size_frac <= 0:
        return 0.0
    notional = min(size_frac * sleeve_equity, sleeve_equity)
    raw = notional / last_close
    sym = symbol.upper()
    if sym in _FRACTIONAL_CRYPTO:
        return round(raw, 8)       # IBKR Paxos supports fractional
    if sym in _METALS:
        return max(1.0, round(raw, 2))   # IDEALPRO in oz, min 1 oz
    return float(max(1, math.floor(raw)))  # ETF: whole shares


# ── Logging ───────────────────────────────────────────────────────────────────

def _log_order(row: dict[str, Any]) -> None:
    ORDER_LOG.parent.mkdir(parents=True, exist_ok=True)
    write_header = not ORDER_LOG.exists()
    with open(ORDER_LOG, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if write_header:
            w.writeheader()
        w.writerow(row)


# ── IBKR position query ───────────────────────────────────────────────────────

def _get_ibkr_positions(ib: Any) -> dict[str, float]:
    """Return {symbol_upper: qty} for all open positions in the account."""
    positions: dict[str, float] = {}
    try:
        for pos in ib.positions():
            sym = pos.contract.symbol.upper()
            positions[sym] = float(pos.position)
    except Exception as exc:
        logger.warning("Could not read IBKR positions: %s", exc)
    return positions


# ── Order submission ──────────────────────────────────────────────────────────

def _submit_order(
    ib: Any,
    symbol: str,
    action: str,   # "BUY" or "SELL"
    qty: float,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """Place a market order. Returns a log row dict."""
    from ib_async import MarketOrder

    contract = _ibkr_contract(symbol)
    sym = symbol.upper()
    tif = "OPG" if sym == "BNO" else "DAY"   # BNO uses opening auction; 24h assets use DAY
    order = MarketOrder(action, qty, tifCondition=None)
    order.tif = tif

    mode = "LIVE" if _live_mode() else "PAPER"
    ts = pd.Timestamp.utcnow().isoformat()
    log_row = {
        "timestamp_utc": ts,
        "mode": mode,
        "dry_run": dry_run,
        "symbol": sym,
        "action": action,
        "qty": qty,
        "tif": tif,
        "status": "PENDING",
        "order_id": "",
        "error": "",
    }

    if dry_run:
        log_row["status"] = "DRY_RUN"
        _log_order(log_row)
        logger.info("[DRY RUN] %s %s %s qty=%.8g tif=%s", mode, action, sym, qty, tif)
        return log_row

    try:
        trade = ib.placeOrder(contract, order)
        ib.sleep(1)   # give TWS a moment to assign order ID
        log_row["order_id"] = str(trade.order.orderId)
        log_row["status"] = str(trade.orderStatus.status)
        logger.info("[%s] Placed %s %s qty=%.8g → orderId=%s status=%s",
                    mode, action, sym, qty, trade.order.orderId, trade.orderStatus.status)
    except Exception as exc:
        log_row["status"] = "ERROR"
        log_row["error"] = str(exc)
        logger.error("[%s] Order failed %s %s qty=%.8g: %s", mode, action, sym, qty, exc)

    _log_order(log_row)
    return log_row


# ── Main entry point ──────────────────────────────────────────────────────────

def execute_daily_signals(
    results: list[dict[str, Any]],
    *,
    dry_run: bool = True,
) -> list[dict[str, Any]]:
    """
    Given the output of run_symbol() for each live sleeve, reconcile against
    actual IBKR positions and submit any required BUY / SELL orders.

    Parameters
    ----------
    results  : list returned by [run_symbol(...) for symbol in LIVE_SYMBOLS]
    dry_run  : if True, log intended orders but do NOT submit to IBKR.
               Forced True when IBKR_MODE != "live" regardless of this arg.

    Returns list of log row dicts (one per order attempted).
    """
    # Safety: never place real orders unless explicitly in live mode.
    if not _live_mode():
        dry_run = True

    mode_label = "LIVE" if _live_mode() else "PAPER"
    host = _tws_host()
    port = _tws_port()

    if not ibkr_available(host, port):
        logger.warning(
            "IBKR %s (%s:%s) unreachable — skipping order execution.",
            mode_label, host, port,
        )
        return []

    from ib_async import IB

    ib = IB()
    order_logs: list[dict[str, Any]] = []

    try:
        ib.connect(host, port, clientId=43, readonly=False, timeout=15)
        ibkr_positions = _get_ibkr_positions(ib)

        for result in results:
            symbol = result["symbol"]
            sym = symbol.upper()
            pending  = result.get("pending_entry")
            open_pos = result.get("open_position")
            summary  = result.get("summary", {})
            curve    = result.get("curve", pd.DataFrame())

            # Current sleeve equity & last close from simulation output
            sleeve_equity = float(result.get("equity", 14_286))
            last_close    = float(summary.get("final_close") or
                                  (curve.iloc[-1]["close"] if not curve.empty and "close" in curve.columns else 0))

            ibkr_qty = ibkr_positions.get(sym, 0.0)
            ibkr_holds = ibkr_qty > 0.0

            # ── ENTRY ────────────────────────────────────────────────────────
            # Simulation says enter tomorrow at open AND IBKR has no position.
            if pending and not ibkr_holds:
                size_frac = float(pending.get("size_frac") or 0.0)
                if size_frac <= 0:
                    continue
                qty = _qty(sym, size_frac, sleeve_equity, last_close)
                if qty * last_close < _MIN_ORDER_VALUE:
                    logger.info("%s: order value $%.2f < min $%.0f — skipping",
                                sym, qty * last_close, _MIN_ORDER_VALUE)
                    continue
                log = _submit_order(ib, sym, "BUY", qty, dry_run=dry_run)
                log["size_frac"] = size_frac
                log["notional_usd"] = round(qty * last_close, 2)
                order_logs.append(log)

            # ── EXIT ─────────────────────────────────────────────────────────
            # Simulation is flat (no open_pos) AND IBKR still holds.
            elif not open_pos and ibkr_holds:
                log = _submit_order(ib, sym, "SELL", ibkr_qty, dry_run=dry_run)
                log["ibkr_qty"] = ibkr_qty
                order_logs.append(log)

            # ── HOLD / IDLE ──────────────────────────────────────────────────
            else:
                status = "IN_POSITION" if open_pos else "FLAT"
                logger.debug("%s: %s — no order needed (ibkr_holds=%s)", sym, status, ibkr_holds)

    finally:
        ib.disconnect()

    if not dry_run and order_logs:
        print(f"\n  [{mode_label}] {len(order_logs)} order(s) submitted — see {ORDER_LOG.name}")
    elif dry_run and order_logs:
        print(f"\n  [DRY RUN / {mode_label}] {len(order_logs)} order(s) would be placed:")
        for o in order_logs:
            notional = f"  ~${o.get('notional_usd', '?')}" if "notional_usd" in o else ""
            print(f"    {o['action']:4} {o['symbol']:8} qty={o['qty']:.8g}{notional}")

    return order_logs


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(HERE))

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(description="Show IBKR mode / connection status")
    p.parse_args()

    mode = "LIVE" if _live_mode() else "PAPER"
    host, port = _tws_host(), _tws_port()
    avail = ibkr_available(host, port)
    print(f"Mode   : {mode}")
    print(f"Host   : {host}:{port}")
    print(f"TWS up : {avail}")
    if avail:
        from ib_async import IB
        ib = IB()
        ib.connect(host, port, clientId=44, readonly=True, timeout=10)
        positions = _get_ibkr_positions(ib)
        ib.disconnect()
        if positions:
            print(f"Positions: {positions}")
        else:
            print("Positions: none")
