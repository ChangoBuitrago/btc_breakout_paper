#!/usr/bin/env python3
"""
World Cup / memecoin candidate screen (research only; live unchanged).

Symbols: PEPE, SHIB, WIF, BONK, CHZ, FLOKI (+ DOGE reference).
Modes: DOGE-like params (sma200_95) vs regime-off (trend_mode=all).
Windows: full, WC2018, WC2022, WC2026 run-up.
Slippage: +10 bps/side stress on solo replay.

Run: python3 btc_breakout_clean/world_cup_candidate_validation.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import (  # noqa: E402
    LIVE_MAX_CONCURRENT_ENTRIES,
    LIVE_SLEEVE_EQUITY,
    LIVE_SYMBOLS,
    fetch_binance_daily,
    live_strategy_config,
)
from strategy_validation import (  # noqa: E402
    DATA_START,
    portfolio_metrics,
    preload_raw,
    run_full_book_live,
    run_sleeve,
    trades_in_window,
)

OUT_PATH = HERE / "world_cup_candidate_validation_results.json"

CANDIDATES = ("PEPEUSDT", "SHIBUSDT", "WIFUSDT", "BONKUSDT", "CHZUSDT", "FLOKIUSDT")
REFERENCE = "DOGEUSDT"

EVENT_WINDOWS: list[tuple[str, str | None, str | None]] = [
    ("full", None, None),
    ("wc2018_runup", "2018-04-01", "2018-08-31"),
    ("wc2022_runup", "2022-09-01", "2023-02-28"),
    ("wc2026_runup", "2026-01-01", None),
]

MODES: dict[str, dict[str, Any]] = {
    "doge_like": {
        "note": "Clone DOGE live params (sma200_95, 12% stop, 225 cap)",
        "kw": {},
        "from_doge": True,
    },
    "regime_off": {
        "note": "Same as DOGE-like but trend_mode=all (no SMA gate)",
        "kw": {"trend_mode": "all"},
        "from_doge": True,
    },
    "chz_fan": {
        "note": "CHZ: shorter hold, 10% stop, sma200_90",
        "kw": {
            "lookback": 20,
            "buffer_bps": 100.0,
            "hold_min": 5,
            "hold_max": 12,
            "hold_days": 5,
            "stop_loss_pct": 0.10,
            "trend_mode": "sma200_90",
        },
        "from_doge": False,
    },
}


def meme_config(symbol: str, mode: str) -> Any:
    meta = MODES[mode]
    if meta.get("from_doge"):
        base = replace(live_strategy_config(REFERENCE), **{"compound": True})
        if symbol != REFERENCE:
            base = replace(base, fee_bps=10.0)
    else:
        base = live_strategy_config("BNBUSDT")
        base = replace(
            base,
            lookback=20,
            buffer_bps=100.0,
            max_breakout_bps=225.0,
            trend_mode="sma200_90",
            hold_min=5,
            hold_max=12,
            hold_days=5,
            dynamic_hold=True,
            stop_loss_pct=0.10,
            fee_bps=10.0,
            compound=True,
        )
    return replace(base, **meta.get("kw", {}))


def fetch_symbol(symbol: str) -> pd.DataFrame | None:
    try:
        df = fetch_binance_daily(symbol, DATA_START, None)
        if len(df) < 200:
            return None
        return df
    except Exception as exc:
        print(f"  skip {symbol}: {exc}", flush=True)
        return None


def window_summary(
    trades: pd.DataFrame,
    curve: pd.DataFrame,
    equity: float,
    start: str | None,
    end: str | None,
) -> dict[str, Any]:
    s = pd.Timestamp(start, tz="UTC") if start else None
    e = pd.Timestamp(end, tz="UTC") if end else None
    wtr = trades_in_window(trades, s, e)
    pnls = pd.to_numeric(wtr["net_pnl"], errors="coerce") if not wtr.empty else pd.Series(dtype=float)
    cu = curve.copy()
    cu["date"] = pd.to_datetime(cu["date"], utc=True)
    cu = cu.set_index("date").sort_index()
    if s is not None:
        cu = cu.loc[cu.index >= s]
    if e is not None:
        cu = cu.loc[cu.index < e]
    if cu.empty or len(cu) < 2:
        return {"trades": int(len(pnls)), "return_pct": 0.0, "max_dd_pct": float("nan")}
    eq = cu["equity"].astype(float)
    ret = float(eq.iloc[-1] / eq.iloc[0] - 1.0) if float(eq.iloc[0]) > 0 else 0.0
    dd = float((eq / eq.cummax() - 1.0).min())
    wins = int((pnls > 0).sum()) if len(pnls) else 0
    pf = (
        float(pnls[pnls > 0].sum() / abs(pnls[pnls <= 0].sum()))
        if len(pnls) and (pnls <= 0).any() and (pnls > 0).any()
        else float("nan")
    )
    return {
        "trades": int(len(pnls)),
        "return_pct": 100.0 * ret,
        "max_dd_pct": 100.0 * dd,
        "profit_factor": pf,
        "win_rate_pct": 100.0 * wins / len(pnls) if len(pnls) else float("nan"),
        "net_pnl": float(pnls.sum()) if len(pnls) else 0.0,
    }


def run_solo(
    raw: pd.DataFrame,
    symbol: str,
    cfg: Any,
    *,
    slippage_bps: float = 0.0,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    strat = cfg
    if slippage_bps > 0:
        strat = replace(cfg, fee_bps=cfg.fee_bps + slippage_bps)
    tr, cu, summary = run_sleeve(raw, symbol, strat, LIVE_SLEEVE_EQUITY)
    return tr, cu, summary


def run_add_to_book(
    raw_ext: dict[str, pd.DataFrame],
    new_sym: str,
    cfg: Any,
) -> dict[str, Any]:
    syms = tuple(LIVE_SYMBOLS) + (new_sym,)
    strats = {s: live_strategy_config(s) for s in LIVE_SYMBOLS}
    strats[new_sym] = cfg
    eq = {s: LIVE_SLEEVE_EQUITY for s in syms}
    n = len(syms)
    eq = {s: 100_000.0 / n for s in syms}
    curves, _, trades, _ = run_full_book_live(
        raw_ext, syms, strats, max_concurrent=LIVE_MAX_CONCURRENT_ENTRIES, equities_by_symbol=eq
    )
    m = portfolio_metrics(curves, trades, sum(eq.values()), initial_equity_by_sleeve=eq)
    return {"metrics": m, "trades": int(len(trades))}


def main() -> None:
    print("World Cup / memecoin candidate screen", flush=True)
    raw_cache: dict[str, pd.DataFrame] = {}
    listing: dict[str, str] = {}

    for sym in (REFERENCE, *CANDIDATES):
        df = fetch_symbol(sym)
        if df is not None:
            raw_cache[sym] = df
            listing[sym] = str(df.index.min())[:10]
            print(f"  loaded {sym} from {listing[sym]} ({len(df)} bars)", flush=True)

    solo_rows: list[dict[str, Any]] = []
    for sym in CANDIDATES:
        if sym not in raw_cache:
            continue
        modes_to_run = ("doge_like", "regime_off") if sym != "CHZUSDT" else ("doge_like", "regime_off", "chz_fan")
        for mode in modes_to_run:
            cfg = meme_config(sym, mode if sym != "CHZUSDT" or mode == "chz_fan" else mode)
            if sym == "CHZUSDT" and mode in ("doge_like", "regime_off"):
                cfg = meme_config(sym, mode)
            elif sym != "CHZUSDT" and mode == "chz_fan":
                continue
            tr, cu, summary = run_solo(raw_cache[sym], sym, cfg)
            row: dict[str, Any] = {
                "symbol": sym,
                "mode": mode,
                "note": MODES[mode]["note"],
                "listing_start": listing.get(sym),
                "full_sample": summary,
                "windows": {},
            }
            for wname, ws, we in EVENT_WINDOWS:
                row["windows"][wname] = window_summary(tr, cu, LIVE_SLEEVE_EQUITY, ws, we)
            tr_slip, cu_slip, sum_slip = run_solo(raw_cache[sym], sym, cfg, slippage_bps=10.0)
            row["slippage_plus_10bps"] = sum_slip
            solo_rows.append(row)
            fs = summary
            w26 = row["windows"].get("wc2026_runup", {})
            print(
                f"  {sym:10} {mode:12} full ret={fs.get('return_pct', 0):5.1f}% "
                f"DD={fs.get('max_drawdown_pct', 0):5.1f}% PF={fs.get('profit_factor', 0):.2f} "
                f"tr={fs.get('trades', 0)} | wc26 tr={w26.get('trades', 0)} "
                f"ret={w26.get('return_pct', 0):.1f}%",
                flush=True,
            )

    # DOGE reference same modes
    if REFERENCE in raw_cache:
        for mode in ("doge_like", "regime_off"):
            cfg = meme_config(REFERENCE, mode)
            tr, cu, summary = run_solo(raw_cache[REFERENCE], REFERENCE, cfg)
            solo_rows.append(
                {
                    "symbol": REFERENCE,
                    "mode": mode,
                    "note": "live reference",
                    "listing_start": listing.get(REFERENCE),
                    "full_sample": summary,
                    "windows": {
                        wname: window_summary(tr, cu, LIVE_SLEEVE_EQUITY, ws, we)
                        for wname, ws, we in EVENT_WINDOWS
                    },
                }
            )

    # Best solo candidates → 9-sleeve add test (doge_like only)
    book_rows: list[dict[str, Any]] = []
    raw_book = preload_raw(tuple(LIVE_SYMBOLS))
    raw_book.update({k: v for k, v in raw_cache.items() if k in CANDIDATES})

    ranked = sorted(
        [r for r in solo_rows if r["symbol"] in CANDIDATES and r["mode"] == "doge_like"],
        key=lambda x: (
            x["full_sample"].get("profit_factor") or 0,
            x["full_sample"].get("return_pct") or 0,
        ),
        reverse=True,
    )
    for r in ranked[:3]:
        sym = r["symbol"]
        cfg = meme_config(sym, "doge_like")
        book = run_add_to_book(raw_book, sym, cfg)
        book_rows.append({"symbol": sym, "mode": "doge_like", **book})
        m = book["metrics"]
        print(
            f"  9-sleeve +{sym} ret={m['return_pct']:.1f}% DD={m['max_drawdown_pct']:.2f}% "
            f"worst={m['worst_sleeve_max_drawdown_pct']:.1f}%",
            flush=True,
        )

    baseline_8 = preload_raw(tuple(LIVE_SYMBOLS))
    strats8 = {s: live_strategy_config(s) for s in LIVE_SYMBOLS}
    eq8 = {s: LIVE_SLEEVE_EQUITY for s in LIVE_SYMBOLS}
    _, _, tr8, _ = run_full_book_live(baseline_8, tuple(LIVE_SYMBOLS), strats8)
    _, _, cu8, _ = run_full_book_live(baseline_8, tuple(LIVE_SYMBOLS), strats8)
    # baseline metrics from solo book run
    curves8, _, trades8, _ = run_full_book_live(
        baseline_8, tuple(LIVE_SYMBOLS), strats8, equities_by_symbol=eq8
    )
    bm = portfolio_metrics(curves8, trades8, sum(eq8.values()), initial_equity_by_sleeve=eq8)

    best_solo = ranked[0] if ranked else None
    payload = {
        "candidates": list(CANDIDATES),
        "reference": REFERENCE,
        "listing_dates": listing,
        "event_windows": [{"name": n, "start": s, "end": e} for n, s, e in EVENT_WINDOWS],
        "baseline_8_metrics": bm,
        "solo_results": solo_rows,
        "nine_sleeve_add": book_rows,
        "recommendations": {
            "nft": "Not applicable — no daily OHLC sleeve",
            "best_solo_doge_like": best_solo["symbol"] if best_solo else None,
            "regime_off_helps": [
                r["symbol"]
                for r in solo_rows
                if r["mode"] == "regime_off"
                and r.get("full_sample", {}).get("trades", 0)
                > next(
                    (x["full_sample"].get("trades", 0) for x in solo_rows if x["symbol"] == r["symbol"] and x["mode"] == "doge_like"),
                    0,
                )
            ],
            "satellite_paper": "Only if wc2026_runup window shows trades + positive PF solo; keep off live 8-sleeve",
            "live_unchanged": True,
        },
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
