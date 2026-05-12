#!/usr/bin/env python3
"""
BTC breakout simple probe.

Frozen thesis:
  If BTC closes above its prior N-day close high by X bps,
  enter next open and exit same-day close.

Default tradable version:
  lookback=20, buffer=300bps, hold=1, sizing=min(1x, 1% / 20d daily vol)
"""

from __future__ import annotations

import argparse
import warnings
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")


@dataclass(frozen=True)
class SimResult:
    label: str
    lookback: int
    buffer_bps: float
    daily: pd.DataFrame
    trades: pd.DataFrame


def fetch_btc(start: str, end: str) -> pd.DataFrame:
    raw = yf.download("BTC-USD", start=start, end=end, progress=False, auto_adjust=False)
    df = raw if not isinstance(raw, tuple) else raw[0]
    if df.empty:
        raise RuntimeError("BTC-USD download returned empty data")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).lower().strip() for c in df.columns]
    required = {"open", "high", "low", "close"}
    if not required.issubset(df.columns):
        raise RuntimeError(f"Missing OHLC columns: {list(df.columns)}")
    df.index = pd.to_datetime(df.index, utc=True)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"])


def parse_csv_ints(s: str) -> list[int]:
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def parse_csv_floats(s: str) -> list[float]:
    return [float(x.strip()) for x in s.split(",") if x.strip()]


def signal_series(df: pd.DataFrame, lookback: int, buffer_bps: float, ma_filter: int) -> pd.Series:
    prior_high = df["close"].rolling(lookback).max().shift(1)
    sig = df["close"] > prior_high * (1.0 + buffer_bps / 10_000.0)
    if ma_filter > 0:
        ma = df["close"].rolling(ma_filter).mean()
        sig &= df["close"] > ma
    return sig.fillna(False)


def simulate(
    df: pd.DataFrame,
    *,
    lookback: int,
    buffer_bps: float,
    fee_bps: float,
    sizing: str,
    fixed_alloc: float,
    vol_target: float,
    max_alloc: float,
    ma_filter: int,
) -> SimResult:
    sig = signal_series(df, lookback, buffer_bps, ma_filter)
    vol20 = df["close"].pct_change().rolling(20).std()
    fee = fee_bps / 10_000.0

    rets = pd.Series(0.0, index=df.index)
    exposed = pd.Series(False, index=df.index)
    sizes = pd.Series(0.0, index=df.index)
    trades: list[dict[str, Any]] = []

    for i in range(1, len(df)):
        signal_i = i - 1
        if not bool(sig.iloc[signal_i]):
            continue

        if sizing == "fixed":
            size = min(fixed_alloc, max_alloc)
        elif sizing == "vol":
            rv = float(vol20.iloc[signal_i])
            size = min(max_alloc, vol_target / rv) if np.isfinite(rv) and rv > 0 else 0.0
        else:
            raise ValueError(f"Unknown sizing: {sizing}")

        if size <= 0:
            continue

        entry_px = float(df["open"].iloc[i])
        exit_px = float(df["close"].iloc[i])
        gross = exit_px / entry_px - 1.0
        net = size * gross - 2.0 * fee * size

        rets.iloc[i] = net
        exposed.iloc[i] = True
        sizes.iloc[i] = size
        trades.append(
            {
                "signal_date": df.index[signal_i],
                "entry_date": df.index[i],
                "exit_date": df.index[i],
                "entry_px": entry_px,
                "exit_px": exit_px,
                "size": size,
                "gross_ret": gross,
                "net_ret": net,
            }
        )

    label = f"{sizing}"
    if sizing == "fixed":
        label += f" {fixed_alloc:.2f}x"
    else:
        label += f" target={vol_target:.2%} cap={max_alloc:.2f}x"
    if ma_filter > 0:
        label += f" ma{ma_filter}"

    daily = pd.DataFrame({"strategy_ret": rets, "exposed": exposed, "size": sizes}, index=df.index)
    return SimResult(label, lookback, buffer_bps, daily, pd.DataFrame(trades))


def date_mask(index: pd.DatetimeIndex, mode: str, args: argparse.Namespace) -> pd.Series:
    is0 = pd.Timestamp(args.is_start, tz="UTC")
    is1 = pd.Timestamp(args.is_end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    oos0 = pd.Timestamp(args.oos_start, tz="UTC")
    oos1 = (
        pd.Timestamp(args.oos_end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        if args.oos_end
        else pd.Timestamp.today(tz="UTC").normalize() + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    )

    if mode == "is":
        m = (index >= is0) & (index <= is1)
    elif mode == "validation":
        m = (index >= pd.Timestamp("2021-01-01", tz="UTC")) & (index < pd.Timestamp("2024-01-01", tz="UTC"))
    elif mode == "stress_2024":
        m = index.year == 2024
    elif mode == "forward":
        m = (index >= pd.Timestamp("2025-01-01", tz="UTC")) & (index <= oos1)
    elif mode == "oos":
        m = (index >= oos0) & (index <= oos1)
    elif mode == "oos_ex_2024":
        m = (index >= oos0) & (index <= oos1) & (index.year != 2024)
    else:
        raise ValueError(mode)
    return pd.Series(m, index=index)


def trades_for_period(trades: pd.DataFrame, mode: str, args: argparse.Namespace) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    m = date_mask(pd.DatetimeIndex(t["entry_date"]), mode, args).to_numpy()
    return t.loc[m].copy()


def trade_stats(trades: pd.DataFrame) -> dict[str, float]:
    if trades.empty:
        return {"n": 0, "pf": np.nan, "wr": np.nan, "top3": 0.0, "avg": np.nan}

    r = pd.to_numeric(trades["net_ret"], errors="coerce").dropna()
    if r.empty:
        return {"n": 0, "pf": np.nan, "wr": np.nan, "top3": 0.0, "avg": np.nan}

    wins = r[r > 0]
    losses = r[r < 0]
    net = float(r.sum())
    gross_w = float(wins.sum())
    gross_l = float(losses.sum())
    pf = gross_w / abs(gross_l) if gross_l < 0 else np.nan
    top3 = abs(float(r.nlargest(3).sum()) / net) * 100.0 if net != 0 else 0.0

    return {
        "n": int(len(r)),
        "pf": pf,
        "wr": 100.0 * len(wins) / len(r),
        "top3": top3,
        "avg": float(r.mean()),
    }


def period_stats(res: SimResult, mode: str, args: argparse.Namespace) -> dict[str, float]:
    d = res.daily.loc[date_mask(res.daily.index, mode, args)]
    if d.empty:
        return {"ret": 0.0, "dd": np.nan, "exp": 0.0, "avg_size": 0.0, **trade_stats(pd.DataFrame())}

    eq = (1.0 + d["strategy_ret"]).cumprod()
    dd = eq / eq.cummax() - 1.0
    ts = trade_stats(trades_for_period(res.trades, mode, args))
    avg_size = float(d.loc[d["exposed"], "size"].mean()) if bool(d["exposed"].any()) else 0.0

    return {
        "ret": float(eq.iloc[-1] - 1.0),
        "dd": float(dd.min()),
        "exp": 100.0 * float(d["exposed"].mean()),
        "avg_size": avg_size,
        **ts,
    }


def selected_buy_hold_equity(df: pd.DataFrame, mask: pd.Series) -> pd.Series:
    selected = pd.Series(mask, index=df.index).astype(bool)
    daily_ret = df["close"].pct_change().fillna(0.0)
    starts = selected & ~selected.shift(1, fill_value=False)
    selected_ret = daily_ret.loc[selected].copy()
    selected_ret.loc[starts.loc[selected]] = 0.0
    return (1.0 + selected_ret).cumprod()


def fmt_pf(v: float) -> str:
    return f"{v:.2f}" if np.isfinite(v) else "n/a"


def print_stats_table(title: str, res: SimResult, args: argparse.Namespace) -> None:
    print("\n" + "=" * 100)
    print(f"  {title}: {res.label}, lb={res.lookback}, buffer={res.buffer_bps:.0f}bps")
    print("=" * 100)
    print(f"  {'slice':14} {'n':>5} {'ret':>9} {'PF':>6} {'WR':>7} {'top3':>7} {'DD':>8} {'exp':>6} {'avgSz':>7}")
    print("-" * 100)
    for mode in ("is", "validation", "stress_2024", "forward", "oos", "oos_ex_2024"):
        s = period_stats(res, mode, args)
        print(
            f"  {mode:14} {int(s['n']):>5} {s['ret'] * 100:>8.2f}% {fmt_pf(s['pf']):>6} "
            f"{s['wr']:>6.1f}% {s['top3']:>6.1f}% {s['dd'] * 100:>7.2f}% "
            f"{s['exp']:>5.1f}% {s['avg_size']:>6.2f}x"
        )


def print_buy_hold(df: pd.DataFrame, args: argparse.Namespace) -> None:
    print("\n  Buy-and-hold benchmark")
    print(f"  {'slice':14} {'ret':>9} {'DD':>8}")
    print("-" * 36)
    for mode in ("is", "validation", "stress_2024", "forward", "oos", "oos_ex_2024"):
        mask = date_mask(df.index, mode, args)
        eq = selected_buy_hold_equity(df, mask)
        if len(eq) < 2:
            continue
        dd = eq / eq.cummax() - 1.0
        print(f"  {mode:14} {(eq.iloc[-1] - 1.0) * 100:>8.2f}% {dd.min() * 100:>7.2f}%")


def print_yearly(res: SimResult, args: argparse.Namespace) -> None:
    print("\n  Yearly OOS")
    print(f"  {'year':>6} {'n':>5} {'ret':>9} {'PF':>6} {'DD':>8} {'avgSz':>7}")
    print("-" * 54)
    d = res.daily.loc[date_mask(res.daily.index, "oos", args)]
    for year, grp in d.groupby(d.index.year):
        eq = (1.0 + grp["strategy_ret"]).cumprod()
        dd = eq / eq.cummax() - 1.0
        tr = res.trades.copy()
        if not tr.empty:
            tr["entry_date"] = pd.to_datetime(tr["entry_date"], utc=True)
            tr = tr.loc[tr["entry_date"].dt.year == year]
        ts = trade_stats(tr)
        avg_size = float(grp.loc[grp["exposed"], "size"].mean()) if bool(grp["exposed"].any()) else 0.0
        print(
            f"  {year:>6} {ts['n']:>5} {(eq.iloc[-1] - 1.0) * 100:>8.2f}% "
            f"{fmt_pf(ts['pf']):>6} {dd.min() * 100:>7.2f}% {avg_size:>6.2f}x"
        )


def print_fee_stress(df: pd.DataFrame, args: argparse.Namespace) -> None:
    print("\n" + "=" * 100)
    print("  FEE STRESS - candidate vol sizing, OOS ex-2024")
    print("=" * 100)
    print(f"  {'fee/side':>8} {'n':>5} {'ret':>9} {'PF':>6} {'top3':>7} {'DD':>8}")
    print("-" * 100)
    for fee in parse_csv_floats(args.fee_stress):
        res = simulate(
            df,
            lookback=args.lookback,
            buffer_bps=args.buffer_bps,
            fee_bps=fee,
            sizing="vol",
            fixed_alloc=1.0,
            vol_target=args.vol_target,
            max_alloc=args.max_alloc,
            ma_filter=args.ma_filter,
        )
        s = period_stats(res, "oos_ex_2024", args)
        print(
            f"  {fee:>7.1f}b {int(s['n']):>5} {s['ret'] * 100:>8.2f}% "
            f"{fmt_pf(s['pf']):>6} {s['top3']:>6.1f}% {s['dd'] * 100:>7.2f}%"
        )


def print_neighborhood(df: pd.DataFrame, args: argparse.Namespace) -> None:
    print("\n" + "=" * 100)
    print("  NEIGHBORHOOD - vol sizing, OOS ex-2024")
    print("=" * 100)
    print(f"  {'lb':>4} {'buf':>5} {'n':>5} {'ret':>9} {'PF':>6} {'top3':>7} {'DD':>8} {'pass':>6}")
    print("-" * 100)

    rows = []
    for lb in parse_csv_ints(args.lookbacks):
        for buf in parse_csv_floats(args.buffers):
            res = simulate(
                df,
                lookback=lb,
                buffer_bps=buf,
                fee_bps=args.fee_bps,
                sizing="vol",
                fixed_alloc=1.0,
                vol_target=args.vol_target,
                max_alloc=args.max_alloc,
                ma_filter=args.ma_filter,
            )
            s = period_stats(res, "oos_ex_2024", args)
            ok = s["pf"] >= 1.30 and s["top3"] <= 80.0 and s["n"] >= args.min_trades
            rows.append((ok, lb, buf, s))

    rows.sort(key=lambda x: (x[0], x[3]["pf"], x[3]["ret"]), reverse=True)
    for ok, lb, buf, s in rows:
        print(
            f"  {lb:>4} {buf:>5.0f} {int(s['n']):>5} {s['ret'] * 100:>8.2f}% "
            f"{fmt_pf(s['pf']):>6} {s['top3']:>6.1f}% {s['dd'] * 100:>7.2f}% "
            f"{'PASS' if ok else 'FAIL':>6}"
        )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Simplified BTC breakout validation probe")
    p.add_argument("--start", default="2014-09-17")
    p.add_argument("--end", default=None)
    p.add_argument("--is-start", default="2015-01-01")
    p.add_argument("--is-end", default="2020-12-31")
    p.add_argument("--oos-start", default="2021-01-01")
    p.add_argument("--oos-end", default=None)

    p.add_argument("--lookback", type=int, default=20)
    p.add_argument("--buffer-bps", type=float, default=300.0)
    p.add_argument("--fee-bps", type=float, default=10.0)
    p.add_argument("--vol-target", type=float, default=0.01, help="Daily vol target, e.g. 0.01 = 1%")
    p.add_argument("--max-alloc", type=float, default=1.0)
    p.add_argument("--ma-filter", type=int, default=0, help="Optional trend filter, e.g. 200; 0 disables")

    p.add_argument("--lookbacks", default="15,20,25,30")
    p.add_argument("--buffers", default="200,300,400,500")
    p.add_argument("--fee-stress", default="5,10,20,30,50")
    p.add_argument("--min-trades", type=int, default=30)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    end = args.end or pd.Timestamp.today(tz="UTC").strftime("%Y-%m-%d")
    df = fetch_btc(args.start, end)

    fixed = simulate(
        df,
        lookback=args.lookback,
        buffer_bps=args.buffer_bps,
        fee_bps=args.fee_bps,
        sizing="fixed",
        fixed_alloc=1.0,
        vol_target=args.vol_target,
        max_alloc=1.0,
        ma_filter=args.ma_filter,
    )
    vol = simulate(
        df,
        lookback=args.lookback,
        buffer_bps=args.buffer_bps,
        fee_bps=args.fee_bps,
        sizing="vol",
        fixed_alloc=1.0,
        vol_target=args.vol_target,
        max_alloc=args.max_alloc,
        ma_filter=args.ma_filter,
    )

    print("=" * 100)
    print("  BTC BREAKOUT SIMPLE PROBE")
    print("=" * 100)
    print(f"  Data: {df.index[0].date()} -> {df.index[-1].date()} rows={len(df):,}")
    print(f"  Rule: close > prior {args.lookback}d close high + {args.buffer_bps:.0f}bps")
    print("  Execution: signal at close, enter next open, exit same-day close")
    print(f"  Cost: {args.fee_bps:.1f}bps per side")
    print(f"  Tradable sizing: min({args.max_alloc:.2f}x, {args.vol_target:.2%} / 20d daily vol)")
    if args.ma_filter > 0:
        print(f"  Optional filter active: close > {args.ma_filter}d MA")

    print_buy_hold(df, args)
    print_stats_table("EDGE VIEW", fixed, args)
    print_stats_table("TRADABLE VIEW", vol, args)
    print_yearly(vol, args)
    print_fee_stress(df, args)
    print_neighborhood(df, args)
    print("\n" + "=" * 100)


if __name__ == "__main__":
    main()
