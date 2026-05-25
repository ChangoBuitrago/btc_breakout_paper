"""
Open-source strategy templates on the same daily bars as Simona (long-only).

Proxies common Freqtrade community patterns and QuantConnect LEAN samples:
  - RSI mean-reversion long (oversold buy)
  - MACD cross long
  - Bollinger lower-band mean reversion (typical Freqtrade BB)
  - Bollinger upper breakout (momentum variant)
  - SMA fast/slow cross (LEAN MovingAverageCross-style)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from btc_breakout_paper_sim import SimConfig, cagr, max_drawdown, profit_factor


@dataclass(frozen=True)
class OSSStrategyConfig:
    strategy_id: str
    label: str
    source: str  # freqtrade | quantconnect
    fee_bps: float = 10.0
    vol_target: float = 0.015
    max_alloc: float = 0.75
    max_hold: int = 20
    compound: bool = True


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    up = delta.clip(lower=0.0)
    down = (-delta).clip(lower=0.0)
    avg_up = up.rolling(period).mean()
    avg_down = down.rolling(period).mean()
    rs = avg_up / avg_down.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def add_oss_indicators(df: pd.DataFrame, cfg: OSSStrategyConfig) -> pd.DataFrame:
    out = df.copy()
    out["vol20"] = out["close"].pct_change().rolling(20).std()
    out["entry_signal"] = False
    out["exit_signal"] = False

    if cfg.strategy_id == "ft_rsi_long":
        out["rsi"] = _rsi(out["close"], 14)
        out["entry_signal"] = out["rsi"] < 30.0
        out["exit_signal"] = out["rsi"] > 70.0

    elif cfg.strategy_id == "ft_macd_long":
        ema12 = out["close"].ewm(span=12, adjust=False).mean()
        ema26 = out["close"].ewm(span=26, adjust=False).mean()
        out["macd"] = ema12 - ema26
        out["macd_sig"] = out["macd"].ewm(span=9, adjust=False).mean()
        cross_up = (out["macd"] > out["macd_sig"]) & (out["macd"].shift(1) <= out["macd_sig"].shift(1))
        cross_dn = (out["macd"] < out["macd_sig"]) & (out["macd"].shift(1) >= out["macd_sig"].shift(1))
        out["entry_signal"] = cross_up
        out["exit_signal"] = cross_dn

    elif cfg.strategy_id == "ft_bb_mean_rev":
        mid = out["close"].rolling(20).mean()
        std = out["close"].rolling(20).std()
        lower = mid - 2.0 * std
        out["entry_signal"] = out["close"] < lower
        out["exit_signal"] = out["close"] > mid

    elif cfg.strategy_id == "ft_bb_breakout":
        mid = out["close"].rolling(20).mean()
        std = out["close"].rolling(20).std()
        upper = mid + 2.0 * std
        out["entry_signal"] = out["close"] > upper
        out["exit_signal"] = out["close"] < mid

    elif cfg.strategy_id == "lean_sma_cross":
        fast = out["close"].rolling(20).mean()
        slow = out["close"].rolling(50).mean()
        cross_up = (fast > slow) & (fast.shift(1) <= slow.shift(1))
        cross_dn = (fast < slow) & (fast.shift(1) >= slow.shift(1))
        out["entry_signal"] = cross_up
        out["exit_signal"] = cross_dn

    else:
        raise ValueError(f"Unknown strategy_id: {cfg.strategy_id}")

    out["entry_signal"] = out["entry_signal"].fillna(False)
    out["exit_signal"] = out["exit_signal"].fillna(False)
    return out


def simulate_oss_account(
    df: pd.DataFrame,
    *,
    sim_cfg: SimConfig,
    oss_cfg: OSSStrategyConfig,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    fee = oss_cfg.fee_bps / 10_000.0
    equity = float(sim_cfg.equity)
    fixed_sizing_equity = float(sim_cfg.equity)
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []

    in_pos = False
    entry_i = 0
    entry_px = 0.0
    entry_notional = 0.0
    entry_fee = 0.0
    qty = 0.0
    size_frac = 0.0
    equity_before_entry = equity
    pending_signal_i: int | None = None

    for i in range(1, len(df)):
        entry_date = df.index[i]
        if entry_date < sim_cfg.sim_start:
            continue

        day_pnl = 0.0
        action = "HOLD" if in_pos else "NO_SIGNAL"
        sig_i = i - 1
        entry_day = entry_date.normalize()

        if not in_pos and bool(df["entry_signal"].iloc[sig_i]):
            pending_signal_i = sig_i

        if not in_pos and pending_signal_i is not None:
            if entry_day in sim_cfg.blocked_entry_dates:
                pending_signal_i = None
            else:
                rv = float(df["vol20"].iloc[pending_signal_i])
                todays_size_frac = (
                    min(oss_cfg.max_alloc, oss_cfg.vol_target / rv) if np.isfinite(rv) and rv > 0 else 0.0
                )
                if todays_size_frac > 0.0:
                    sizing_base = equity if oss_cfg.compound else fixed_sizing_equity
                    entry_px = float(df["open"].iloc[i])
                    entry_notional = sizing_base * todays_size_frac
                    qty = entry_notional / entry_px
                    entry_fee = entry_notional * fee
                    equity_before_entry = equity
                    equity -= entry_fee
                    day_pnl -= entry_fee
                    in_pos = True
                    entry_i = i
                    size_frac = todays_size_frac
                    action = "ENTRY"
                    pending_signal_i = None

        if in_pos:
            hold_bars = i - entry_i + 1
            exit_hit = bool(df["exit_signal"].iloc[i]) and hold_bars >= 1
            time_exit = hold_bars >= oss_cfg.max_hold
            if exit_hit or time_exit:
                exit_px = float(df["close"].iloc[i])
                exit_notional = qty * exit_px
                exit_fee = exit_notional * fee
                gross_pnl = exit_notional - entry_notional
                net_pnl = gross_pnl - entry_fee - exit_fee
                day_pnl += net_pnl
                equity += net_pnl
                action = "EXIT"
                trades.append(
                    {
                        "entry_date": df.index[entry_i].isoformat(),
                        "exit_date": entry_date.isoformat(),
                        "hold_days": hold_bars,
                        "exit_reason": "signal" if exit_hit else "max_hold",
                        "strategy": oss_cfg.strategy_id,
                        "entry_px": entry_px,
                        "exit_px": exit_px,
                        "open_to_exit_pct": 100.0 * (exit_px / entry_px - 1.0),
                        "qty": qty,
                        "size_frac": size_frac,
                        "entry_notional": entry_notional,
                        "net_pnl": net_pnl,
                        "equity_after": equity,
                    }
                )
                in_pos = False

        curve.append(
            {
                "date": entry_date.isoformat(),
                "equity": equity,
                "daily_pnl": day_pnl,
                "action": action,
                "in_position": in_pos,
            }
        )

    if in_pos and curve:
        last_i = len(df) - 1
        exit_px = float(df["close"].iloc[last_i])
        exit_notional = qty * exit_px
        exit_fee = exit_notional * fee
        net_pnl = exit_notional - entry_notional - entry_fee - exit_fee
        equity += net_pnl
        trades.append(
            {
                "entry_date": df.index[entry_i].isoformat(),
                "exit_date": df.index[last_i].isoformat(),
                "hold_days": last_i - entry_i + 1,
                "exit_reason": "force_exit",
                "strategy": oss_cfg.strategy_id,
                "entry_px": entry_px,
                "exit_px": exit_px,
                "net_pnl": net_pnl,
                "equity_after": equity,
            }
        )
        curve[-1]["equity"] = equity
        curve[-1]["action"] = "FORCE_EXIT"

    trades_df = pd.DataFrame(trades)
    curve_df = pd.DataFrame(curve)
    if curve_df.empty:
        summary = {
            "trades": 0,
            "return_pct": 0.0,
            "max_drawdown_pct": float("nan"),
            "profit_factor": float("nan"),
        }
    else:
        eq = curve_df.set_index(pd.to_datetime(curve_df["date"], utc=True))["equity"].astype(float)
        ret = equity / float(sim_cfg.equity) - 1.0
        pnls = pd.to_numeric(trades_df["net_pnl"], errors="coerce") if not trades_df.empty else pd.Series(dtype=float)
        summary = {
            "trades": int(len(trades_df)),
            "return_pct": 100.0 * ret,
            "cagr_pct": 100.0 * cagr(ret, eq.index[0], eq.index[-1]) if len(eq) > 1 else 0.0,
            "max_drawdown_pct": 100.0 * max_drawdown(eq),
            "profit_factor": float(profit_factor(pnls)) if len(pnls) else float("nan"),
        }
    return trades_df, curve_df, summary


OSS_STRATEGIES: dict[str, OSSStrategyConfig] = {
    "ft_rsi_long": OSSStrategyConfig(
        strategy_id="ft_rsi_long",
        label="Freqtrade-style RSI long (buy <30, sell >70)",
        source="freqtrade",
        max_hold=15,
    ),
    "ft_macd_long": OSSStrategyConfig(
        strategy_id="ft_macd_long",
        label="Freqtrade-style MACD cross long",
        source="freqtrade",
        max_hold=25,
    ),
    "ft_bb_mean_rev": OSSStrategyConfig(
        strategy_id="ft_bb_mean_rev",
        label="Freqtrade-style BB mean reversion (buy lower band)",
        source="freqtrade",
        max_hold=12,
    ),
    "ft_bb_breakout": OSSStrategyConfig(
        strategy_id="ft_bb_breakout",
        label="Freqtrade-style BB upper breakout",
        source="freqtrade",
        max_hold=12,
    ),
    "lean_sma_cross": OSSStrategyConfig(
        strategy_id="lean_sma_cross",
        label="QuantConnect LEAN SMA 20/50 cross long",
        source="quantconnect",
        max_hold=40,
    ),
}
