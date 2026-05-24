#!/usr/bin/env python3
"""
Turtle Trading System 1 reference (long-only): 20-day high entry, 10-day low exit,
2×N (20-day true-range ATR) stop, 1% equity risk per unit. Enter at next open.

Used for apples-to-apples benchmark vs live breakout book (same data, fees, period).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from btc_breakout_paper_sim import SimConfig, cagr, max_drawdown, profit_factor


@dataclass(frozen=True)
class TurtleConfig:
    entry_lookback: int = 20
    exit_lookback: int = 10
    atr_period: int = 20
    stop_n_mult: float = 2.0
    risk_pct: float = 0.01
    max_alloc: float = 1.0
    fee_bps: float = 10.0
    compound: bool = True
    long_only: bool = True


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    hl = df["high"] - df["low"]
    hc = (df["high"] - prev_close).abs()
    lc = (df["low"] - prev_close).abs()
    return pd.concat([hl, hc, lc], axis=1).max(axis=1)


def add_turtle_indicators(df: pd.DataFrame, cfg: TurtleConfig) -> pd.DataFrame:
    out = df.copy()
    out["n_atr"] = true_range(out).rolling(cfg.atr_period).mean()
    out["entry_level"] = out["high"].rolling(cfg.entry_lookback).max().shift(1)
    out["exit_level"] = out["low"].rolling(cfg.exit_lookback).min().shift(1)
    # Breakout on prior bar close above prior channel (signal evaluated at close)
    out["signal"] = out["close"] > out["entry_level"]
    out["signal"] = out["signal"].fillna(False)
    return out


def turtle_size_frac(entry_px: float, n_atr: float, equity: float, cfg: TurtleConfig) -> float:
    """Fraction of equity notional so a 2N adverse move loses ~risk_pct of equity."""
    if not np.isfinite(n_atr) or n_atr <= 0 or entry_px <= 0:
        return 0.0
    stop_dist = cfg.stop_n_mult * n_atr
    notional = cfg.risk_pct * equity * entry_px / stop_dist
    return min(cfg.max_alloc, notional / equity)


def simulate_turtle_account(
    df: pd.DataFrame,
    *,
    sim_cfg: SimConfig,
    turtle_cfg: TurtleConfig | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    cfg = turtle_cfg or TurtleConfig()
    fee = cfg.fee_bps / 10_000.0
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
    stop_px = 0.0
    pending_signal_i: int | None = None

    for i in range(1, len(df)):
        entry_date = df.index[i]
        if entry_date < sim_cfg.sim_start:
            continue

        day_pnl = 0.0
        action = "HOLD" if in_pos else "NO_SIGNAL"
        todays_signal_i = i - 1
        todays_signal = bool(df["signal"].iloc[todays_signal_i])
        entry_day = entry_date.normalize()

        if not in_pos and todays_signal:
            pending_signal_i = todays_signal_i

        if not in_pos and pending_signal_i is not None:
            veto_block = entry_day in sim_cfg.blocked_entry_dates
            if not veto_block:
                n_atr = float(df["n_atr"].iloc[pending_signal_i])
                sizing_base = equity if cfg.compound else fixed_sizing_equity
                todays_size_frac = turtle_size_frac(
                    float(df["open"].iloc[i]), n_atr, sizing_base, cfg
                )
                if todays_size_frac > 0.0:
                    entry_px = float(df["open"].iloc[i])
                    entry_notional = sizing_base * todays_size_frac
                    qty = entry_notional / entry_px
                    entry_fee = entry_notional * fee
                    equity_before_entry = equity
                    equity -= entry_fee
                    day_pnl -= entry_fee
                    stop_px = entry_px - cfg.stop_n_mult * n_atr
                    size_frac = todays_size_frac
                    in_pos = True
                    entry_i = i
                    action = "ENTRY"
                    pending_signal_i = None

        if in_pos:
            cur_close = float(df["close"].iloc[i])
            cur_low = float(df["low"].iloc[i])
            exit_level = float(df["exit_level"].iloc[i])
            stop_hit = cur_low <= stop_px
            channel_exit = np.isfinite(exit_level) and cur_close < exit_level
            target_exit = stop_hit or channel_exit

            if target_exit:
                exit_px = stop_px if stop_hit else cur_close
                exit_notional = qty * exit_px
                exit_fee = exit_notional * fee
                gross_pnl = exit_notional - entry_notional
                net_pnl = gross_pnl - entry_fee - exit_fee
                day_pnl += net_pnl
                equity += net_pnl
                hold_bars = i - entry_i + 1
                action = ("ENTRY_EXIT" if hold_bars == 1 else "EXIT") + ("_STOP" if stop_hit else "_CH")
                trades.append(
                    {
                        "entry_date": df.index[entry_i].isoformat(),
                        "exit_date": entry_date.isoformat(),
                        "hold_days": hold_bars,
                        "exit_reason": "stop_loss" if stop_hit else "channel_exit",
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

    trades_df = pd.DataFrame(trades)
    curve_df = pd.DataFrame(curve)
    if curve_df.empty:
        summary = {
            "trades": 0,
            "initial_equity": float(sim_cfg.equity),
            "final_equity": equity,
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
            "initial_equity": float(sim_cfg.equity),
            "final_equity": equity,
            "return_pct": 100.0 * ret,
            "cagr_pct": 100.0 * cagr(ret, eq.index[0], eq.index[-1]) if len(eq) > 1 else 0.0,
            "max_drawdown_pct": 100.0 * max_drawdown(eq),
            "profit_factor": float(profit_factor(pnls)) if len(pnls) else float("nan"),
        }
    return trades_df, curve_df, summary
