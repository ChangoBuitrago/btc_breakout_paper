"""Long-only daily simulator for regime-off mechanisms."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from regime_off_mr.config import INITIAL_EQUITY, SIM_START, SleeveParams
from regime_off_mr.signals import add_signals


def profit_factor(pnls: pd.Series) -> float:
    wins = float(pnls[pnls > 0].sum())
    losses = float(pnls[pnls < 0].sum())
    return wins / abs(losses) if losses < 0 else float("nan")


def max_drawdown(equity: pd.Series) -> float:
    dd = equity / equity.cummax() - 1.0
    return float(dd.min()) if len(dd) else float("nan")


def simulate(df: pd.DataFrame, params: SleeveParams, *, equity: float = INITIAL_EQUITY) -> tuple[pd.DataFrame, dict[str, Any]]:
    frame = add_signals(df, params)
    fee = params.fee_bps / 10_000.0
    sim_start = pd.Timestamp(SIM_START, tz="UTC")

    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    in_pos = False
    entry_i = 0
    signal_i_at_entry = 0
    entry_px = 0.0
    entry_notional = 0.0
    qty = 0.0
    size_frac = 0.0
    equity_before_entry = equity
    pending_signal_i: int | None = None

    for i in range(1, len(frame)):
        bar_date = frame.index[i]
        if bar_date < sim_start:
            continue

        todays_signal_i = i - 1
        todays_signal = bool(frame["signal"].iloc[todays_signal_i])
        entry_day = bar_date.normalize()

        if not in_pos and todays_signal:
            pending_signal_i = todays_signal_i

        if not in_pos and pending_signal_i is not None:
            saturday_block = params.skip_saturday_entry and entry_day.dayofweek == 5
            if not saturday_block:
                rv = float(frame["vol20"].iloc[pending_signal_i])
                sf = (
                    min(params.max_alloc, params.vol_target / rv)
                    if np.isfinite(rv) and rv > 0
                    else 0.0
                )
                if sf > 0.0:
                    entry_px = float(frame["open"].iloc[i])
                    equity_before_entry = equity
                    entry_notional = equity * sf
                    qty = entry_notional / entry_px
                    entry_fee = entry_notional * fee
                    equity -= entry_fee
                    in_pos = True
                    entry_i = i
                    signal_i_at_entry = pending_signal_i
                    size_frac = sf
                pending_signal_i = None
            else:
                pending_signal_i = None

        if in_pos:
            hold_bars = i - entry_i + 1
            if hold_bars >= params.hold_days:
                exit_px = float(frame["close"].iloc[i])
                exit_notional = qty * exit_px
                exit_fee = exit_notional * fee
                entry_fee = entry_notional * fee
                gross = exit_notional - entry_notional
                net = gross - entry_fee - exit_fee
                equity += gross - exit_fee

                trades.append(
                    {
                        "signal_date": frame.index[signal_i_at_entry].isoformat(),
                        "entry_date": frame.index[entry_i].isoformat(),
                        "exit_date": bar_date.isoformat(),
                        "hold_days": hold_bars,
                        "mechanism": params.mechanism,
                        "stretch_bps": float(frame["stretch_bps"].iloc[signal_i_at_entry])
                        if params.mechanism == "M1_stretch_mr"
                        else None,
                        "breakout_bps": float(frame["breakout_bps"].iloc[signal_i_at_entry])
                        if params.mechanism == "M0_bear_breakout"
                        else None,
                        "entry_px": entry_px,
                        "exit_px": exit_px,
                        "net_pnl": net,
                        "size_frac": size_frac,
                        "open_to_exit_pct": 100.0 * (exit_px / entry_px - 1.0),
                    }
                )
                in_pos = False

        curve.append({"date": bar_date.isoformat(), "equity": equity})

    if in_pos:
        last_i = len(frame) - 1
        exit_px = float(frame["close"].iloc[last_i])
        exit_notional = qty * exit_px
        exit_fee = exit_notional * fee
        entry_fee = entry_notional * fee
        gross = exit_notional - entry_notional
        net = gross - entry_fee - exit_fee
        equity += gross - exit_fee
        hold_bars = last_i - entry_i + 1
        trades.append(
            {
                "signal_date": frame.index[signal_i_at_entry].isoformat(),
                "entry_date": frame.index[entry_i].isoformat(),
                "exit_date": frame.index[last_i].isoformat(),
                "hold_days": hold_bars,
                "mechanism": params.mechanism,
                "exit_reason": "force_exit",
                "entry_px": entry_px,
                "exit_px": exit_px,
                "net_pnl": net,
                "size_frac": size_frac,
                "open_to_exit_pct": 100.0 * (exit_px / entry_px - 1.0),
            }
        )
        if curve:
            curve[-1]["equity"] = equity

    trades_df = pd.DataFrame(trades)
    curve_df = pd.DataFrame(curve)
    pnls = pd.to_numeric(trades_df["net_pnl"], errors="coerce") if not trades_df.empty else pd.Series(dtype=float)
    eq_series = (
        curve_df.set_index(pd.to_datetime(curve_df["date"], utc=True))["equity"].astype(float)
        if not curve_df.empty
        else pd.Series([equity], index=[frame.index[-1]])
    )
    ret = equity / INITIAL_EQUITY - 1.0
    summary = {
        "symbol": params.symbol,
        "mechanism": params.mechanism,
        "params": params.label(),
        "trades": int(len(pnls)),
        "final_equity": float(equity),
        "return_pct": 100.0 * ret,
        "max_drawdown_pct": 100.0 * max_drawdown(eq_series),
        "profit_factor": float(profit_factor(pnls)) if len(pnls) else float("nan"),
        "win_rate_pct": 100.0 * float((pnls > 0).mean()) if len(pnls) else float("nan"),
    }
    return trades_df, summary
