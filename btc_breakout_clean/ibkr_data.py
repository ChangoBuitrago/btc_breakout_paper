#!/usr/bin/env python3
"""
IBKR historical daily bar loader for the Simona paper book.

Fetches `1 day` bars directly from TWS / IB Gateway via ib_async (v2.x).
Falls back to the previous proxy source (Binance / Dukascopy / yfinance)
when TWS is unreachable — so the daily bot never fails at 00:10 UTC because
TWS is offline.

Requires ib_async:
  pip install ib_async

Connection defaults:
  host  : 127.0.0.1
  port  : 7497   (TWS paper)   or 4002 (IB Gateway paper)
  live  : 7496 / 4001
  client: 42

Set env vars to override at runtime:
  IBKR_HOST, IBKR_PORT, IBKR_CLIENT_ID

IBKR pacing rule: max 60 historical-data requests per 10-minute window.
  The loader serialises requests with a 2-second inter-request pause by
  default and chunks long histories into ≤1-year requests.

Data-quality notes per sleeve:
  BTC / ETH / SOL / DOGE  — Crypto(symbol, 'PAXOS', 'USD')
      whatToShow='TRADES', useRTH=False  (crypto trades around the clock)
  XAU / XAG               — Commodity('XAUUSD'/'XAGUSD', 'SMART', 'USD')
      whatToShow='MIDPOINT', useRTH=False  (Forex IDEALPRO returns no contract on many accounts)
  BNO                     — Stock('BNO', 'ARCA', 'USD')
      whatToShow='TRADES', useRTH=True
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)

IBKR_CACHE_DIR = HERE / "cache"

# Default TWS connection parameters.
# Port auto-selects based on IBKR_MODE env var:
#   IBKR_MODE=paper (default) → 7497  (TWS paper)   or 4002 (IB Gateway paper)
#   IBKR_MODE=live            → 7496  (TWS live)     or 4001 (IB Gateway live)
# Override any default with IBKR_PORT env var.
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT_PAPER = 7497
_DEFAULT_PORT_LIVE = 7496
# Client IDs are randomised per-run to avoid "already in use" errors after crashes.
# TWS allows up to 32 simultaneous connections; we stay in the 10-29 range for
# data fetching and 90-99 for probes (ibkr_orders uses 43).
_DEFAULT_CLIENT_ID = None   # randomised on first use; see _next_client_id()


def _default_port() -> int:
    explicit = os.environ.get("IBKR_PORT")
    if explicit:
        return int(explicit)
    return _DEFAULT_PORT_LIVE if os.environ.get("IBKR_MODE", "paper").lower() == "live" else _DEFAULT_PORT_PAPER


# Keep as a module-level alias so existing code that imports _DEFAULT_PORT still works.
_DEFAULT_PORT = _DEFAULT_PORT_PAPER

_client_id_counter = 0


def _next_client_id(base: int = 10) -> int:
    """Return a fresh client ID in [base, base+19] cycling with each call."""
    global _client_id_counter
    _client_id_counter = (_client_id_counter + 1) % 20
    return base + _client_id_counter

# Seconds to pause between consecutive historical-data requests to stay well
# within the IBKR pacing limit (60 req / 10 min = 6 s, we use 2 s conservative).
_INTER_REQUEST_PAUSE = 2.0

# Maximum duration per single reqHistoricalData call (IBKR limit: ≤365 days
# for daily bars in a single request).
_MAX_CHUNK_DAYS = 360

# IBKR Paxos crypto history availability (AGGTRADES) starts ~Jan 2021.
# Requesting earlier returns empty; we clamp the start date per asset type.
_IBKR_START_FLOOR: dict[str, str] = {
    "BTCUSD":   "2021-01-01",   # Paxos AGGTRADES start
    "ETHUSDT":  "2021-01-01",
    "SOLUSDT":  "2025-03-01",   # Zero Hash SOL AGGTRADES observed from ~Mar 2025
    "DOGEUSDT": "2024-01-01",   # Zero Hash DOGE — try from 2024, fall back on empty
    "XAUUSD":   "2018-01-01",   # CMDTY @ SMART
    "XAGUSD":   "2018-01-01",
    "BNO":      "2010-01-01",   # ETF inception 2006; IBKR data from ~2010
}


# ── contract map ─────────────────────────────────────────────────────────────

def _ibkr_contract(symbol: str) -> Any:
    """
    Return the ib_async contract object for a live-book sleeve.

    Raises ImportError if ib_async is not installed.
    Raises ValueError for unknown symbols.
    """
    try:
        from ib_async import Commodity, Crypto, Stock
    except ImportError as exc:
        raise ImportError(
            "ib_async is required for IBKR data source. "
            "Run: pip install ib_async"
        ) from exc

    sym = symbol.upper()

    # Crypto via Paxos (BTC, ETH) or Zero Hash (SOL, DOGE, ETH, BTC also on ZH)
    # IBKR uses the Paxos exchange for BTC/ETH; Zero Hash for SOL/DOGE.
    # In practice, reqHistoricalData works for all four via 'PAXOS' exchange
    # because IBKR routes through whichever custodian they assigned — specifying
    # PAXOS as the exchange is the standard symbol for the Crypto secType.
    # BTC and ETH are custodied by Paxos; SOL and DOGE by Zero Hash.
    _paxos_map  = {"BTCUSD": "BTC", "ETHUSDT": "ETH"}
    _zeroh_map  = {"SOLUSDT": "SOL", "DOGEUSDT": "DOGE"}
    if sym in _paxos_map:
        return Crypto(_paxos_map[sym], "PAXOS", "USD")
    if sym in _zeroh_map:
        return Crypto(_zeroh_map[sym], "ZEROHASH", "USD")

    # Metals: API historical uses CMDTY @ SMART (IDEALPRO Forex often has 0 conId).
    if sym in {"XAUUSD", "XAGUSD"}:
        return Commodity(sym, "SMART", "USD")

    # BNO Brent Oil ETF on NYSE Arca
    if sym == "BNO":
        return Stock("BNO", "ARCA", "USD")

    raise ValueError(
        f"No IBKR contract mapping for symbol '{symbol}'. "
        "Add it to ibkr_data._ibkr_contract()."
    )


def _what_to_show(symbol: str) -> str:
    """IBKR whatToShow parameter per asset type.

    Paxos crypto requires AGGTRADES (error 10299 if you send TRADES).
    Forex metals use MIDPOINT. BNO (STK) uses TRADES.
    """
    sym = symbol.upper()
    if sym in {"XAUUSD", "XAGUSD"}:
        return "MIDPOINT"
    if sym in {"BTCUSD", "ETHUSDT", "SOLUSDT", "DOGEUSDT"}:
        return "AGGTRADES"   # Paxos crypto mandatory
    return "TRADES"          # STK (BNO)


def _use_rth(symbol: str) -> bool:
    """useRTH parameter: False for 24h assets, True for equity/ETF."""
    sym = symbol.upper()
    if sym in {"BTCUSD", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XAUUSD", "XAGUSD"}:
        return False
    return True  # ETF (BNO) uses regular trading hours only


# ── helpers ───────────────────────────────────────────────────────────────────

def ibkr_cache_path(symbol: str) -> Path:
    return IBKR_CACHE_DIR / f"{symbol.upper()}_ibkr_daily.csv"


def _load_cache(path: Path, start: str, include_current: bool) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
        df.index = pd.to_datetime(df.index, utc=True)
        df.columns = [str(c).strip().lower() for c in df.columns]
        df = df[~df.index.duplicated(keep="first")].sort_index()
        if not include_current:
            df = df.loc[df.index < pd.Timestamp.utcnow().normalize()]
        start_ts = pd.Timestamp(start, tz="UTC").normalize()
        return df.loc[df.index >= start_ts]
    except Exception:
        return pd.DataFrame()


def _cache_is_fresh(df: pd.DataFrame, start: str, include_current: bool) -> bool:
    if df.empty:
        return False
    start_ts = pd.Timestamp(start, tz="UTC").normalize()
    today = pd.Timestamp.utcnow().normalize()
    needed_end = today if include_current else today - pd.Timedelta(days=1)
    return df.index.min() <= start_ts and df.index.max() >= needed_end


def _bars_to_df(bars: list[Any]) -> pd.DataFrame:
    """Convert ib_async BarData list to a normalised OHLC DataFrame."""
    if not bars:
        return pd.DataFrame()
    rows = []
    for b in bars:
        rows.append({
            "date": pd.Timestamp(b.date, tz="UTC") if hasattr(b.date, "tzinfo") else pd.Timestamp(str(b.date), tz="UTC"),
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": float(getattr(b, "volume", 0) or 0),
        })
    df = pd.DataFrame(rows).set_index("date").sort_index()
    df = df[~df.index.duplicated(keep="first")]
    # Normalise to UTC midnight
    df.index = df.index.normalize()
    return df.dropna(subset=["open", "high", "low", "close"])


# ── main fetch ────────────────────────────────────────────────────────────────

def download_ibkr_daily(
    symbol: str,
    start: str,
    end: str | None = None,
    *,
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
) -> pd.DataFrame:
    """Connect to TWS/Gateway (port auto-detected from IBKR_MODE) and pull daily bars.

    Breaks the request into ≤360-day chunks to respect IBKR pacing.
    Pauses _INTER_REQUEST_PAUSE seconds between chunks.
    Clamps start to _IBKR_START_FLOOR per asset (Paxos history is limited).
    Raises RuntimeError if connection fails or no data is returned.
    """
    try:
        from ib_async import IB, util
    except ImportError as exc:
        raise ImportError("Install ib_async: pip install ib_async") from exc

    host = host or os.environ.get("IBKR_HOST", _DEFAULT_HOST)
    port = port or _default_port()
    explicit_cid = os.environ.get("IBKR_CLIENT_ID")
    client_id = client_id or (int(explicit_cid) if explicit_cid else _next_client_id(10))

    contract = _ibkr_contract(symbol)
    wts = _what_to_show(symbol)
    rth = _use_rth(symbol)

    # Clamp start to the earliest date IBKR actually has data for this asset.
    sym = symbol.upper()
    floor_str = _IBKR_START_FLOOR.get(sym, "2018-01-01")
    floor_ts = pd.Timestamp(floor_str, tz="UTC").normalize()
    start_ts = max(pd.Timestamp(start, tz="UTC").normalize(), floor_ts)
    end_ts = pd.Timestamp(end, tz="UTC").normalize() if end else pd.Timestamp.utcnow().normalize()

    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, timeout=10, readonly=True)
    except Exception as exc:
        raise RuntimeError(
            f"Cannot connect to IBKR TWS/Gateway at {host}:{port}. "
            f"Is TWS/Gateway running? ({exc})"
        ) from exc

    # Silence ib_async's per-request error logs (162 = no data for chunk,
    # 10299 = wrong whatToShow hint, 200 = no contract on this account).
    # These are expected for chunks outside the available history window.
    import logging as _logging
    _suppress = ["ib_async", "ib_async.wrapper", "ib_async.client",
                 "ib_async.ib", "ib_async.util"]
    _saved_levels = {}
    for _name in _suppress:
        _lg = _logging.getLogger(_name)
        _saved_levels[_name] = _lg.level
        _lg.setLevel(_logging.CRITICAL)

    all_bars: list[Any] = []
    chunk_start = start_ts
    try:
        while chunk_start < end_ts:
            chunk_end = min(chunk_start + pd.Timedelta(days=_MAX_CHUNK_DAYS), end_ts)
            end_str = chunk_end.strftime("%Y%m%d %H:%M:%S")
            delta_days = max(1, (chunk_end - chunk_start).days)
            duration = f"{delta_days} D"

            try:
                bars = ib.reqHistoricalData(
                    contract,
                    endDateTime=end_str,
                    durationStr=duration,
                    barSizeSetting="1 day",
                    whatToShow=wts,
                    useRTH=rth,
                    formatDate=2,   # UTC datetime objects
                    timeout=60,
                )
                all_bars.extend(bars)
            except Exception as exc:
                logger.debug("IBKR chunk skipped for %s [%s→%s]: %s",
                             symbol, chunk_start.date(), chunk_end.date(), exc)

            chunk_start = chunk_end
            if chunk_start < end_ts:
                time.sleep(_INTER_REQUEST_PAUSE)
    finally:
        for _name, _lvl in _saved_levels.items():
            _logging.getLogger(_name).setLevel(_lvl)
        ib.disconnect()

    if not all_bars:
        raise RuntimeError(f"IBKR returned no daily bars for {symbol} ({start} → {end or 'today'})")

    df = _bars_to_df(all_bars)
    # Trim to requested window
    df = df.loc[(df.index >= start_ts) & (df.index <= end_ts)]
    if df.empty:
        raise RuntimeError(f"IBKR data for {symbol} is empty after trimming to [{start}, {end or 'today'}]")
    return df


def fetch_ibkr_daily(
    symbol: str,
    start: str,
    end: str | None = None,
    include_current: bool = False,
    *,
    refresh_cache: bool = False,
    host: str | None = None,
    port: int | None = None,
    client_id: int | None = None,
) -> pd.DataFrame:
    """
    Return daily OHLC for `symbol` sourced from IBKR TWS/Gateway.

    On first call (or refresh_cache=True), connects to TWS and downloads the
    full history from `start`. Subsequent calls serve from cache unless stale.

    Falls back to cached data if TWS is unreachable and cache exists.
    Raises RuntimeError if TWS is unreachable AND no cache exists.
    """
    cache = ibkr_cache_path(symbol)
    IBKR_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Clamp the requested start to the earliest date IBKR has for this symbol.
    # A cache built from 2021 is fully "fresh" for a request starting 2018 if
    # IBKR simply has no data before 2021.
    floor_str = _IBKR_START_FLOOR.get(symbol.upper(), "2018-01-01")
    effective_start = max(
        pd.Timestamp(start, tz="UTC").normalize(),
        pd.Timestamp(floor_str, tz="UTC").normalize(),
    ).strftime("%Y-%m-%d")

    cached = _load_cache(cache, effective_start, include_current)

    if not refresh_cache and _cache_is_fresh(cached, effective_start, include_current):
        logger.debug("IBKR cache hit for %s", symbol)
        return cached

    try:
        df = download_ibkr_daily(symbol, effective_start, end, host=host, port=port, client_id=client_id)
    except Exception as exc:
        if not cached.empty:
            logger.warning(
                "IBKR download failed for %s (%s). Serving from cache (%d rows).",
                symbol, exc, len(cached),
            )
            return cached
        raise

    # Merge with any older cached data so we don't lose history on partial pulls
    if not cached.empty:
        df = (
            pd.concat([cached, df])
            .sort_index()
            .loc[lambda d: ~d.index.duplicated(keep="last")]
        )

    if not include_current:
        df = df.loc[df.index < pd.Timestamp.utcnow().normalize()]
    if end:
        df = df.loc[df.index < pd.Timestamp(end, tz="UTC").normalize()]

    df.to_csv(cache)
    logger.info("IBKR cache updated for %s: %d rows → %s", symbol, len(df), cache)
    return df.loc[df.index >= pd.Timestamp(start, tz="UTC").normalize()]


# ── convenience ───────────────────────────────────────────────────────────────

_ibkr_available_cache: dict[str, bool] = {}


def ibkr_available(host: str | None = None, port: int | None = None) -> bool:
    """
    Lightweight check: can we reach TWS/Gateway right now?
    Result is cached per (host, port) for the lifetime of the process so we
    probe at most once per run regardless of how many sleeves are fetched.
    Suppresses ib_async's own stderr noise on connection failure.
    """
    try:
        from ib_async import IB
    except ImportError:
        return False

    host = host or os.environ.get("IBKR_HOST", _DEFAULT_HOST)
    port = port or _default_port()
    key = f"{host}:{port}"

    if key in _ibkr_available_cache:
        return _ibkr_available_cache[key]

    import io
    import sys

    ib = IB()
    # Suppress ib_async's "API connection failed" stderr message on probe failure.
    _stderr, sys.stderr = sys.stderr, io.StringIO()
    try:
        ib.connect(host, port, clientId=_next_client_id(90), timeout=5, readonly=True)
        ib.disconnect()
        result = True
    except Exception:
        result = False
    finally:
        sys.stderr = _stderr

    _ibkr_available_cache[key] = result
    if not result:
        logger.debug("IBKR not available at %s — will use fallback sources.", key)
    return result


if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    p = argparse.ArgumentParser(description="Download IBKR daily bars to cache")
    p.add_argument("symbols", nargs="+", help="Sleeve symbols, e.g. BTCUSD XAUUSD BNO")
    p.add_argument("--start", default="2018-01-01")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--client-id", type=int, default=None)
    p.add_argument("--refresh", action="store_true")
    args = p.parse_args()

    for sym in args.symbols:
        print(f"\n--- {sym} ---")
        try:
            df = fetch_ibkr_daily(
                sym, args.start,
                refresh_cache=args.refresh,
                host=args.host,
                port=args.port,
                client_id=args.client_id,
            )
            print(f"  rows: {len(df)}  range: {df.index[0].date()} → {df.index[-1].date()}")
            print(df.tail(3)[["open", "high", "low", "close"]].to_string())
        except Exception as e:
            print(f"  ERROR: {e}")
