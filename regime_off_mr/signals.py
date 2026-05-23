"""Regime-off entry signals — stretch / low-tag with stabilization filters."""

from __future__ import annotations

import numpy as np
import pandas as pd

from regime_off_mr.config import REGIME_STYLE, SleeveParams


def regime_floor(df: pd.DataFrame, symbol: str) -> pd.Series:
    sma200 = df["sma200"]
    style = REGIME_STYLE.get(symbol.upper(), "sma200")
    if style == "sma200_95":
        return sma200 * 0.95
    return sma200


def add_base_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ret"] = out["close"].pct_change()
    out["vol20"] = out["ret"].rolling(20).std()
    out["sma50"] = out["close"].rolling(50).mean()
    out["sma200"] = out["close"].rolling(200).mean()
    out["ret5_pct"] = 100.0 * (out["close"] / out["close"].shift(5) - 1.0)
    out["bounce"] = out["close"] > out["close"].shift(1)
    return out


def regime_off_streak(regime_off: pd.Series) -> pd.Series:
    """Consecutive regime-off days ending at this bar."""
    streak = pd.Series(0, index=regime_off.index, dtype=int)
    run = 0
    for i, off in enumerate(regime_off.astype(bool)):
        run = run + 1 if off else 0
        streak.iloc[i] = run
    return streak


def add_signals(df: pd.DataFrame, params: SleeveParams) -> pd.DataFrame:
    out = add_base_indicators(df)
    floor = regime_floor(out, params.symbol)
    out["regime_floor"] = floor
    out["regime_off"] = out["close"] < floor
    out["regime_on"] = ~out["regime_off"]
    out["regime_off_run"] = regime_off_streak(out["regime_off"])
    out["stretch_bps"] = np.where(
        out["regime_off"] & (out["close"] > 0),
        10_000.0 * (floor / out["close"] - 1.0),
        np.nan,
    )

    lb = int(params.lookback)
    out["prior_low"] = out["close"].rolling(lb).min().shift(1)

    stretch_ok = (
        out["regime_off"]
        & (out["stretch_bps"] >= params.stretch_min_bps)
        & (out["stretch_bps"] <= params.stretch_max_bps)
    )
    not_crashing = out["ret5_pct"] >= params.min_ret5_pct
    off_mature = out["regime_off_run"] >= params.min_regime_off_days
    bounce_ok = out["bounce"] if params.require_bounce else pd.Series(True, index=out.index)

    m = params.mechanism
    sig = pd.Series(False, index=out.index)

    if m in ("M1_stretch_mr", "M3_stretch_bounce"):
        sig = stretch_ok & not_crashing & off_mature & bounce_ok
        if m == "M3_stretch_bounce":
            # Stronger: also need close still below SMA50 (true dip, not extended rally in bear)
            sig &= out["close"] < out["sma50"]

    elif m == "M2_prior_low_tag":
        near_low = out["close"] <= out["prior_low"] * (1.0 + params.tag_bps / 10_000.0)
        sig = out["regime_off"] & near_low & off_mature & bounce_ok & not_crashing

    out["signal"] = sig.fillna(False)
    return out
