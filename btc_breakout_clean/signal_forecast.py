#!/usr/bin/env python3
"""Historical entry timing and setup-quality estimates for the breakout algo."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from btc_breakout_binance_paper_bot import (
    fetch_binance_daily,
    live_binance_symbol,
    live_symbol_fallback_source,
    live_symbol_source,
    live_yfinance_ticker,
)
from btc_breakout_paper_sim import (
    StrategyConfig,
    add_indicators,
    dukascopy_cache_path,
    fetch_dukascopy_instrument,
    fetch_yfinance_daily,
    normalize_ohlc,
    yfinance_cache_path,
)

HERE = Path(__file__).resolve().parent


def binance_cache_path(symbol: str) -> Path:
    return HERE / "cache" / f"{symbol.upper()}_binance_1d.csv"


def _load_binance(sym: str, start: str, start_ts: pd.Timestamp, refresh_cache: bool) -> pd.DataFrame:
    pair = live_binance_symbol(sym)
    path = binance_cache_path(pair)
    cached = pd.DataFrame()
    if path.exists():
        cached = normalize_ohlc(pd.read_csv(path, index_col=0, parse_dates=True))
    if not refresh_cache and not cached.empty:
        return cached.loc[cached.index >= start_ts]
    try:
        df = fetch_binance_daily(pair, start, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(path)
        return df
    except Exception:
        if cached.empty:
            raise
        return cached.loc[cached.index >= start_ts]


def _load_yfinance(sym: str, start: str, start_ts: pd.Timestamp, refresh_cache: bool) -> pd.DataFrame:
    ticker = live_yfinance_ticker(sym)
    path = yfinance_cache_path(ticker)
    cached = pd.DataFrame()
    if path.exists():
        cached = normalize_ohlc(pd.read_csv(path, index_col=0, parse_dates=True))
    if not refresh_cache and not cached.empty:
        return cached.loc[cached.index >= start_ts]
    try:
        df = fetch_yfinance_daily(ticker, start, None, False, path, refresh_cache)
        return df.loc[df.index >= start_ts]
    except Exception:
        if cached.empty:
            raise
        return cached.loc[cached.index >= start_ts]


def load_daily_bars(symbol: str, start: str = "2018-01-01", *, refresh_cache: bool = False) -> pd.DataFrame:
    sym = symbol.upper()
    source = live_symbol_source(sym)
    start_ts = pd.Timestamp(start, tz="UTC").normalize()

    if source == "ibkr":
        from ibkr_data import fetch_ibkr_daily, ibkr_available
        if ibkr_available():
            try:
                return fetch_ibkr_daily(sym, start, include_current=False, refresh_cache=refresh_cache)
            except Exception:
                pass
        # Fall through to proxy source
        fallback = live_symbol_fallback_source(sym)
        if fallback == "binance":
            return _load_binance(sym, start, start_ts, refresh_cache)
        if fallback == "yfinance":
            return _load_yfinance(sym, start, start_ts, refresh_cache)
        # dukascopy fallback handled below via source = "dukascopy" path
        source = fallback

    if source == "binance":
        return _load_binance(sym, start, start_ts, refresh_cache)

    if source == "yfinance":
        return _load_yfinance(sym, start, start_ts, refresh_cache)

    return fetch_dukascopy_instrument(
        sym,
        dukascopy_cache_path(sym),
        start,
        None,
        include_current=False,
        refresh_cache=refresh_cache,
    )


def _regime_margin_bps(row: pd.Series, trend_mode: str) -> float | None:
    close = float(row["close"])
    sma200 = float(row.get("sma200", np.nan))
    if not np.isfinite(sma200) or sma200 <= 0:
        return None
    if trend_mode == "sma200_95":
        floor = sma200 * 0.95
    elif trend_mode == "sma200_90":
        floor = sma200 * 0.90
    elif trend_mode in {"bull_only", "sma200"}:
        floor = sma200
    else:
        return None
    return 10_000.0 * (close / floor - 1.0)


def _days_until_signal(df: pd.DataFrame, i: int, *, max_days: int = 90) -> int | None:
    n = len(df)
    for j in range(1, min(max_days, n - i)):
        if bool(df["signal"].iloc[i + j]):
            return j
    return None


def flat_signal_horizon_stats(
    df: pd.DataFrame,
    strat_cfg: StrategyConfig,
    *,
    gap_bps: float,
    band_bps: float = 40.0,
    max_forward: int = 90,
    min_samples: int = 8,
) -> dict[str, Any]:
    """From similar historical flat setups, days until the next signal bar."""
    buffer_bps = float(strat_cfg.buffer_bps)
    regime = df["regime_on"].astype(bool)
    bps = pd.to_numeric(df["breakout_bps"], errors="coerce")
    signal = df["signal"].astype(bool)
    warmup = max(strat_cfg.lookback, 200) + 5
    horizons: list[int] = []

    for i in range(warmup, len(df) - 1):
        if not bool(regime.iloc[i]) or bool(signal.iloc[i]):
            continue
        dist = buffer_bps - float(bps.iloc[i])
        if not np.isfinite(dist) or dist < 0:
            continue
        if abs(dist - gap_bps) > band_bps:
            continue
        h = _days_until_signal(df, i, max_days=max_forward)
        if h is not None:
            horizons.append(h)

    if len(horizons) < min_samples:
        band_bps = min(80.0, band_bps * 2)
        horizons = []
        for i in range(warmup, len(df) - 1):
            if not bool(regime.iloc[i]) or bool(signal.iloc[i]):
                continue
            dist = buffer_bps - float(bps.iloc[i])
            if not np.isfinite(dist) or dist < 0:
                continue
            if abs(dist - gap_bps) > band_bps:
                continue
            h = _days_until_signal(df, i, max_days=max_forward)
            if h is not None:
                horizons.append(h)

    if not horizons:
        return {"n": 0, "median_days": None, "prob_7d": None, "prob_14d": None}

    arr = np.array(horizons, dtype=float)
    return {
        "n": int(len(arr)),
        "median_days": int(np.median(arr)),
        "prob_7d": float((arr <= 7).mean()),
        "prob_14d": float((arr <= 14).mean()),
    }


def _analogue_gap_bps(gap: float | None, buffer_bps: float) -> tuple[float, float]:
    """Match near-breakout history when price is far below prior highs."""
    if gap is None:
        return 50.0, 40.0
    near_cap = buffer_bps + 50.0
    if gap > buffer_bps * 2:
        return near_cap, max(40.0, buffer_bps * 0.6)
    return gap, max(40.0, min(80.0, gap * 0.35))


def median_inter_entry_gap_days(trades: pd.DataFrame) -> float | None:
    if trades is None or trades.empty or len(trades) < 2:
        return None
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    gaps = t["entry_date"].sort_values().diff().dt.days.dropna()
    if gaps.empty:
        return None
    return float(gaps.median())


def days_since_last_exit(trades: pd.DataFrame, as_of: pd.Timestamp) -> int | None:
    if trades is None or trades.empty:
        return None
    t = trades.copy()
    t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
    last_exit = t["exit_date"].max()
    if pd.isna(last_exit):
        return None
    return int((as_of.normalize() - last_exit.normalize()).days)


def historical_setup_quality(
    trades: pd.DataFrame,
    *,
    target_breakout_bps: float,
    band_bps: float = 75.0,
    min_n: int = 3,
) -> dict[str, Any]:
    if trades is None or trades.empty:
        return {
            "n": 0,
            "win_pct": None,
            "med_open_to_exit_pct": None,
            "med_net_ret_pct": None,
            "quality_score": None,
            "quality_tier": "n/a",
            "low_sample": True,
        }

    t = trades.copy()
    t["breakout_bps"] = pd.to_numeric(t["breakout_bps"], errors="coerce")
    t["open_to_exit_pct"] = pd.to_numeric(t["open_to_exit_pct"], errors="coerce")
    t["net_ret_on_equity"] = pd.to_numeric(t.get("net_ret_on_equity"), errors="coerce")
    t["net_pnl"] = pd.to_numeric(t["net_pnl"], errors="coerce")

    similar = t.loc[(t["breakout_bps"] - target_breakout_bps).abs() <= band_bps]
    low_sample = len(similar) < min_n
    if low_sample:
        similar = t

    wins = similar["net_pnl"] > 0
    win_pct = 100.0 * float(wins.mean()) if len(similar) else None
    med_o2e = float(similar["open_to_exit_pct"].median()) if len(similar) else None
    if "net_ret_on_equity" in similar.columns and similar["net_ret_on_equity"].notna().any():
        med_net = 100.0 * float(similar["net_ret_on_equity"].median())
    else:
        med_net = None

    all_win = 100.0 * float((t["net_pnl"] > 0).mean())
    all_o2e = float(t["open_to_exit_pct"].median())

    score = None
    tier = "n/a"
    if win_pct is not None and med_o2e is not None:
        win_edge = (win_pct - all_win) / 50.0
        ret_edge = (med_o2e - all_o2e) / max(abs(all_o2e), 1.0)
        raw = 50.0 + 22.0 * win_edge + 28.0 * ret_edge
        score = int(max(0, min(100, round(raw))))
        if low_sample or len(similar) < min_n:
            tier = "low sample"
        elif score >= 65:
            tier = "strong"
        elif score >= 45:
            tier = "average"
        else:
            tier = "weak"

    return {
        "n": int(len(similar)),
        "win_pct": win_pct,
        "med_open_to_exit_pct": med_o2e,
        "med_net_ret_pct": med_net,
        "quality_score": score,
        "quality_tier": tier,
        "low_sample": low_sample,
        "target_breakout_bps": target_breakout_bps,
    }


def forecast_entry(
    *,
    latest: dict[str, Any],
    strat_cfg: StrategyConfig,
    trades: pd.DataFrame,
    df: pd.DataFrame,
    open_position: dict[str, Any] | None = None,
    pending_entry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Estimate next long entry timing and historical quality for similar setups."""
    buffer_bps = float(strat_cfg.buffer_bps)
    max_bps = float(strat_cfg.max_breakout_bps) if strat_cfg.max_breakout_bps is not None else None
    breakout_bps = latest.get("breakout_bps")
    breakout_bps_f = float(breakout_bps) if breakout_bps is not None else None
    regime_on = bool(latest.get("regime_on", latest.get("bull", False)))
    as_of = pd.Timestamp(latest["signal_date"], tz="UTC")
    blockers: list[str] = []

    gap = None
    if breakout_bps_f is not None and breakout_bps_f < buffer_bps:
        gap = buffer_bps - breakout_bps_f

    med_gap = median_inter_entry_gap_days(trades)
    since_exit = days_since_last_exit(trades, as_of)
    regime_margin = _regime_margin_bps(df.iloc[-1], strat_cfg.trend_mode) if len(df) else None

    def _base() -> dict[str, Any]:
        return {
            "gap_bps": gap,
            "breakout_bps": breakout_bps_f,
            "regime_on": regime_on,
            "regime_margin_bps": regime_margin,
            "buffer_bps": buffer_bps,
        }

    # --- in position: next IN is after exit ---
    if open_position:
        hold_left = max(0, int(open_position["hold_max"]) - int(open_position["hold_day"]))
        q = historical_setup_quality(
            trades,
            target_breakout_bps=breakout_bps_f if breakout_bps_f is not None else buffer_bps,
        )
        return {
            "state": "in_position",
            "next_in_label": f"after exit (~{hold_left}d hold left)",
            "next_in_days": hold_left + 1,
            "prob_7d": None,
            "prob_14d": None,
            "days_since_exit": since_exit,
            "median_gap_days": med_gap,
            "blockers": blockers,
            **{k: v for k, v in q.items() if k not in {"target_breakout_bps"}},
            "setup_bps": breakout_bps_f,
            **_base(),
        }

    # --- signal pending / fired ---
    if pending_entry or bool(latest.get("signal")):
        if float(latest.get("next_size_frac") or 0.0) <= 0.0 and not pending_entry:
            blockers.append("size 0")
        if max_bps is not None and breakout_bps_f is not None and breakout_bps_f > max_bps:
            blockers.append(f"stretched {breakout_bps_f:.0f}bps")
        target = breakout_bps_f if breakout_bps_f is not None else buffer_bps
        q = historical_setup_quality(trades, target_breakout_bps=target)
        label = "next open (~1d)" if not blockers else f"blocked ({', '.join(blockers)})"
        return {
            "state": "imminent",
            "next_in_label": label,
            "next_in_days": 1,
            "prob_7d": 1.0 if not blockers else 0.0,
            "prob_14d": 1.0 if not blockers else 0.0,
            "days_since_exit": since_exit,
            "median_gap_days": med_gap,
            "blockers": blockers,
            **{k: v for k, v in q.items() if k not in {"target_breakout_bps"}},
            "setup_bps": target,
            **_base(),
        }

    # --- flat ---
    if not regime_on:
        last = df.iloc[-1]
        margin = _regime_margin_bps(last, strat_cfg.trend_mode)
        if margin is not None and margin < 0:
            blockers.append(f"regime {margin:+.0f}bps")
        else:
            blockers.append("regime off")

    if max_bps is not None and breakout_bps_f is not None and breakout_bps_f > max_bps:
        blockers.append(f"stretched {breakout_bps_f:.0f}bps")

    target = buffer_bps
    if breakout_bps_f is not None and breakout_bps_f >= buffer_bps:
        target = breakout_bps_f
    elif gap is not None:
        target = buffer_bps

    q = historical_setup_quality(trades, target_breakout_bps=target)

    if blockers and any("regime" in b for b in blockers):
        label = blockers[0]
        if gap is not None:
            label += f" · +{gap:.0f}bps to breakout"
        return {
            "state": "flat",
            "next_in_label": label,
            "next_in_days": None,
            "prob_7d": None,
            "prob_14d": None,
            "days_since_exit": since_exit,
            "median_gap_days": med_gap,
            "blockers": blockers,
            **{k: v for k, v in q.items() if k not in {"target_breakout_bps"}},
            "setup_bps": target,
            **_base(),
        }

    match_gap, match_band = _analogue_gap_bps(gap, buffer_bps)
    far_from_breakout = gap is not None and gap > buffer_bps * 2
    horizon = flat_signal_horizon_stats(
        df, strat_cfg, gap_bps=match_gap, band_bps=match_band,
    )

    parts: list[str] = []
    if gap is not None and gap > 0:
        parts.append(f"+{gap:.0f}bps to signal")
    if horizon["median_days"] is not None:
        p7 = horizon["prob_7d"]
        p7s = f"{100*p7:.0f}%" if p7 is not None else "—"
        tag = "once near breakout" if far_from_breakout else "med"
        parts.append(f"~{horizon['median_days']}d {tag} ({p7s} in 7d, n={horizon['n']})")
    elif med_gap is not None:
        parts.append(f"~{med_gap:.0f}d between entries (hist)")
    elif far_from_breakout:
        parts.append("far below highs — timing from near-breakout analogues")
    if since_exit is not None and med_gap is not None:
        parts.append(f"{since_exit}d since exit")

    return {
        "state": "flat",
        "next_in_label": " · ".join(parts) if parts else "no historical analogues",
        "next_in_days": horizon.get("median_days"),
        "prob_7d": horizon.get("prob_7d"),
        "prob_14d": horizon.get("prob_14d"),
        "days_since_exit": since_exit,
        "median_gap_days": med_gap,
        "blockers": blockers,
        **{k: v for k, v in q.items() if k not in {"target_breakout_bps"}},
        "setup_bps": target,
        "horizon_n": horizon.get("n"),
        "far_from_breakout": far_from_breakout,
        **_base(),
    }


def forecast_display(fc: dict[str, Any]) -> dict[str, Any]:
    """Short labels for dashboard tables (readable at a glance)."""
    state = str(fc.get("state") or "flat")
    blockers = fc.get("blockers") or []

    if state == "imminent":
        status = "ENTER" if not blockers else "BLOCKED"
        timing = "Next open" if not blockers else "Blocked"
    elif state == "in_position":
        status = "IN TRADE"
        d = fc.get("next_in_days")
        timing = f"~{d}d after exit" if d is not None else "After exit"
    elif blockers:
        status = "BLOCKED"
        timing = "—"
    else:
        status = "WATCH"
        d = fc.get("next_in_days")
        timing = f"~{d}d median" if d is not None else "—"

    gap = fc.get("gap_bps")
    gap_label = f"+{gap:.0f}" if gap is not None and gap > 0 else ("OK" if gap is not None and gap <= 0 else "—")

    p7 = fc.get("prob_7d")
    p14 = fc.get("prob_14d")
    p7_pct = round(100 * float(p7)) if p7 is not None else None
    p14_pct = round(100 * float(p14)) if p14 is not None else None

    tier = str(fc.get("quality_tier") or "n/a")
    score = fc.get("quality_score")
    quality_label = f"{score}" if score is not None else "—"

    return {
        "status": status,
        "timing": timing,
        "gap_bps_label": gap_label,
        "p7_pct": p7_pct,
        "p14_pct": p14_pct,
        "quality_score": score,
        "quality_tier": tier.title() if tier != "n/a" else "N/A",
        "blockers_short": "; ".join(blockers) if blockers else "",
        "regime_label": "ON" if fc.get("regime_on") else "OFF",
        "since_exit": fc.get("days_since_exit"),
        "hist_n": fc.get("n") or fc.get("horizon_n"),
    }


def forecast_sort_key(fc: dict[str, Any]) -> tuple[Any, ...]:
    disp = forecast_display(fc)
    rank = {"ENTER": 0, "IN TRADE": 1, "WATCH": 2, "BLOCKED": 3}.get(disp["status"], 9)
    p7 = fc.get("prob_7d") if fc.get("prob_7d") is not None else -1.0
    days = fc.get("next_in_days") if fc.get("next_in_days") is not None else 999
    gap = fc.get("gap_bps") if fc.get("gap_bps") is not None else 9999
    return (rank, -p7, days, gap)


def forecast_for_symbol(
    symbol: str,
    *,
    latest: dict[str, Any],
    strat_cfg: StrategyConfig,
    trades: pd.DataFrame,
    open_position: dict[str, Any] | None = None,
    pending_entry: dict[str, Any] | None = None,
    start: str = "2018-01-01",
) -> dict[str, Any]:
    raw = load_daily_bars(symbol, start=start)
    df = add_indicators(raw, strat_cfg)
    return forecast_entry(
        latest=latest,
        strat_cfg=strat_cfg,
        trades=trades,
        df=df,
        open_position=open_position,
        pending_entry=pending_entry,
    )


def forecast_label_short(fc: dict[str, Any], *, max_len: int = 42) -> str:
    label = str(fc.get("next_in_label") or "—")
    if len(label) <= max_len:
        return label
    return label[: max_len - 1] + "…"
