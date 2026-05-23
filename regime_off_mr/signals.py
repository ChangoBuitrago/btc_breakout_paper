"""Regime-off entry signals (M0–M2)."""

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
    out["sma200"] = out["close"].rolling(200).mean()
    return out


def add_signals(df: pd.DataFrame, params: SleeveParams) -> pd.DataFrame:
    out = add_base_indicators(df)
    floor = regime_floor(out, params.symbol)
    out["regime_off"] = out["close"] < floor
    out["stretch_bps"] = np.where(
        out["regime_off"] & (out["close"] > 0),
        10_000.0 * (floor / out["close"] - 1.0),
        np.nan,
    )

    lb = int(params.lookback)
    out["prior_high"] = out["close"].rolling(lb).max().shift(1)
    out["prior_low"] = out["close"].rolling(lb).min().shift(1)
    out["breakout_bps"] = 10_000.0 * (out["close"] / out["prior_high"] - 1.0)

    m = params.mechanism
    sig = pd.Series(False, index=out.index)

    if m == "M0_bear_breakout":
        raw = out["close"] > out["prior_high"] * (1.0 + params.buffer_bps / 10_000.0)
        if params.max_breakout_bps is not None:
            raw &= out["breakout_bps"] <= params.max_breakout_bps
        sig = raw & out["regime_off"]

    elif m == "M1_stretch_mr":
        sig = out["regime_off"] & (out["stretch_bps"] >= params.stretch_min_bps)

    elif m == "M2_prior_low_tag":
        near_low = out["close"] <= out["prior_low"] * (1.0 + params.tag_bps / 10_000.0)
        sig = out["regime_off"] & near_low

    out["signal"] = sig.fillna(False)
    return out
