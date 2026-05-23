"""Regime-off research config — independent of the breakout live book."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MechanismId = Literal[
    "M1_stretch_mr",
    "M2_prior_low_tag",
    "M3_stretch_bounce",
]

DATA_START = "2018-01-01"
SIM_START = "2018-01-01"
EX_2024 = "2024-01-01"
INITIAL_EQUITY = 10_000.0

RESEARCH_SYMBOLS: tuple[str, ...] = ("XAUUSD", "BRENT", "XAGUSD")

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

# Discovery gates (v2 — fewer false positives from tiny ex-2024 samples).
MIN_PF_FULL = 1.15
MIN_PF_EX_2024 = 1.05
MIN_TRADES_FULL = 12
MIN_TRADES_EX_2024 = 5
MAX_TRADES_FULL = 55
MAX_DD_PCT = -15.0
MAX_TOP_TRADE_PNL_SHARE = 0.45
MAX_EX_PF_IF_FEW_TRADES = 8.0  # ex-PF > this with < 8 ex trades => reject


@dataclass(frozen=True)
class SleeveParams:
    symbol: str
    mechanism: MechanismId
    lookback: int = 30
    stretch_min_bps: float = 200.0
    stretch_max_bps: float = 600.0
    tag_bps: float = 75.0
    min_ret5_pct: float = -6.0
    min_regime_off_days: int = 3
    require_bounce: bool = True
    hold_days: int = 6
    hold_max: int = 10
    exit_at_regime_on: bool = True
    exit_at_sma50: bool = True
    cooldown_days: int = 5
    fee_bps: float = 2.0
    vol_target: float = 0.015
    max_alloc: float = 0.40
    skip_saturday_entry: bool = True

    def label(self) -> str:
        m = self.mechanism
        base = (
            f"{m} str{self.stretch_min_bps:.0f}-{self.stretch_max_bps:.0f}"
            if m != "M2_prior_low_tag"
            else f"{m} lb{self.lookback} tag{self.tag_bps:.0f}"
        )
        flags = []
        if self.require_bounce:
            flags.append("bounce")
        if self.exit_at_regime_on:
            flags.append("xRegOn")
        if self.exit_at_sma50:
            flags.append("xSMA50")
        flag_s = ("+" + ",".join(flags)) if flags else ""
        return (
            f"{base} off{self.min_regime_off_days}d cd{self.cooldown_days} "
            f"h{self.hold_days}-{self.hold_max}{flag_s}"
        )


def default_sleeve(symbol: str, mechanism: MechanismId) -> SleeveParams:
    fee = FEE_BPS.get(symbol.upper(), 5.0)
    if mechanism == "M2_prior_low_tag":
        return SleeveParams(
            symbol=symbol.upper(),
            mechanism=mechanism,
            lookback=30,
            tag_bps=75.0,
            stretch_min_bps=0.0,
            stretch_max_bps=9999.0,
            hold_days=5,
            hold_max=8,
            cooldown_days=7,
            fee_bps=fee,
        )
    # M1 / M3 stretch family
    return SleeveParams(
        symbol=symbol.upper(),
        mechanism=mechanism,
        stretch_min_bps=200.0,
        stretch_max_bps=550.0,
        min_ret5_pct=-5.0,
        min_regime_off_days=3,
        hold_days=6,
        hold_max=10,
        cooldown_days=5,
        fee_bps=fee,
    )


MECHANISMS: tuple[MechanismId, ...] = (
    "M3_stretch_bounce",
    "M1_stretch_mr",
    "M2_prior_low_tag",
)
