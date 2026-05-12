#!/usr/bin/env python3
"""
btc_strategy_probe.py

Dedicated BTCUSD probe for the two candidates surfaced by pivot_regime_screen:

  - RAM: Regime-Adaptive Mean Reversion
  - DPB: Daily Position Breakout

The pivot screen was intentionally coarse, so this probe prints enough diagnostics
to tell whether the BTC pass is real or just an artifact:

  - pivot-compatible variants (same long-only semantics as pivot_regime_screen)
  - directional variants (more trading-realistic long/short semantics)
  - IS / OOS / OOS ex-2024
  - yearly attribution
  - top-3 trade fragility
  - fee stress
  - rolling walk-forward, fixed default parameters

Usage:
  python3 last/btc_strategy_probe.py
  python3 last/btc_strategy_probe.py --fee-bps 20
  python3 last/btc_strategy_probe.py --start 2018-01-01 --end 2026-05-08
"""

from __future__ import annotations

import argparse
import warnings
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

IS_T0 = pd.Timestamp("2018-01-01", tz="UTC")
IS_T1 = pd.Timestamp("2022-12-31 23:59:59", tz="UTC")
OOS_T0 = pd.Timestamp("2023-01-01", tz="UTC")
OOS_T1 = pd.Timestamp("2026-05-08 23:59:59", tz="UTC")


@dataclass(frozen=True)
class StrategySpec:
    name: str
    kind: str
    signal_fn: Callable[[pd.DataFrame], pd.Series]


def fetch_btc(start: str, end: str) -> pd.DataFrame:
    raw = yf.download("BTC-USD", start=start, end=end, progress=False, auto_adjust=False)
    df = raw if not isinstance(raw, tuple) else raw[0]
    if df.empty:
        raise RuntimeError("BTC-USD download returned empty data")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).strip().lower() for c in df.columns]
    if "close" not in df.columns:
        raise RuntimeError(f"BTC-USD data has no close column: {list(df.columns)}")
    df.index = pd.to_datetime(df.index, utc=True)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["ret"] = df["close"].pct_change()
    return df.dropna(subset=["close", "ret"])


def _z_price(df: pd.DataFrame, win: int) -> pd.Series:
    ma = df["close"].rolling(win).mean()
    sd = df["close"].rolling(win).std(ddof=1)
    return (df["close"] - ma) / sd


def _low_vol_regime(df: pd.DataFrame, rvol_win: int = 720, med_win: int = 730) -> pd.Series:
    # Keep defaults aligned with pivot_regime_screen; daily BTC has enough history.
    rvol = df["ret"].rolling(rvol_win).std() * np.sqrt(252)
    med = rvol.rolling(med_win).median()
    return rvol < med


def ram_pivot_signal(df: pd.DataFrame, z_th: float = 2.1, vol_win: int = 60) -> pd.Series:
    """Pivot-compatible RAM: long-only after either extreme, gated by low-vol regime."""
    z = _z_price(df, vol_win)
    regime = _low_vol_regime(df)
    sig = ((z < -z_th) & regime) | ((z > z_th) & regime)
    return sig.astype(float)


def ram_directional_signal(df: pd.DataFrame, z_th: float = 2.1, vol_win: int = 60) -> pd.Series:
    """Mean-reversion RAM: long below -Z, short above +Z, gated by low-vol regime."""
    z = _z_price(df, vol_win)
    regime = _low_vol_regime(df)
    sig = pd.Series(0.0, index=df.index)
    sig[(z < -z_th) & regime] = 1.0
    sig[(z > z_th) & regime] = -1.0
    return sig


def dpb_pivot_signal(df: pd.DataFrame, lookback: int = 20) -> pd.Series:
    """Pivot-compatible DPB: long-only after either upside/downside breakout."""
    high = df["close"].rolling(lookback).max().shift(1)
    low = df["close"].rolling(lookback).min().shift(1)
    sig = (df["close"] > high) | (df["close"] < low)
    return sig.astype(float)


def dpb_directional_signal(df: pd.DataFrame, lookback: int = 20) -> pd.Series:
    """Directional DPB: long upside breakout, short downside breakout."""
    high = df["close"].rolling(lookback).max().shift(1)
    low = df["close"].rolling(lookback).min().shift(1)
    sig = pd.Series(0.0, index=df.index)
    sig[df["close"] > high] = 1.0
    sig[df["close"] < low] = -1.0
    return sig


def build_trades(df: pd.DataFrame, signal: pd.Series, fee_bps: float) -> pd.DataFrame:
    """One-bar holding model: signal at close t, PnL over next daily return."""
    sig = signal.reindex(df.index).fillna(0.0).astype(float)
    ret_fol = df["ret"].shift(-1)
    traded = sig != 0.0
    pnl = sig * ret_fol - traded.astype(float) * (fee_bps / 10_000.0)
    out = pd.DataFrame(
        {
            "close": df["close"],
            "signal": sig,
            "ret_fol": ret_fol,
            "pnl": pnl,
            "traded": traded,
        },
        index=df.index,
    )
    return out.dropna(subset=["pnl"])


def slice_df(df: pd.DataFrame, mode: str) -> pd.DataFrame:
    if mode == "is":
        return df.loc[(df.index >= IS_T0) & (df.index <= IS_T1)]
    if mode == "oos":
        return df.loc[(df.index >= OOS_T0) & (df.index <= OOS_T1)]
    if mode == "oos_ex_2024":
        x = slice_df(df, "oos")
        return x.loc[x.index.year != 2024]
    if mode == "full":
        return df.loc[(df.index >= IS_T0) & (df.index <= OOS_T1)]
    raise ValueError(mode)


def stats(tr: pd.DataFrame) -> dict[str, float]:
    x = tr[tr["traded"]].copy()
    pnl = pd.to_numeric(x["pnl"], errors="coerce").dropna()
    n = int(len(pnl))
    if n == 0:
        return {
            "n": 0,
            "net": 0.0,
            "pf": float("nan"),
            "wr": float("nan"),
            "avg_bps": float("nan"),
            "top3": 0.0,
        }
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    gross_w = float(wins.sum())
    gross_l = float(losses.sum())
    net = float(pnl.sum())
    pf = gross_w / abs(gross_l) if gross_l < 0 else float("nan")
    top3 = abs(float(pnl.nlargest(3).sum()) / net) * 100.0 if net != 0 else 0.0
    return {
        "n": n,
        "net": net,
        "pf": pf,
        "wr": 100.0 * len(wins) / n,
        "avg_bps": float(pnl.mean() * 10_000.0),
        "top3": top3,
    }


def fmt_pf(v: float) -> str:
    return f"{v:.2f}" if np.isfinite(v) else "n/a"


def print_stats_row(label: str, tr: pd.DataFrame) -> None:
    s = stats(tr)
    print(
        f"  {label:14} {s['n']:>5}  {s['net'] * 100:>9.2f}%  "
        f"{fmt_pf(s['pf']):>6}  {s['wr']:>6.1f}%  {s['avg_bps']:>8.1f}  {s['top3']:>8.1f}%"
    )


def print_strategy_report(name: str, tr: pd.DataFrame, fee_bps: float) -> None:
    print("\n" + "=" * 78)
    print(f"  {name}  |  fee={fee_bps:.1f} bps / trade")
    print("=" * 78)
    print(f"  {'slice':14} {'n':>5}  {'net':>10}  {'PF':>6}  {'WR':>7}  {'avg bp':>8}  {'top3':>9}")
    print("-" * 78)
    print_stats_row("IS", slice_df(tr, "is"))
    print_stats_row("OOS", slice_df(tr, "oos"))
    print_stats_row("OOS ex-2024", slice_df(tr, "oos_ex_2024"))

    print("\n  Yearly OOS attribution")
    print(f"  {'year':>6} {'n':>5} {'net':>10} {'PF':>6}")
    print("-" * 34)
    oos = slice_df(tr, "oos")
    for year, grp in oos.groupby(oos.index.year):
        s = stats(grp)
        print(f"  {year:>6} {s['n']:>5} {s['net'] * 100:>9.2f}% {fmt_pf(s['pf']):>6}")


def print_fee_stress(name: str, df: pd.DataFrame, sig_fn: Callable[[pd.DataFrame], pd.Series], fee_bps: float) -> None:
    print(f"\n  Fee stress — {name} (OOS ex-2024)")
    print(f"  {'fee bps':>8} {'n':>5} {'net':>10} {'PF':>6} {'top3':>8}")
    print("-" * 46)
    for mult in (0.5, 1.0, 2.0, 4.0):
        fee = fee_bps * mult
        tr = build_trades(df, sig_fn(df), fee)
        s = stats(slice_df(tr, "oos_ex_2024"))
        print(f"  {fee:>8.1f} {s['n']:>5} {s['net'] * 100:>9.2f}% {fmt_pf(s['pf']):>6} {s['top3']:>7.1f}%")


def month_add(ts: pd.Timestamp, months: int) -> pd.Timestamp:
    return ts + pd.DateOffset(months=months)


def print_walk_forward(name: str, tr: pd.DataFrame) -> None:
    print(f"\n  Walk-forward fixed-params — {name} (IS=2y -> OOS=6m, step=6m)")
    print(f"  {'fold':>4} {'IS':21} {'OOS':21} {'n':>4} {'net':>9} {'PF':>6}")
    print("-" * 78)
    start = IS_T0
    fold = 1
    nets: list[float] = []
    pfs: list[float] = []
    ok = 0
    while True:
        is0 = start
        is1 = month_add(is0, 24)
        oos0 = is1
        oos1 = month_add(oos0, 6)
        if oos1 > OOS_T1:
            break
        oos = tr.loc[(tr.index >= oos0) & (tr.index < oos1)]
        s = stats(oos)
        nets.append(s["net"])
        if np.isfinite(s["pf"]):
            pfs.append(s["pf"])
        if s["net"] > 0:
            ok += 1
        print(
            f"  {fold:>4} {is0.date()}->{is1.date()}  {oos0.date()}->{oos1.date()}  "
            f"{s['n']:>4} {s['net'] * 100:>8.2f}% {fmt_pf(s['pf']):>6}"
        )
        start = month_add(start, 6)
        fold += 1
    total = sum(nets)
    denom = max(fold - 1, 1)
    print("-" * 78)
    print(f"  folds={fold - 1} profitable={ok}/{fold - 1} ({100 * ok / denom:.0f}%)  agg_net={total * 100:.2f}%  mean_pf={np.mean(pfs) if pfs else float('nan'):.2f}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BTCUSD RAM/DPB probe")
    p.add_argument("--start", default="2018-01-01")
    p.add_argument("--end", default="2026-05-08")
    p.add_argument("--fee-bps", type=float, default=10.0, help="Per-trade fee/slippage in basis points")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    df = fetch_btc(args.start, args.end)
    specs = [
        StrategySpec("RAM_pivot_compat", "RAM", ram_pivot_signal),
        StrategySpec("DPB_pivot_compat", "DPB", dpb_pivot_signal),
        StrategySpec("RAM_directional", "RAM", ram_directional_signal),
        StrategySpec("DPB_directional", "DPB", dpb_directional_signal),
    ]

    print("=" * 78)
    print("  BTCUSD STRATEGY PROBE — RAM / DPB")
    print("=" * 78)
    print(f"  Data: {df.index[0].date()} -> {df.index[-1].date()}  rows={len(df):,}")
    print(f"  IS: {IS_T0.date()} -> {IS_T1.date()}   OOS: {OOS_T0.date()} -> {OOS_T1.date()}")
    print(f"  Fee/slippage: {args.fee_bps:.1f} bps per trade")
    print("  Note: *_pivot_compat matches the coarse screen semantics; *_directional is more realistic.")

    for spec in specs:
        tr = build_trades(df, spec.signal_fn(df), args.fee_bps)
        print_strategy_report(spec.name, tr, args.fee_bps)
        print_fee_stress(spec.name, df, spec.signal_fn, args.fee_bps)
        print_walk_forward(spec.name, tr)

    print("\n" + "=" * 78)
    print("  Readout rule: require OOS ex-2024 PF > 1.30, top3 < 80%, and walk-forward > 60% profitable folds.")
    print("=" * 78)


if __name__ == "__main__":
    main()
