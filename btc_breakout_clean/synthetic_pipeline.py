#!/usr/bin/env python3
"""
Synthetic institutional daily bars from Binance intraday (research).

Session closes at 21:00 UTC (5pm EST). Optional weekend return dampening.

Does not modify live data paths.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from btc_breakout_paper_sim import normalize_ohlc

INSTITUTIONAL_CLOSE_UTC_HOUR = 21
BINANCE_SYMBOL = {
    "BTCUSD": "BTCUSDT",
    "ETHUSDT": "ETHUSDT",
    "BNBUSDT": "BNBUSDT",
    "SOLUSDT": "SOLUSDT",
    "DOGEUSDT": "DOGEUSDT",
}


def h1_to_institutional_daily(
    h1: pd.DataFrame,
    *,
    weekend_dampen: float = 1.0,
) -> pd.DataFrame:
    """Aggregate H1 Binance bars to 21:00 UTC institutional dailies."""
    df = normalize_ohlc(h1.copy())
    if df.empty:
        return df

    offset = pd.Timedelta(hours=INSTITUTIONAL_CLOSE_UTC_HOUR)
    agg = (
        df.resample("1D", origin="epoch", offset=offset)
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["close"])
    )

    if weekend_dampen >= 1.0 - 1e-12:
        return normalize_ohlc(agg)

    rets = agg["close"].pct_change()
    dow = agg.index.dayofweek
    damped = rets.copy()
    damped.loc[dow.isin([5, 6])] = damped.loc[dow.isin([5, 6])] * weekend_dampen
    closes = [float(agg["close"].iloc[0])]
    for r in damped.iloc[1:]:
        if pd.isna(r):
            closes.append(closes[-1])
        else:
            closes.append(closes[-1] * (1.0 + float(r)))

    out = agg.copy()
    out["close"] = closes
    out["open"] = out["close"].shift(1).fillna(out["close"].iloc[0])
    range_up = (agg["high"] - np.maximum(agg["open"], agg["close"])).clip(lower=0.0)
    range_dn = (np.minimum(agg["open"], agg["close"]) - agg["low"]).clip(lower=0.0)
    body_top = np.maximum(out["open"].to_numpy(), out["close"].to_numpy())
    body_bot = np.minimum(out["open"].to_numpy(), out["close"].to_numpy())
    out["high"] = body_top + range_up.to_numpy()
    out["low"] = body_bot - range_dn.to_numpy()
    return normalize_ohlc(out)
