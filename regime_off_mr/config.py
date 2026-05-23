"""Regime-off research config — independent of the breakout live book."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MechanismId = Literal["M0_bear_breakout", "M1_stretch_mr", "M2_prior_low_tag"]

DATA_START = "2018-01-01"
SIM_START = "2018-01-01"
EX_2024 = "2024-01-01"
INITIAL_EQUITY = 10_000.0

# Phase-1 metals only (Dukascopy sleeves).
RESEARCH_SYMBOLS: tuple[str, ...] = ("XAUUSD", "BRENT", "XAGUSD")

# Mirror breakout regime style per symbol (research reference only).
REGIME_STYLE: dict[str, Literal["sma200_95", "sma200"]] = {
    "XAUUSD": "sma200_95",
    "BRENT": "sma200_95",
    "XAGUSD": "sma200",
}

FEE_BPS: dict[str, float] = {
    "XAUUSD": 2.0,
    "XAGUSD": 2.0,
    "BRENT": 5.0,
}

# Discovery gates (solo Algo 2 — stricter than book promotion).
MIN_PF_FULL = 1.15
MIN_PF_EX_2024 = 1.0
MIN_TRADES_FULL = 15
MIN_TRADES_EX_2024 = 3
MAX_DD_PCT = -15.0
MAX_TOP_TRADE_PNL_SHARE = 0.50


@dataclass(frozen=True)
class SleeveParams:
    symbol: str
    mechanism: MechanismId
    lookback: int = 30
    buffer_bps: float = 75.0
    max_breakout_bps: float | None = 225.0
    stretch_min_bps: float = 150.0
    tag_bps: float = 75.0
    hold_days: int = 6
    fee_bps: float = 2.0
    vol_target: float = 0.015
    max_alloc: float = 0.50
    skip_saturday_entry: bool = True

    def label(self) -> str:
        m = self.mechanism
        if m == "M0_bear_breakout":
            return f"{m} lb{self.lookback} buf{self.buffer_bps:.0f} h{self.hold_days}"
        if m == "M1_stretch_mr":
            return f"{m} stretch{self.stretch_min_bps:.0f} h{self.hold_days}"
        return f"{m} lb{self.lookback} tag{self.tag_bps:.0f} h{self.hold_days}"


def default_sleeve(symbol: str, mechanism: MechanismId) -> SleeveParams:
    fee = FEE_BPS.get(symbol.upper(), 5.0)
    if mechanism == "M0_bear_breakout":
        return SleeveParams(
            symbol=symbol.upper(),
            mechanism=mechanism,
            lookback=30,
            buffer_bps=75.0,
            hold_days=7,
            fee_bps=fee,
        )
    if mechanism == "M1_stretch_mr":
        return SleeveParams(
            symbol=symbol.upper(),
            mechanism=mechanism,
            stretch_min_bps=150.0,
            hold_days=6,
            fee_bps=fee,
        )
    return SleeveParams(
        symbol=symbol.upper(),
        mechanism=mechanism,
        lookback=30,
        tag_bps=75.0,
        hold_days=5,
        fee_bps=fee,
    )


MECHANISMS: tuple[MechanismId, ...] = (
    "M0_bear_breakout",
    "M1_stretch_mr",
    "M2_prior_low_tag",
)
