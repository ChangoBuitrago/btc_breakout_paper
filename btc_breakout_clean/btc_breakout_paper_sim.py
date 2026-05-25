#!/usr/bin/env python3
"""
Breakout fake-money simulator and data helpers.

Implements long-only breakout signals, regime filters, multi-day holds, optional
trailing exits, and non-compounding or compounding sizing. Supports yfinance
(optional), Dukascopy (H1 resampled to daily), and ad-hoc sources such as
Binance daily candles when wired through SimConfig.

Terminal-first by default; does not place real orders.
"""

from __future__ import annotations

import argparse
import json
import logging
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

DUKASCOPY_CHUNK_DAYS = 180
DEFAULT_DUKASCOPY_INSTRUMENT = "BTCUSD"
DUKASCOPY_INSTRUMENTS = {
    "BTCUSD": "INSTRUMENT_VCCY_BTC_USD",
    "XAUUSD": "INSTRUMENT_FX_METALS_XAU_USD",
    "XAGUSD": "INSTRUMENT_FX_METALS_XAG_USD",
    "XCUUSD": "INSTRUMENT_CMD_METALS_COPPER_CMD_USD",
    "COPPER": "INSTRUMENT_CMD_METALS_COPPER_CMD_USD",
    "CL": "INSTRUMENT_CMD_ENERGY_E_LIGHT",
    "WTI": "INSTRUMENT_CMD_ENERGY_E_LIGHT",
    "BRENT": "INSTRUMENT_CMD_ENERGY_E_BRENT",
    "DIESEL": "INSTRUMENT_CMD_ENERGY_DIESEL_CMD_USD",
    "GAS": "INSTRUMENT_CMD_ENERGY_GAS_CMD_USD",
    "US500": "INSTRUMENT_IDX_AMERICA_E_SANDP_500",
    "NAS100": "INSTRUMENT_IDX_AMERICA_E_NQ_100",
    "US2000": "INSTRUMENT_IDX_AMERICA_RUSSELL_IDX_USD",
    "RUSSELL": "INSTRUMENT_IDX_AMERICA_RUSSELL_IDX_USD",
    "USSC2000": "INSTRUMENT_IDX_AMERICA_USSC2000_IDX_USD",
    "DJIA": "INSTRUMENT_IDX_AMERICA_E_D_J_IND",
    "DAX": "INSTRUMENT_IDX_EUROPE_E_DAAX",
    "UK100": "INSTRUMENT_IDX_EUROPE_E_FUTSEE_100",
    "FTSE": "INSTRUMENT_IDX_EUROPE_E_FUTSEE_100",
    "CAC40": "INSTRUMENT_IDX_EUROPE_E_CAAC_40",
    "N225": "INSTRUMENT_IDX_ASIA_E_N225JAP",
    "NIKKEI": "INSTRUMENT_IDX_ASIA_E_N225JAP",
    "HK50": "INSTRUMENT_IDX_ASIA_E_H_KONG",
    "CHINA50": "INSTRUMENT_IDX_ASIA_CHI_IDX_USD",
    "DOLLAR": "INSTRUMENT_IDX_AMERICA_DOLLAR_IDX_USD",
    "DXY": "INSTRUMENT_IDX_AMERICA_DOLLAR_IDX_USD",
    "DOLLARIDX": "INSTRUMENT_IDX_AMERICA_DOLLAR_IDX_USD",
    "XPTUSD": "INSTRUMENT_CMD_METALS_XPT_CMD_USD",
    "XPT": "INSTRUMENT_CMD_METALS_XPT_CMD_USD",
    "XPDUSD": "INSTRUMENT_CMD_METALS_XPD_CMD_USD",
    "XPD": "INSTRUMENT_CMD_METALS_XPD_CMD_USD",
    "COFFEE": "INSTRUMENT_CMD_AGRICULTURAL_COFFEE_CMD_USX",
    "SUGAR": "INSTRUMENT_CMD_AGRICULTURAL_SUGAR_CMD_USD",
    "COCOA": "INSTRUMENT_CMD_AGRICULTURAL_COCOA_CMD_USD",
    "SOYBEAN": "INSTRUMENT_CMD_AGRICULTURAL_SOYBEAN_CMD_USX",
    "COTTON": "INSTRUMENT_CMD_AGRICULTURAL_COTTON_CMD_USX",
    "EURUSD": "INSTRUMENT_FX_MAJORS_EUR_USD",
    "GBPUSD": "INSTRUMENT_FX_MAJORS_GBP_USD",
    "AUDUSD": "INSTRUMENT_FX_MAJORS_AUD_USD",
    "NZDUSD": "INSTRUMENT_FX_MAJORS_NZD_USD",
    "USDCAD": "INSTRUMENT_FX_MAJORS_USD_CAD",
    "USDCHF": "INSTRUMENT_FX_MAJORS_USD_CHF",
    "USDJPY": "INSTRUMENT_FX_MAJORS_USD_JPY",
    "EURJPY": "INSTRUMENT_FX_CROSSES_EUR_JPY",
    "GBPJPY": "INSTRUMENT_FX_CROSSES_GBP_JPY",
    "AUDJPY": "INSTRUMENT_FX_CROSSES_AUD_JPY",
    "CADJPY": "INSTRUMENT_FX_CROSSES_CAD_JPY",
    "CHFJPY": "INSTRUMENT_FX_CROSSES_CHF_JPY",
    "EURGBP": "INSTRUMENT_FX_CROSSES_EUR_GBP",
    "EURAUD": "INSTRUMENT_FX_CROSSES_EUR_AUD",
    "EURCHF": "INSTRUMENT_FX_CROSSES_EUR_CHF",
    "USTBOND": "INSTRUMENT_BND_CFD_USTBOND_TR_USD",
    "BUND": "INSTRUMENT_BND_CFD_BUND_TR_EUR",
    "UKBOND": "INSTRUMENT_BND_CFD_UKGILT_TR_GBP",
}
TREND_MODE_CHOICES = (
    "all",
    "bull_only",
    "bear_only",
    "sma200",
    "sma200_95",
    "sma200_90",
    "sma100",
    "sma50",
    "sma50_slope_up",
    "sma200_slope_up",
)


@dataclass(frozen=True)
class StrategyConfig:
    lookback: int
    buffer_bps: float
    max_breakout_bps: float | None
    trend_mode: str
    hold_days: int
    trail_atr: float
    fee_bps: float
    vol_target: float
    max_alloc: float
    compound: bool
    hold_min: int | None = None
    hold_max: int | None = None
    dynamic_hold: bool = False
    hold_giveback_pct: float = 0.03
    # Hard stop from entry (fraction below entry_px, e.g. 0.06 = 6%). 0 = off.
    stop_loss_pct: float = 0.0
    # Turtle-style stop: entry_px - stop_atr_mult × n_atr20 (true range). 0 = off.
    stop_atr_mult: float = 0.0
    stop_atr_period: int = 20
    # When True, intraday low can trigger stop; fill at stop price (conservative).
    stop_use_low: bool = True
    # Turtle-style exit: close below prior N-day low (Donchian exit channel). 0 = off.
    exit_channel_lookback: int = 0
    # If True, channel exit replaces momentum_fade (still respects hold_min / hold_max).
    channel_exit_replaces_fade: bool = False
    # Turtle-style trail: low touches peak_close - trail_n_mult × n_atr20 (ratchets with peak). 0 = off.
    trail_n_mult: float = 0.0
    # Sizing: "vol" = vol_target/vol20 (live); "atr_risk" = Turtle 1% risk per 2N unit.
    sizing_mode: str = "vol"
    atr_risk_pct: float = 0.01
    atr_risk_stop_n: float = 2.0
    # System-2 backup entry: also signal on backup_entry_lookback-day high (0 = off).
    backup_entry_lookback: int = 0
    # Pyramiding: max units (1 = no adds); add when prior high >= last_add + pyramid_n_step × N.
    max_pyramid_units: int = 1
    pyramid_n_step: float = 0.5
    # Breakout quality: close in top (1-x) of day range; 0.65 = top 35%. 0 = off.
    breakout_min_close_position: float = 0.0
    # Day range must be >= mult × 20d avg range pct. 0 = off; 1.0 = at least average expansion.
    breakout_min_range_expansion: float = 0.0
    # Require weekly close > weekly SMA (reduces false breakouts on weak days).
    require_weekly_trend: bool = False
    weekly_sma_weeks: int = 40
    # OOB: pending signal expires after N sessions without entry (0 = off).
    signal_max_pending_days: int = 0
    # OOB: skip entry if |open/signal_close-1|×100 exceeds this (0 = off).
    max_gap_entry_pct: float = 0.0
    # OOB: no re-entry for N days after a stop exit (0 = off).
    post_stop_cooldown_days: int = 0
    # OOB: buffer_bps scaled by (1 + mult × (vol20/ref_vol - 1)); 0 = off.
    vol_buffer_vol_mult: float = 0.0
    # OOB: prior close must also clear breakout band (two-day confirmation).
    require_two_close_confirm: bool = False
    # OOB: wider lookback when vol20 > median (high-vol = slower ceiling).
    adaptive_lookback_wide: bool = False
    adaptive_lookback_min: int = 10
    adaptive_lookback_max: int = 40
    # OOB: extend hold_max by N days when price makes new highs after hold_min.
    extend_hold_on_new_highs: int = 0
    extend_hold_max_extra: int = 10
    # OOB: on momentum_fade exit only this fraction (0 = full exit; 0.5 = half runner).
    partial_exit_frac: float = 0.0
    # OOB: skip entry when vol20 above this rolling percentile (1.0 = off).
    meta_vol20_max_pctile: float = 1.0
    # Research: scale vol sizing by breakout strength (bps/buffer), capped at tiered_sizing_max_mult.
    tiered_sizing_by_breakout: bool = False
    tiered_sizing_max_mult: float = 1.5
    # Momentum-fade exit components (dynamic hold); all True = live default.
    momentum_fade_use_giveback: bool = True
    momentum_fade_use_sma50: bool = True
    momentum_fade_use_sma50_slope: bool = True


def effective_hold_min(cfg: StrategyConfig) -> int:
    return int(cfg.hold_min if cfg.hold_min is not None else cfg.hold_days)


def effective_hold_max(cfg: StrategyConfig) -> int:
    if cfg.hold_max is not None:
        return int(cfg.hold_max)
    return effective_hold_min(cfg)


def uses_dynamic_hold(cfg: StrategyConfig) -> bool:
    return bool(cfg.dynamic_hold) and effective_hold_max(cfg) > effective_hold_min(cfg)


def momentum_faded(
    df: pd.DataFrame,
    i: int,
    *,
    peak_close: float,
    giveback_pct: float,
    use_giveback: bool = True,
    use_sma50: bool = True,
    use_sma50_slope: bool = True,
) -> bool:
    """True when open-trade momentum has weakened (evaluated at bar i close)."""
    cur = float(df["close"].iloc[i])
    if use_giveback and peak_close > 0 and cur <= peak_close * (1.0 - giveback_pct):
        return True
    if use_sma50:
        sma50 = float(df["sma50"].iloc[i])
        if np.isfinite(sma50) and cur < sma50:
            return True
    if use_sma50_slope:
        slope = float(df["sma50_slope20"].iloc[i])
        if np.isfinite(slope) and slope < 0:
            return True
    return False


def compute_entry_size_frac(
    df: pd.DataFrame,
    entry_bar_i: int,
    signal_i: int,
    strat_cfg: StrategyConfig,
    sizing_equity: float,
) -> float:
    """Position size as fraction of sizing_equity (notional / equity)."""
    if sizing_equity <= 0.0:
        return 0.0
    if strat_cfg.sizing_mode == "atr_risk":
        px = float(df["open"].iloc[entry_bar_i])
        n_atr = float(df["n_atr20"].iloc[entry_bar_i])
        if not np.isfinite(px) or px <= 0.0 or not np.isfinite(n_atr) or n_atr <= 0.0:
            return 0.0
        stop_n = strat_cfg.atr_risk_stop_n if strat_cfg.atr_risk_stop_n > 0.0 else 2.0
        stop_dist = stop_n * n_atr
        notional = strat_cfg.atr_risk_pct * sizing_equity * px / stop_dist
        return min(strat_cfg.max_alloc, notional / sizing_equity)
    rv = float(df["vol20"].iloc[signal_i])
    if not np.isfinite(rv) or rv <= 0.0:
        return 0.0
    frac = min(strat_cfg.max_alloc, strat_cfg.vol_target / rv)
    if strat_cfg.tiered_sizing_by_breakout and strat_cfg.buffer_bps > 0.0:
        bps = float(df["breakout_bps"].iloc[signal_i])
        if np.isfinite(bps) and bps > 0.0:
            mult = min(float(strat_cfg.tiered_sizing_max_mult), max(1.0, bps / strat_cfg.buffer_bps))
            frac = min(strat_cfg.max_alloc, frac * mult)
    return frac


def channel_exit_triggered(df: pd.DataFrame, i: int, *, lookback: int, cur_close: float) -> bool:
    """True when close breaks below the prior lookback-session low channel (Turtle exit)."""
    if lookback <= 0:
        return False
    exit_level = float(df["exit_channel_level"].iloc[i]) if "exit_channel_level" in df.columns else float("nan")
    return np.isfinite(exit_level) and cur_close < exit_level


def true_range_series(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    hl = df["high"] - df["low"]
    hc = (df["high"] - prev_close).abs()
    lc = (df["low"] - prev_close).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1)


def entry_hard_stop_px(entry_px: float, n_atr: float, cfg: StrategyConfig) -> float:
    """Long stop limit: highest (tightest) of pct and ATR floors. 0 = no stop."""
    if entry_px <= 0.0:
        return 0.0
    floors: list[float] = []
    if cfg.stop_loss_pct > 0.0:
        floors.append(entry_px * (1.0 - cfg.stop_loss_pct))
    if cfg.stop_atr_mult > 0.0 and np.isfinite(n_atr) and n_atr > 0.0:
        floors.append(entry_px - cfg.stop_atr_mult * n_atr)
    if not floors:
        return 0.0
    return max(floors)


def hard_stop_triggered(
    df: pd.DataFrame,
    i: int,
    *,
    stop_px: float,
    stop_use_low: bool,
) -> bool:
    if stop_px <= 0.0:
        return False
    if stop_use_low:
        return float(df["low"].iloc[i]) <= stop_px
    return float(df["close"].iloc[i]) <= stop_px


def stop_exit_reason(entry_px: float, n_atr: float, stop_px: float, cfg: StrategyConfig) -> str:
    if stop_px <= 0.0:
        return "stop_loss"
    px_atr = (
        entry_px - cfg.stop_atr_mult * n_atr
        if cfg.stop_atr_mult > 0.0 and np.isfinite(n_atr) and n_atr > 0.0
        else 0.0
    )
    px_pct = entry_px * (1.0 - cfg.stop_loss_pct) if cfg.stop_loss_pct > 0.0 else 0.0
    if px_atr > 0.0 and stop_px >= px_atr - 1e-12:
        return "stop_atr"
    if px_pct > 0.0 and stop_px >= px_pct - 1e-12:
        return "stop_loss"
    return "stop_loss"


@dataclass(frozen=True)
class SimConfig:
    source: str
    data_start: str
    sim_start: pd.Timestamp
    end: str | None
    equity: float
    include_current: bool
    cache_path: Path
    dukascopy_path: Path
    refresh_cache: bool
    show_trades: int
    write_files: bool
    out_dir: Path
    instrument: str = DEFAULT_DUKASCOPY_INSTRUMENT
    skip_saturday_entry: bool = False
    hwm_pause_pct: float | None = None
    blocked_entry_dates: frozenset[pd.Timestamp] = frozenset()
    # Research: TWAP-style entry slippage = max(min_bps, vol_mult × vol20). 0 = off (live default).
    entry_slippage_min_bps: float = 0.0
    entry_slippage_vol_mult: float = 0.0


def default_skip_saturday_entry(source: str) -> bool:
    """Dukascopy daily bars include Saturday; align with Pine skip_sat_entry."""
    return source == "dukascopy"


def apply_entry_slippage(open_px: float, vol20: float, sim_cfg: SimConfig) -> float:
    """Research hook: worse long entry at open (TWAP proxy). Defaults preserve live fills."""
    if sim_cfg.entry_slippage_min_bps <= 0.0 and sim_cfg.entry_slippage_vol_mult <= 0.0:
        return open_px
    slip = max(sim_cfg.entry_slippage_min_bps / 10_000.0, sim_cfg.entry_slippage_vol_mult * vol20)
    return open_px * (1.0 + slip)


def normalize_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).strip().lower() for c in df.columns]

    required = {"open", "high", "low", "close"}
    if not required.issubset(df.columns):
        raise RuntimeError(f"Data missing OHLC columns: {list(df.columns)}")

    df.index = pd.to_datetime(df.index, utc=True)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"])


def latest_needed_date(end: str | None, include_current: bool) -> pd.Timestamp:
    if end:
        return pd.Timestamp(end, tz="UTC").normalize() - pd.Timedelta(days=1)
    today = pd.Timestamp.today(tz="UTC").normalize()
    return today if include_current else today - pd.Timedelta(days=1)


def cache_is_fresh(df: pd.DataFrame, start: str, end: str | None, include_current: bool) -> bool:
    if df.empty:
        return False
    start_ts = pd.Timestamp(start, tz="UTC").normalize()
    return df.index.min() <= start_ts and df.index.max() >= latest_needed_date(end, include_current)


def load_cached_btc(cache_path: Path, start: str, end: str | None, include_current: bool) -> pd.DataFrame:
    if not cache_path.exists():
        return pd.DataFrame()
    df = pd.read_csv(cache_path, index_col=0)
    df = normalize_ohlc(df)
    if not include_current:
        df = df.loc[df.index < pd.Timestamp.utcnow().normalize()]
    if end:
        df = df.loc[df.index < pd.Timestamp(end, tz="UTC").normalize()]
    return df.loc[df.index >= pd.Timestamp(start, tz="UTC").normalize()]


def download_btc(start: str, end: str | None, include_current: bool) -> pd.DataFrame:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("Install yfinance to use --source yfinance or --source compare") from exc

    today = pd.Timestamp.today(tz="UTC").strftime("%Y-%m-%d")
    tomorrow = (pd.Timestamp.today(tz="UTC") + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    end_candidates = [end] if end else ([tomorrow, today, None] if include_current else [today, None])

    df = pd.DataFrame()
    attempted: list[str] = []
    for dl_end in end_candidates:
        attempted.append(str(dl_end or "latest"))
        try:
            raw = yf.download("BTC-USD", start=start, end=dl_end, progress=False, auto_adjust=False)
        except Exception:
            continue
        df = raw if not isinstance(raw, tuple) else raw[0]
        if not df.empty:
            break

    if df.empty:
        raise RuntimeError(f"BTC-USD download returned empty data; attempted end={attempted}")
    df = normalize_ohlc(df)

    if not include_current and not df.empty:
        df = df.loc[df.index < pd.Timestamp.utcnow().normalize()]
    return df


def fetch_btc(start: str, end: str | None, include_current: bool, cache_path: Path, refresh_cache: bool) -> pd.DataFrame:
    cached = pd.DataFrame()
    if not refresh_cache:
        cached = load_cached_btc(cache_path, start, end, include_current)
        if cache_is_fresh(cached, start, end, include_current):
            return cached

    try:
        df = download_btc(start, end, include_current)
    except Exception:
        if not cached.empty:
            return cached
        raise

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache_path)
    return df


@contextmanager
def quiet_dukascopy_logs():
    root = logging.getLogger()
    duka = logging.getLogger("DUKASCRIPT")
    root_level, duka_level, duka_disabled = root.level, duka.level, duka.disabled
    root.setLevel(logging.WARNING)
    duka.disabled = True
    try:
        yield
    finally:
        duka.disabled = duka_disabled
        duka.setLevel(duka_level)
        root.setLevel(root_level)


def dukascopy_cache_path(instrument: str) -> Path:
    return Path(__file__).resolve().parent / "cache" / f"{instrument.upper()}_dukascopy_h1.csv"


def resolve_dukascopy_instrument(instrument: str) -> str:
    key = instrument.upper().replace("/", "").replace("-", "").replace("_", "")
    return DUKASCOPY_INSTRUMENTS.get(key, instrument)


def download_dukascopy_h1(instrument: str, start: str, end: str | None) -> pd.DataFrame:
    import dukascopy_python as dka
    import dukascopy_python.instruments as ins

    instrument_name = resolve_dukascopy_instrument(instrument)
    instr = getattr(ins, instrument_name)
    t0 = pd.Timestamp(start, tz="UTC")
    t1 = pd.Timestamp(end, tz="UTC") if end else pd.Timestamp.now(tz="UTC")
    t1 = min(t1, pd.Timestamp.now(tz="UTC"))

    parts: list[pd.DataFrame] = []
    cur = t0
    with quiet_dukascopy_logs():
        while cur < t1:
            nxt = min(cur + pd.Timedelta(days=DUKASCOPY_CHUNK_DAYS), t1)
            chunk = dka.fetch(
                instr,
                dka.INTERVAL_HOUR_1,
                dka.OFFER_SIDE_BID,
                cur.to_pydatetime(),
                nxt.to_pydatetime(),
            )
            if len(chunk):
                parts.append(chunk)
            cur = nxt

    if not parts:
        raise RuntimeError(f"Dukascopy returned empty data for {instrument}")

    h1 = pd.concat(parts, axis=0)
    h1 = normalize_ohlc(h1)
    if "volume" not in h1.columns:
        h1["volume"] = 100.0
    return h1


def download_dukascopy_btc_h1(start: str, end: str | None) -> pd.DataFrame:
    return download_dukascopy_h1("BTCUSD", start, end)


def resample_h1_to_daily(h1: pd.DataFrame) -> pd.DataFrame:
    return normalize_ohlc(h1.resample("D").agg({"open": "first", "high": "max", "low": "min", "close": "last"}))


def fetch_dukascopy_instrument(
    instrument: str,
    path: Path,
    start: str,
    end: str | None,
    include_current: bool,
    refresh_cache: bool,
) -> pd.DataFrame:
    cached = pd.DataFrame()
    if not path.exists():
        refresh_cache = True
    else:
        cached = normalize_ohlc(pd.read_csv(path, index_col=0, parse_dates=True))

    if not refresh_cache and cache_is_fresh(cached, start, end, include_current):
        daily = resample_h1_to_daily(cached)
    else:
        try:
            h1 = download_dukascopy_h1(instrument, start, end)
            path.parent.mkdir(parents=True, exist_ok=True)
            h1.to_csv(path)
            daily = resample_h1_to_daily(h1)
        except Exception:
            if cached.empty:
                raise
            daily = resample_h1_to_daily(cached)

    if not include_current:
        daily = daily.loc[daily.index < pd.Timestamp.utcnow().normalize()]
    if end:
        daily = daily.loc[daily.index < pd.Timestamp(end, tz="UTC").normalize()]
    return daily.loc[daily.index >= pd.Timestamp(start, tz="UTC").normalize()]


def fetch_dukascopy_btc(path: Path, start: str, end: str | None, include_current: bool, refresh_cache: bool) -> pd.DataFrame:
    return fetch_dukascopy_instrument("BTCUSD", path, start, end, include_current, refresh_cache)


def fetch_source_data(sim_cfg: SimConfig) -> pd.DataFrame:
    if sim_cfg.source == "yfinance":
        return fetch_btc(
            sim_cfg.data_start,
            sim_cfg.end,
            sim_cfg.include_current,
            sim_cfg.cache_path,
            sim_cfg.refresh_cache,
        )
    if sim_cfg.source == "dukascopy":
        return fetch_dukascopy_instrument(
            sim_cfg.instrument,
            sim_cfg.dukascopy_path,
            sim_cfg.data_start,
            sim_cfg.end,
            sim_cfg.include_current,
            sim_cfg.refresh_cache,
        )
    raise ValueError(f"Unsupported source for single run: {sim_cfg.source}")


def add_indicators(df: pd.DataFrame, cfg: StrategyConfig) -> pd.DataFrame:
    out = df.copy()
    out["ret"] = out["close"].pct_change()
    out["vol20"] = out["ret"].rolling(20).std()
    out["atr14"] = out["close"].pct_change().abs().rolling(14).mean()
    period = max(int(cfg.stop_atr_period), 1)
    out["n_atr20"] = true_range_series(out).rolling(period).mean()
    out["sma50"] = out["close"].rolling(50).mean()
    out["sma100"] = out["close"].rolling(100).mean()
    out["sma200"] = out["close"].rolling(200).mean()
    out["sma50_slope20"] = out["sma50"] - out["sma50"].shift(20)
    out["sma200_slope20"] = out["sma200"] - out["sma200"].shift(20)
    ref_vol = out["vol20"].rolling(252, min_periods=60).median()
    lb_narrow = max(int(cfg.adaptive_lookback_min), int(cfg.lookback))
    lb_wide = min(int(cfg.adaptive_lookback_max), max(lb_narrow + 5, int(cfg.lookback * 1.5)))
    if cfg.adaptive_lookback_wide:
        use_wide = (out["vol20"] > ref_vol * 1.05).fillna(False)
        prior_narrow = out["close"].rolling(lb_narrow).max()
        prior_wide = out["close"].rolling(lb_wide).max()
        prior_combo = pd.Series(
            np.where(use_wide.to_numpy(), prior_wide.to_numpy(), prior_narrow.to_numpy()),
            index=out.index,
        )
        out["prior_high"] = prior_combo.shift(1)
    else:
        out["prior_high"] = out["close"].rolling(cfg.lookback).max().shift(1)
    out["vol20_pctile"] = out["vol20"].rolling(252, min_periods=60).rank(pct=True)
    if cfg.exit_channel_lookback > 0:
        lb = int(cfg.exit_channel_lookback)
        out["exit_channel_level"] = out["low"].rolling(lb).min().shift(1)
    out["breakout_bps"] = 10_000.0 * (out["close"] / out["prior_high"] - 1.0)
    hl_range = out["high"] - out["low"]
    out["close_position"] = np.where(hl_range > 0, (out["close"] - out["low"]) / hl_range, 0.5)
    out["day_range_pct"] = hl_range / out["close"].replace(0, np.nan)
    out["avg_range20"] = out["day_range_pct"].rolling(20).mean()
    wks = max(int(cfg.weekly_sma_weeks), 4)
    weekly_close = out["close"].resample("W-FRI").last()
    weekly_sma = weekly_close.rolling(wks).mean()
    out["weekly_trend_on"] = (weekly_close > weekly_sma).reindex(out.index, method="ffill").fillna(False)

    buf_series = pd.Series(float(cfg.buffer_bps), index=out.index, dtype=float)
    if cfg.vol_buffer_vol_mult > 0.0:
        vol_scale = (1.0 + cfg.vol_buffer_vol_mult * (out["vol20"] / ref_vol - 1.0)).clip(0.85, 1.5)
        buf_series = buf_series * vol_scale
    level_mult = 1.0 + buf_series / 10_000.0
    primary = out["close"] > out["prior_high"] * level_mult
    if cfg.require_two_close_confirm:
        prev_level = out["prior_high"].shift(1) * level_mult.shift(1)
        primary &= out["close"].shift(1) > prev_level
    if cfg.max_breakout_bps is not None:
        primary &= out["breakout_bps"] <= cfg.max_breakout_bps
    if cfg.breakout_min_close_position > 0.0:
        primary &= out["close_position"] >= cfg.breakout_min_close_position
    if cfg.breakout_min_range_expansion > 0.0:
        min_range = cfg.breakout_min_range_expansion * out["avg_range20"]
        primary &= np.isfinite(min_range) & (out["day_range_pct"] >= min_range)
    out["signal"] = primary
    if cfg.backup_entry_lookback > 0:
        bl = int(cfg.backup_entry_lookback)
        out["prior_high_backup"] = out["close"].rolling(bl).max().shift(1)
        backup = out["close"] > out["prior_high_backup"] * (1.0 + cfg.buffer_bps / 10_000.0)
        if cfg.max_breakout_bps is not None:
            backup_bps = 10_000.0 * (out["close"] / out["prior_high_backup"] - 1.0)
            backup &= backup_bps <= cfg.max_breakout_bps
        out["signal"] = out["signal"] | backup.fillna(False)
    sma200_bull = out["close"] > out["sma200"]
    out["bull"] = sma200_bull
    out["bear"] = out["close"] < out["sma200"]
    if cfg.trend_mode in {"bull_only", "sma200"}:
        regime = sma200_bull
    elif cfg.trend_mode == "bear_only":
        regime = out["bear"]
    elif cfg.trend_mode == "sma200_95":
        regime = out["close"] > out["sma200"] * 0.95
    elif cfg.trend_mode == "sma200_90":
        regime = out["close"] > out["sma200"] * 0.90
    elif cfg.trend_mode == "sma100":
        regime = out["close"] > out["sma100"]
    elif cfg.trend_mode == "sma50":
        regime = out["close"] > out["sma50"]
    elif cfg.trend_mode == "sma50_slope_up":
        regime = (out["close"] > out["sma50"]) & (out["sma50_slope20"] > 0)
    elif cfg.trend_mode == "sma200_slope_up":
        regime = out["sma200_slope20"] > 0
    elif cfg.trend_mode == "all":
        regime = pd.Series(True, index=out.index)
    else:
        raise ValueError(f"Unsupported trend_mode: {cfg.trend_mode}")
    out["regime_on"] = regime.fillna(False)
    out["signal"] &= out["regime_on"]
    if cfg.require_weekly_trend:
        out["signal"] &= out["weekly_trend_on"]
    out["signal"] = out["signal"].fillna(False)
    return out


def profit_factor(pnls: pd.Series) -> float:
    wins = float(pnls[pnls > 0].sum())
    losses = float(pnls[pnls < 0].sum())
    return wins / abs(losses) if losses < 0 else float("nan")


def max_drawdown(equity: pd.Series) -> float:
    dd = equity / equity.cummax() - 1.0
    return float(dd.min()) if len(dd) else float("nan")


def cagr(total_return: float, start: pd.Timestamp, end: pd.Timestamp) -> float:
    years = max((end - start).days / 365.25, 1e-9)
    return (1.0 + total_return) ** (1.0 / years) - 1.0


def apr(total_return: float, start: pd.Timestamp, end: pd.Timestamp) -> float:
    years = max((end - start).days / 365.25, 1e-9)
    return total_return / years


def simulate_account(
    df: pd.DataFrame,
    *,
    sim_cfg: SimConfig,
    strat_cfg: StrategyConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    fee = strat_cfg.fee_bps / 10_000.0
    equity = float(sim_cfg.equity)
    fixed_sizing_equity = float(sim_cfg.equity)
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    in_pos = False
    entry_i = 0
    signal_i_at_entry = 0
    entry_px = 0.0
    entry_notional = 0.0
    entry_fee = 0.0
    qty = 0.0
    size_frac = 0.0
    equity_before_entry = equity
    peak_close = 0.0
    entry_stop_px = 0.0
    entry_n_atr = 0.0
    pyramid_units = 0
    last_add_px = 0.0
    pending_signal_i: int | None = None
    pending_set_i: int | None = None
    cooldown_until_i: int = -1
    extra_hold_days = 0
    partial_taken = False
    equity_peak = float(sim_cfg.equity)
    entries_paused = False

    for i in range(1, len(df)):
        entry_date = df.index[i]
        if entry_date < sim_cfg.sim_start:
            continue

        day_pnl = 0.0
        action = "HOLD" if in_pos else "NO_SIGNAL"
        todays_signal_i = i - 1
        todays_signal = bool(df["signal"].iloc[todays_signal_i])
        todays_size_frac = 0.0
        entry_day = entry_date.normalize()

        if not in_pos and todays_signal:
            pending_signal_i = todays_signal_i
            pending_set_i = todays_signal_i

        if (
            not in_pos
            and pending_signal_i is not None
            and strat_cfg.signal_max_pending_days > 0
            and pending_set_i is not None
            and (i - pending_set_i) > strat_cfg.signal_max_pending_days
        ):
            pending_signal_i = None
            pending_set_i = None

        equity_peak = max(equity_peak, equity)
        if sim_cfg.hwm_pause_pct is not None and sim_cfg.hwm_pause_pct > 0:
            pause_floor = equity_peak * (1.0 - sim_cfg.hwm_pause_pct / 100.0)
            if equity < pause_floor:
                entries_paused = True
            elif equity >= pause_floor:
                entries_paused = False

        if not in_pos and pending_signal_i is not None:
            saturday_block = sim_cfg.skip_saturday_entry and entry_day.dayofweek == 5
            veto_block = entry_day in sim_cfg.blocked_entry_dates
            hwm_block = entries_paused
            if veto_block:
                pending_signal_i = None
            elif not saturday_block and not hwm_block and i > cooldown_until_i:
                signal_i = pending_signal_i
                signal_close = float(df["close"].iloc[signal_i])
                gap_pct = 100.0 * abs(float(df["open"].iloc[i]) / signal_close - 1.0)
                gap_block = (
                    strat_cfg.max_gap_entry_pct > 0.0 and gap_pct > strat_cfg.max_gap_entry_pct
                )
                vol_pct = float(df["vol20_pctile"].iloc[signal_i]) if "vol20_pctile" in df.columns else 1.0
                meta_block = (
                    strat_cfg.meta_vol20_max_pctile < 1.0
                    and np.isfinite(vol_pct)
                    and vol_pct > strat_cfg.meta_vol20_max_pctile
                )
                sizing_base = equity if strat_cfg.compound else fixed_sizing_equity
                todays_size_frac = 0.0 if gap_block or meta_block else compute_entry_size_frac(
                    df, i, signal_i, strat_cfg, sizing_base
                )
                if todays_size_frac > 0.0:
                    raw_open = float(df["open"].iloc[i])
                    vol20 = float(df["vol20"].iloc[signal_i]) if "vol20" in df.columns else 0.0
                    entry_px = apply_entry_slippage(raw_open, vol20, sim_cfg)
                    entry_notional = sizing_base * todays_size_frac
                    qty = entry_notional / entry_px
                    entry_fee = entry_notional * fee
                    equity_before_entry = equity
                    equity -= entry_fee
                    day_pnl -= entry_fee
                    in_pos = True
                    entry_i = i
                    signal_i_at_entry = signal_i
                    size_frac = todays_size_frac
                    peak_close = float(df["close"].iloc[i])
                    entry_n_atr = float(df["n_atr20"].iloc[i])
                    entry_stop_px = entry_hard_stop_px(entry_px, entry_n_atr, strat_cfg)
                    pyramid_units = 1
                    last_add_px = entry_px
                    action = "ENTRY"
                    pending_signal_i = None
                    pending_set_i = None
                    extra_hold_days = 0
                    partial_taken = False
                elif gap_block or meta_block:
                    pending_signal_i = None
                    pending_set_i = None

        if in_pos:
            hold_bars = i - entry_i + 1
            if (
                strat_cfg.max_pyramid_units > 1
                and pyramid_units < strat_cfg.max_pyramid_units
                and hold_bars > 1
            ):
                n_p = float(df["n_atr20"].iloc[i])
                if np.isfinite(n_p) and n_p > 0.0:
                    trigger_px = last_add_px + strat_cfg.pyramid_n_step * n_p
                    if float(df["high"].iloc[i - 1]) >= trigger_px:
                        sizing_base_p = equity if strat_cfg.compound else fixed_sizing_equity
                        add_frac = compute_entry_size_frac(df, i, i - 1, strat_cfg, sizing_base_p)
                        if add_frac > 0.0:
                            raw_add = float(df["open"].iloc[i])
                            vol20_p = float(df["vol20"].iloc[i - 1]) if "vol20" in df.columns else 0.0
                            add_px = apply_entry_slippage(raw_add, vol20_p, sim_cfg)
                            add_notional = sizing_base_p * add_frac
                            add_qty = add_notional / add_px
                            add_fee = add_notional * fee
                            equity -= add_fee
                            day_pnl -= add_fee
                            entry_fee += add_fee
                            entry_notional += add_notional
                            qty += add_qty
                            entry_px = entry_notional / qty
                            last_add_px = add_px
                            pyramid_units += 1
                            entry_stop_px = max(
                                entry_stop_px,
                                entry_hard_stop_px(add_px, n_p, strat_cfg),
                            )
                            action = "PYRAMID"

            cur_close = float(df["close"].iloc[i])
            atr = float(df["atr14"].iloc[i]) if np.isfinite(df["atr14"].iloc[i]) else 0.0
            peak_close = max(peak_close, cur_close)
            stop_hit = hard_stop_triggered(
                df, i, stop_px=entry_stop_px, stop_use_low=strat_cfg.stop_use_low
            )
            stop_px = entry_stop_px
            trail_n_px = 0.0
            trail_n_hit = False
            if not stop_hit and strat_cfg.trail_n_mult > 0.0:
                n_tr = float(df["n_atr20"].iloc[i])
                if np.isfinite(n_tr) and n_tr > 0.0:
                    trail_n_px = peak_close - strat_cfg.trail_n_mult * n_tr
                    trail_n_hit = hard_stop_triggered(
                        df, i, stop_px=trail_n_px, stop_use_low=strat_cfg.stop_use_low
                    )
            trail_atr_hit = (
                not stop_hit
                and not trail_n_hit
                and strat_cfg.trail_atr > 0.0
                and atr > 0.0
                and cur_close < peak_close * (1.0 - strat_cfg.trail_atr * atr)
            )
            trail_hit = trail_n_hit or trail_atr_hit
            hmin = effective_hold_min(strat_cfg)
            hmax = effective_hold_max(strat_cfg)
            if (
                strat_cfg.extend_hold_on_new_highs > 0
                and hold_bars >= hmin
                and cur_close >= peak_close * (1.0 - 1e-12)
                and extra_hold_days < strat_cfg.extend_hold_max_extra
            ):
                extra_hold_days = min(
                    extra_hold_days + strat_cfg.extend_hold_on_new_highs,
                    strat_cfg.extend_hold_max_extra,
                )
            hmax_eff = hmax + extra_hold_days
            channel_hit = (
                not stop_hit
                and not trail_hit
                and strat_cfg.exit_channel_lookback > 0
                and hold_bars >= hmin
                and channel_exit_triggered(
                    df, i, lookback=strat_cfg.exit_channel_lookback, cur_close=cur_close
                )
            )
            if uses_dynamic_hold(strat_cfg):
                faded = False if strat_cfg.channel_exit_replaces_fade else momentum_faded(
                    df,
                    i,
                    peak_close=peak_close,
                    giveback_pct=strat_cfg.hold_giveback_pct,
                    use_giveback=strat_cfg.momentum_fade_use_giveback,
                    use_sma50=strat_cfg.momentum_fade_use_sma50,
                    use_sma50_slope=strat_cfg.momentum_fade_use_sma50_slope,
                )
                time_exit = hold_bars >= hmax_eff or (
                    hold_bars >= hmin and (channel_hit or faded)
                )
                target_exit = time_exit
                if hold_bars >= hmax_eff:
                    exit_reason = "max_hold"
                elif channel_hit:
                    exit_reason = "channel_exit"
                elif faded:
                    exit_reason = "momentum_fade"
                else:
                    exit_reason = ""
            else:
                target_exit = hold_bars >= strat_cfg.hold_days
                exit_reason = "fixed_hold" if target_exit else ""

            partial_now = (
                target_exit
                and not stop_hit
                and not trail_hit
                and exit_reason == "momentum_fade"
                and strat_cfg.partial_exit_frac > 0.0
                and strat_cfg.partial_exit_frac < 1.0
                and not partial_taken
            )
            if partial_now:
                frac = float(strat_cfg.partial_exit_frac)
                exit_qty = qty * frac
                exit_px = cur_close
                exit_notional_part = exit_qty * exit_px
                exit_fee_part = exit_notional_part * fee
                gross_part = exit_notional_part - entry_notional * frac
                net_part = gross_part - exit_fee_part
                day_pnl += net_part
                equity += net_part
                qty *= 1.0 - frac
                entry_notional *= 1.0 - frac
                entry_fee *= 1.0 - frac
                partial_taken = True
                target_exit = False
                action = "PARTIAL_EXIT"

            if stop_hit or target_exit or trail_hit:
                signal_close = float(df["close"].iloc[signal_i_at_entry])
                signal_prior_high = float(df["prior_high"].iloc[signal_i_at_entry])
                if stop_hit:
                    exit_px = stop_px
                elif trail_n_hit:
                    exit_px = trail_n_px
                else:
                    exit_px = cur_close
                exit_notional = qty * exit_px
                exit_fee = exit_notional * fee
                gross_pnl = exit_notional - entry_notional
                exit_pnl = gross_pnl - exit_fee
                fees = entry_fee + exit_fee
                net_pnl = gross_pnl - fees
                day_pnl += exit_pnl
                equity += exit_pnl
                action = ("ENTRY_EXIT" if hold_bars == 1 else "EXIT") + (
                    "_STOP" if stop_hit else ("_TRAIL" if trail_hit else "")
                )
                if stop_hit:
                    exit_reason = stop_exit_reason(entry_px, entry_n_atr, stop_px, strat_cfg)
                elif trail_n_hit:
                    exit_reason = "trail_n"
                elif trail_atr_hit:
                    exit_reason = "trail"

                trades.append(
                    {
                        "signal_date": df.index[signal_i_at_entry].isoformat(),
                        "entry_date": df.index[entry_i].isoformat(),
                        "exit_date": entry_date.isoformat(),
                        "hold_days": hold_bars,
                        "hold_min": hmin,
                        "hold_max": hmax,
                        "exit_reason": exit_reason,
                        "signal_close": signal_close,
                        "signal_prior_high": signal_prior_high,
                        "breakout_bps": float(df["breakout_bps"].iloc[signal_i_at_entry]),
                        "trend_mode": strat_cfg.trend_mode,
                        "entry_px": entry_px,
                        "exit_px": exit_px,
                        "next_open_gap_pct": 100.0 * (entry_px / signal_close - 1.0),
                        "open_to_exit_pct": 100.0 * (exit_px / entry_px - 1.0),
                        "qty": qty,
                        "size_frac": size_frac,
                        "sizing_base": equity_before_entry if strat_cfg.compound else fixed_sizing_equity,
                        "entry_notional": entry_notional,
                        "gross_pnl": gross_pnl,
                        "fees": fees,
                        "net_pnl": net_pnl,
                        "net_ret_on_equity": net_pnl / equity_before_entry if equity_before_entry else 0.0,
                        "equity_after": equity,
                        "trail_stop": trail_hit,
                        "pyramid_units": pyramid_units,
                    }
                )
                in_pos = False
                pyramid_units = 0
                extra_hold_days = 0
                partial_taken = False
                if stop_hit and strat_cfg.post_stop_cooldown_days > 0:
                    cooldown_until_i = i + int(strat_cfg.post_stop_cooldown_days)

        pending_size_frac = 0.0
        if pending_signal_i is not None and not in_pos:
            sizing_base_pend = equity if strat_cfg.compound else fixed_sizing_equity
            pending_size_frac = compute_entry_size_frac(
                df, i, pending_signal_i, strat_cfg, sizing_base_pend
            )

        curve.append(
            {
                "date": entry_date.isoformat(),
                "equity": equity,
                "daily_pnl": day_pnl,
                "action": action,
                "signal_date": df.index[todays_signal_i].isoformat(),
                "signal": todays_signal,
                "size_frac": size_frac if in_pos else todays_size_frac,
                "in_position": in_pos,
                "pending_entry": pending_signal_i is not None and not in_pos,
                "pending_signal_date": (
                    df.index[pending_signal_i].isoformat() if pending_signal_i is not None else None
                ),
                "pending_size_frac": pending_size_frac,
            }
        )

    if in_pos and curve:
        last_i = len(df) - 1
        exit_px = float(df["close"].iloc[last_i])
        exit_notional = qty * exit_px
        exit_fee = exit_notional * fee
        gross_pnl = exit_notional - entry_notional
        exit_pnl = gross_pnl - exit_fee
        fees = entry_fee + exit_fee
        net_pnl = gross_pnl - fees
        equity += exit_pnl
        signal_close = float(df["close"].iloc[signal_i_at_entry])
        signal_prior_high = float(df["prior_high"].iloc[signal_i_at_entry])
        hmin_f = effective_hold_min(strat_cfg)
        hmax_f = effective_hold_max(strat_cfg)
        trades.append(
            {
                "signal_date": df.index[signal_i_at_entry].isoformat(),
                "entry_date": df.index[entry_i].isoformat(),
                "exit_date": df.index[last_i].isoformat(),
                "hold_days": last_i - entry_i + 1,
                "hold_min": hmin_f,
                "hold_max": hmax_f,
                "exit_reason": "force_exit",
                "signal_close": signal_close,
                "signal_prior_high": signal_prior_high,
                "breakout_bps": float(df["breakout_bps"].iloc[signal_i_at_entry]),
                "trend_mode": strat_cfg.trend_mode,
                "entry_px": entry_px,
                "exit_px": exit_px,
                "next_open_gap_pct": 100.0 * (entry_px / signal_close - 1.0),
                "open_to_exit_pct": 100.0 * (exit_px / entry_px - 1.0),
                "qty": qty,
                "size_frac": size_frac,
                "sizing_base": equity_before_entry if strat_cfg.compound else fixed_sizing_equity,
                "entry_notional": entry_notional,
                "gross_pnl": gross_pnl,
                "fees": fees,
                "net_pnl": net_pnl,
                "net_ret_on_equity": net_pnl / equity_before_entry if equity_before_entry else 0.0,
                "equity_after": equity,
                "trail_stop": False,
            }
        )
        curve[-1]["equity"] = equity
        curve[-1]["daily_pnl"] = float(curve[-1]["daily_pnl"]) + exit_pnl
        curve[-1]["action"] = "FORCE_EXIT"
        curve[-1]["in_position"] = False

    trades_df = pd.DataFrame(trades)
    curve_df = pd.DataFrame(curve)
    return trades_df, curve_df, summarize(trades_df, curve_df, sim_cfg)


def summarize(trades: pd.DataFrame, curve: pd.DataFrame, sim_cfg: SimConfig) -> dict[str, Any]:
    if curve.empty:
        return {
            "trades": 0,
            "initial_equity": sim_cfg.equity,
            "final_equity": sim_cfg.equity,
            "net_pnl": 0.0,
            "return_pct": 0.0,
            "apr_pct": 0.0,
            "cagr_pct": 0.0,
            "max_drawdown_pct": float("nan"),
            "win_rate_pct": float("nan"),
            "profit_factor": float("nan"),
            "exposure_pct": 0.0,
            "avg_size_pct": 0.0,
        }

    equity = pd.to_numeric(curve["equity"], errors="coerce")
    final_equity = float(equity.iloc[-1])
    total_return = final_equity / float(sim_cfg.equity) - 1.0
    pnls = pd.to_numeric(trades["net_pnl"], errors="coerce") if not trades.empty else pd.Series(dtype=float)
    wins = int((pnls > 0).sum()) if len(pnls) else 0
    avg_size = float(pd.to_numeric(trades["size_frac"], errors="coerce").mean()) if not trades.empty else 0.0
    dates = pd.to_datetime(curve["date"], utc=True)
    start_date, end_date = dates.iloc[0], dates.iloc[-1]
    exposed = curve["action"].astype(str).ne("NO_SIGNAL") if "action" in curve.columns else pd.Series(dtype=bool)

    return {
        "trades": int(len(trades)),
        "initial_equity": float(sim_cfg.equity),
        "final_equity": final_equity,
        "net_pnl": final_equity - float(sim_cfg.equity),
        "return_pct": 100.0 * total_return,
        "apr_pct": 100.0 * apr(total_return, start_date, end_date),
        "cagr_pct": 100.0 * cagr(total_return, start_date, end_date),
        "max_drawdown_pct": 100.0 * max_drawdown(equity),
        "win_rate_pct": 100.0 * wins / len(pnls) if len(pnls) else float("nan"),
        "profit_factor": profit_factor(pnls) if len(pnls) else float("nan"),
        "exposure_pct": 100.0 * float(exposed.mean()) if len(exposed) else 0.0,
        "avg_size_pct": 100.0 * avg_size,
    }


def latest_signal_report(df: pd.DataFrame, strat_cfg: StrategyConfig) -> dict[str, Any]:
    if df.empty:
        return {}
    last = df.iloc[-1]
    rv = float(last["vol20"])
    size_frac = min(strat_cfg.max_alloc, strat_cfg.vol_target / rv) if np.isfinite(rv) and rv > 0 else 0.0
    breakout_bps = float(last["breakout_bps"]) if np.isfinite(last["breakout_bps"]) else None
    return {
        "signal_date": df.index[-1].isoformat(),
        "close": float(last["close"]),
        "prior_high": float(last["prior_high"]) if np.isfinite(last["prior_high"]) else None,
        "breakout_bps": breakout_bps,
        "sma200": float(last["sma200"]) if np.isfinite(last["sma200"]) else None,
        "bull": bool(last["bull"]),
        "regime_on": bool(last.get("regime_on", last["bull"])),
        "trend_mode": strat_cfg.trend_mode,
        "vol20": rv if np.isfinite(rv) else None,
        "signal": bool(last["signal"]),
        "next_size_frac": size_frac if bool(last["signal"]) else 0.0,
    }


def write_outputs(
    out_dir: Path,
    trades: pd.DataFrame,
    curve: pd.DataFrame,
    summary: dict[str, Any],
    latest: dict[str, Any],
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    trades.to_csv(out_dir / "paper_trades.csv", index=False)
    curve.to_csv(out_dir / "paper_equity.csv", index=False)
    with (out_dir / "paper_summary.json").open("w", encoding="utf-8") as f:
        json.dump({"summary": summary, "latest_signal": latest}, f, indent=2)


def yearly_rows(trades: pd.DataFrame) -> list[dict[str, Any]]:
    if trades.empty:
        return []
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    t["net_pnl"] = pd.to_numeric(t["net_pnl"], errors="coerce")

    rows: list[dict[str, Any]] = []
    for year, grp in t.groupby(t["entry_date"].dt.year):
        pnls = grp["net_pnl"].dropna()
        wins = pnls[pnls > 0]
        rows.append(
            {
                "year": int(year),
                "trades": int(len(grp)),
                "net_pnl": float(pnls.sum()),
                "pf": profit_factor(pnls),
                "win_rate": 100.0 * len(wins) / len(pnls) if len(pnls) else float("nan"),
            }
        )
    return rows


def worst_reversals(trades: pd.DataFrame, n: int = 6) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()
    t = trades.copy()
    t["net_pnl"] = pd.to_numeric(t["net_pnl"], errors="coerce")
    for col in ("breakout_bps", "next_open_gap_pct", "open_to_exit_pct", "size_frac"):
        t[col] = pd.to_numeric(t[col], errors="coerce")
    return t.nsmallest(n, "net_pnl")


def print_summary(summary: dict[str, Any]) -> None:
    print("-" * 92)
    print(f"  Initial equity: ${fmt(summary['initial_equity'])}")
    print(f"  Final equity:   ${fmt(summary['final_equity'])}")
    print(f"  Net PnL:        ${fmt(summary['net_pnl'])}")
    print(f"  Total return:   {summary['return_pct']:.2f}%")
    print(f"  APR:            {summary['apr_pct']:.2f}%")
    print(f"  CAGR:           {summary['cagr_pct']:.2f}%")
    print(f"  Max DD:         {summary['max_drawdown_pct']:.2f}%")
    print(f"  Trades:         {summary['trades']}  |  exposure {summary['exposure_pct']:.2f}%")
    print(f"  Win rate:       {summary['win_rate_pct']:.1f}%")
    print(f"  Profit factor:  {fmt_pf(summary['profit_factor'])}")
    print(f"  Avg size:       {summary['avg_size_pct']:.2f}% of initial equity")


def print_yearly(trades: pd.DataFrame) -> None:
    rows = yearly_rows(trades)
    if not rows:
        return
    print("-" * 92)
    print("  YEARLY LEDGER")
    print(f"  {'year':>6} {'trades':>7} {'net pnl':>12} {'PF':>6} {'win':>7}")
    print("  " + "-" * 42)
    for row in rows:
        print(
            f"  {row['year']:>6} {row['trades']:>7} ${fmt(row['net_pnl']):>11} "
            f"{fmt_pf(row['pf']):>6} {row['win_rate']:>6.1f}%"
        )


def print_risk_notes(trades: pd.DataFrame) -> None:
    worst = worst_reversals(trades)
    if worst.empty:
        return
    print("-" * 92)
    print("  WORST BREAKOUT FOLLOW-THROUGH FAILURES")
    print("  Known failure mode: breakout confirms, then price reverses before exit.")
    print(f"  {'entry':10} {'hold':>4} {'breakout':>9} {'gap':>8} {'o2e':>8} {'size':>7} {'pnl':>11}")
    print("  " + "-" * 62)
    for _, row in worst.iterrows():
        print(
            f"  {str(row['entry_date'])[:10]:10} {int(row.get('hold_days', 0)):>4} {float(row['breakout_bps']):>8.0f}b "
            f"{float(row['next_open_gap_pct']):>7.2f}% {float(row['open_to_exit_pct']):>7.2f}% "
            f"{100.0 * float(row['size_frac']):>6.2f}% ${fmt(float(row['net_pnl'])):>10}"
        )


def print_recent_trades(trades: pd.DataFrame, n: int) -> None:
    if trades.empty or n <= 0:
        return
    recent = trades.tail(n)
    print("-" * 92)
    print(f"  RECENT TRADES - last {len(recent)}")
    print(f"  {'entry':10} {'size':>7} {'entry px':>12} {'exit px':>12} {'pnl':>11} {'equity':>12}")
    print("  " + "-" * 72)
    for _, row in recent.iterrows():
        print(
            f"  {str(row['entry_date'])[:10]:10} {float(row['size_frac']) * 100:>6.2f}% "
            f"{float(row['entry_px']):>12,.2f} {float(row['exit_px']):>12,.2f} "
            f"${fmt(float(row['net_pnl'])):>10} ${fmt(float(row['equity_after'])):>11}"
        )


def print_latest_signal(latest: dict[str, Any]) -> None:
    if not latest:
        return
    print("-" * 92)
    print(f"  Latest closed candle: {latest['signal_date'][:10]} close={latest['close']:,.2f}")
    prior = fmt(latest["prior_high"]) if latest["prior_high"] is not None else "n/a"
    print(f"  Prior high: {prior}")
    breakout = f"{latest['breakout_bps']:.0f}bps" if latest.get("breakout_bps") is not None else "n/a"
    print(f"  Breakout size: {breakout}")
    sma = fmt(latest["sma200"]) if latest["sma200"] is not None else "n/a"
    print(f"  SMA200: {sma}  |  bull regime: {'YES' if latest['bull'] else 'NO'}")
    if latest.get("trend_mode") not in {"bull_only", "sma200"}:
        print(f"  Active regime ({latest['trend_mode']}): {'YES' if latest.get('regime_on') else 'NO'}")
    print(f"  Signal for next UTC day: {'YES' if latest['signal'] else 'NO'}")
    if latest["signal"]:
        print(f"  Next fake position size: {latest['next_size_frac']:.2%} of initial equity")


def print_report(
    df: pd.DataFrame,
    trades: pd.DataFrame,
    summary: dict[str, Any],
    latest: dict[str, Any],
    sim_cfg: SimConfig,
    strat_cfg: StrategyConfig,
) -> None:
    print("=" * 92)
    print("  BTC BREAKOUT FAKE-MONEY SIMULATOR")
    print("=" * 92)
    source = f"{sim_cfg.source}:{sim_cfg.instrument}" if sim_cfg.source == "dukascopy" else sim_cfg.source
    print(f"  Source: {source}")
    print(f"  Data: {df.index[0].date()} -> {df.index[-1].date()} rows={len(df):,}")
    print(f"  Sim:  {sim_cfg.sim_start.date()} -> {df.index[-1].date()}")
    print(f"  Rule: close > prior {strat_cfg.lookback}d close high + {strat_cfg.buffer_bps:.0f}bps")
    if strat_cfg.max_breakout_bps is not None:
        print(f"  Filter: breakout size <= {strat_cfg.max_breakout_bps:.0f}bps")
    print(f"  Regime: {strat_cfg.trend_mode}")
    print(f"  Execution: signal at close, enter next open, exit after {strat_cfg.hold_days} trading day(s)")
    if strat_cfg.stop_loss_pct > 0.0 or strat_cfg.stop_atr_mult > 0.0:
        low_note = "daily low" if strat_cfg.stop_use_low else "close"
        parts = []
        if strat_cfg.stop_loss_pct > 0.0:
            parts.append(f"{strat_cfg.stop_loss_pct:.1%} pct")
        if strat_cfg.stop_atr_mult > 0.0:
            parts.append(f"{strat_cfg.stop_atr_mult:.1f}×N{strat_cfg.stop_atr_period}")
        print(f"  Stop: {' + '.join(parts)} ({low_note} trigger, tighter bound wins)")
    if strat_cfg.trail_atr > 0.0:
        print(f"  Trail: close below peak close - {strat_cfg.trail_atr:.1f}x ATR14")
    print(f"  Sizing: min({strat_cfg.max_alloc:.2f}x, {strat_cfg.vol_target:.2%} / 20d daily vol)")
    print(f"  Sizing base: {'current equity (compound)' if strat_cfg.compound else 'initial equity (no compound)'}")
    print(f"  Costs: {strat_cfg.fee_bps:.1f}bps per side")
    print_summary(summary)
    print_yearly(trades)
    print_risk_notes(trades)
    print_recent_trades(trades, sim_cfg.show_trades)
    print_latest_signal(latest)
    if sim_cfg.write_files:
        print("-" * 92)
        print(f"  Wrote: {sim_cfg.out_dir / 'paper_trades.csv'}")
        print(f"  Wrote: {sim_cfg.out_dir / 'paper_equity.csv'}")
        print(f"  Wrote: {sim_cfg.out_dir / 'paper_summary.json'}")
    print("=" * 92)


def run_single_source(
    sim_cfg: SimConfig,
    strat_cfg: StrategyConfig,
    *,
    print_full_report: bool,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any], dict[str, Any]]:
    df = add_indicators(fetch_source_data(sim_cfg), strat_cfg)
    trades, curve, summary = simulate_account(df, sim_cfg=sim_cfg, strat_cfg=strat_cfg)
    latest = latest_signal_report(df, strat_cfg)

    if sim_cfg.write_files:
        out_dir = sim_cfg.out_dir / sim_cfg.source if sim_cfg.source == "compare" else sim_cfg.out_dir
        write_outputs(out_dir, trades, curve, summary, latest)

    if print_full_report:
        print_report(df, trades, summary, latest, sim_cfg, strat_cfg)
    return df, trades, summary, latest


def print_source_comparison(rows: list[dict[str, Any]]) -> None:
    print("=" * 92)
    print("  BTC BREAKOUT SOURCE COMPARISON")
    print("=" * 92)
    print(f"  {'source':12} {'data':23} {'trades':>7} {'ret':>8} {'APR':>8} {'CAGR':>8} {'DD':>8} {'PF':>6} {'latest':>8}")
    print("  " + "-" * 87)
    for row in rows:
        summary = row["summary"]
        latest = row["latest"]
        data_span = f"{row['df'].index[0].date()}->{row['df'].index[-1].date()}"
        print(
            f"  {row['source']:12} {data_span:23} {summary['trades']:>7} "
            f"{summary['return_pct']:>7.2f}% {summary['apr_pct']:>7.2f}% "
            f"{summary['cagr_pct']:>7.2f}% "
            f"{summary['max_drawdown_pct']:>7.2f}% {fmt_pf(summary['profit_factor']):>6} "
            f"{'YES' if latest.get('signal') else 'NO':>8}"
        )
    print("=" * 92)


def compare_sources(sim_cfg: SimConfig, strat_cfg: StrategyConfig) -> None:
    y_cfg = SimConfig(**{**sim_cfg.__dict__, "source": "yfinance"})
    d_cfg = SimConfig(**{**sim_cfg.__dict__, "source": "dukascopy"})

    y_df = add_indicators(fetch_source_data(y_cfg), strat_cfg)
    d_df = add_indicators(fetch_source_data(d_cfg), strat_cfg)
    overlap_start = max(y_df.index.min(), d_df.index.min(), sim_cfg.sim_start)
    overlap_end = min(y_df.index.max(), d_df.index.max())
    if overlap_start >= overlap_end:
        raise RuntimeError("No overlapping yfinance/Dukascopy BTC date range")

    rows: list[dict[str, Any]] = []
    for cfg, df in ((y_cfg, y_df), (d_cfg, d_df)):
        run_cfg = SimConfig(**{**cfg.__dict__, "sim_start": overlap_start})
        df = df.loc[(df.index >= overlap_start) & (df.index <= overlap_end)]
        trades, curve, summary = simulate_account(df, sim_cfg=run_cfg, strat_cfg=strat_cfg)
        rows.append(
            {
                "source": cfg.source,
                "df": df,
                "summary": summary,
                "latest": latest_signal_report(df, strat_cfg),
            }
        )

    print_source_comparison(rows)


def fmt(v: float) -> str:
    return f"{v:,.2f}" if np.isfinite(v) else "n/a"


def fmt_pf(v: float) -> str:
    return f"{v:.2f}" if np.isfinite(v) else "n/a"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BTC breakout fake-money simulator")
    p.add_argument("--source", choices=("yfinance", "dukascopy", "compare"), default="dukascopy")
    p.add_argument("--instrument", default=DEFAULT_DUKASCOPY_INSTRUMENT, help="Dukascopy instrument alias, e.g. BTCUSD, XAUUSD, XAGUSD, XCUUSD, CL, BRENT, US500, NAS100")
    p.add_argument("--data-start", default="2018-01-01", help="Warmup data start")
    p.add_argument("--sim-start", default="2018-01-01", help="Fake account start date")
    p.add_argument("--end", default=None)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--lookback", type=int, default=15)
    p.add_argument("--buffer-bps", type=float, default=100.0)
    p.add_argument("--max-breakout-bps", type=float, default=225.0, help="Skip exhausted breakouts above this size; <=0 disables")
    p.add_argument("--trend-mode", choices=TREND_MODE_CHOICES, default="bull_only")
    p.add_argument("--hold-days", type=int, default=5, help="Exit at close after this many trading days")
    p.add_argument("--trail-atr", type=float, default=0.0, help="ATR14 trailing exit multiple; <=0 disables")
    p.add_argument("--fee-bps", type=float, default=10.0)
    p.add_argument("--vol-target", type=float, default=0.015)
    p.add_argument("--max-alloc", type=float, default=0.75)
    p.add_argument("--compound", action="store_true", help="Reinvest gains/losses into future position sizes")
    p.add_argument("--include-current", action="store_true", help="Include current UTC daily candle if yfinance returns it")
    p.add_argument("--cache-path", default="btc_breakout_clean/cache/btc_usd_yfinance_daily.csv", help="Local OHLC cache path")
    p.add_argument("--dukascopy-path", default=None, help="Local Dukascopy H1 CSV path; defaults to cache/<instrument>_dukascopy_h1.csv")
    p.add_argument("--refresh-cache", action="store_true", help="Force a fresh yfinance download")
    p.add_argument("--show-trades", type=int, default=8, help="Print the last N fake-money trades; 0 disables")
    p.add_argument("--write-files", action="store_true", help="Also write CSV/JSON files")
    p.add_argument("--out-dir", default="btc_breakout_clean/paper_btc_breakout")
    return p.parse_args()


def build_configs(args: argparse.Namespace) -> tuple[SimConfig, StrategyConfig]:
    instrument = args.instrument.upper()
    dukascopy_path = Path(args.dukascopy_path) if args.dukascopy_path else dukascopy_cache_path(instrument)
    sim_cfg = SimConfig(
        source=args.source,
        data_start=args.data_start,
        sim_start=pd.Timestamp(args.sim_start, tz="UTC"),
        end=args.end,
        equity=args.equity,
        include_current=args.include_current,
        cache_path=Path(args.cache_path),
        dukascopy_path=dukascopy_path,
        refresh_cache=args.refresh_cache,
        show_trades=args.show_trades,
        write_files=args.write_files,
        out_dir=Path(args.out_dir),
        instrument=instrument,
        skip_saturday_entry=default_skip_saturday_entry(args.source),
    )
    strat_cfg = StrategyConfig(
        lookback=args.lookback,
        buffer_bps=args.buffer_bps,
        max_breakout_bps=args.max_breakout_bps if args.max_breakout_bps > 0 else None,
        trend_mode=args.trend_mode,
        hold_days=args.hold_days,
        trail_atr=args.trail_atr if args.trail_atr > 0 else 0.0,
        fee_bps=args.fee_bps,
        vol_target=args.vol_target,
        max_alloc=args.max_alloc,
        compound=args.compound,
    )
    return sim_cfg, strat_cfg


def main() -> None:
    sim_cfg, strat_cfg = build_configs(parse_args())
    if sim_cfg.source == "compare":
        compare_sources(sim_cfg, strat_cfg)
        return
    run_single_source(sim_cfg, strat_cfg, print_full_report=True)


if __name__ == "__main__":
    main()
