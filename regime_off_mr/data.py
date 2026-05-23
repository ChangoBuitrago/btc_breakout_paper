"""Load daily OHLC from shared Dukascopy H1 cache (read-only; no breakout strategy imports)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "btc_breakout_clean" / "cache"

DUKASCOPY_INSTRUMENTS = {
    "XAUUSD": "INSTRUMENT_FX_METALS_XAU_USD",
    "XAGUSD": "INSTRUMENT_FX_METALS_XAG_USD",
    "BRENT": "INSTRUMENT_CMD_ENERGY_E_BRENT",
}


def cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol.upper()}_dukascopy_h1.csv"


def normalize_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).strip().lower() for c in df.columns]
    df.index = pd.to_datetime(df.index, utc=True)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    for col in ("open", "high", "low", "close"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"])


def resample_h1_to_daily(h1: pd.DataFrame) -> pd.DataFrame:
    return normalize_ohlc(
        h1.resample("D").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    )


def load_daily(symbol: str, start: str = "2018-01-01") -> pd.DataFrame:
    path = cache_path(symbol)
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. Populate via btc_breakout_clean daily run or download first."
        )
    h1 = normalize_ohlc(pd.read_csv(path, index_col=0, parse_dates=True))
    daily = resample_h1_to_daily(h1)
    daily = daily.loc[daily.index < pd.Timestamp.utcnow().normalize()]
    return daily.loc[daily.index >= pd.Timestamp(start, tz="UTC").normalize()]
