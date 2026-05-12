#!/usr/bin/env python3
"""
btc_breakout_position.py

BTC long-only breakout *position* probe.

Entry hypothesis from btc_dpb_refine.py:
  Enter long after BTC closes above its prior N-day high by a buffer.
  Default: N=20, buffer=300 bps (3%).

This script tests exits/risk management, not more entry mining:
  - max holding period
  - close below moving average
  - close below trailing stop from post-entry max close

Execution model:
  - Signal on close[t]
  - Enter next day's open
  - Exit at close when an exit rule triggers
  - Return subtracts round-trip fee/slippage: 2 * fee_bps

Usage:
  python3 last/btc_breakout_position.py
  python3 last/btc_breakout_position.py --max-history
  python3 last/btc_breakout_position.py --fee-bps 20
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

IS_T0 = pd.Timestamp("2015-01-01", tz="UTC")
IS_T1 = pd.Timestamp("2020-12-31 23:59:59", tz="UTC")
OOS_T0 = pd.Timestamp("2021-01-01", tz="UTC")
OOS_T1 = pd.Timestamp.today(tz="UTC").normalize() + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)


@dataclass(frozen=True)
class ExitSpec:
    max_hold: int
    ma_exit: int  # 0 = disabled
    trail_pct: float  # 0 = disabled; e.g. 0.20 = 20% trailing close stop

    @property
    def label(self) -> str:
        ma = f"MA{self.ma_exit}" if self.ma_exit else "noMA"
        tr = f"TR{int(self.trail_pct * 100)}" if self.trail_pct else "noTR"
        return f"H{self.max_hold}_{ma}_{tr}"


def fetch_btc(start: str, end: str) -> pd.DataFrame:
    raw = yf.download("BTC-USD", start=start, end=end, progress=False, auto_adjust=False)
    df = raw if not isinstance(raw, tuple) else raw[0]
    if df.empty:
        raise RuntimeError("BTC-USD download returned empty data")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [str(c).strip().lower() for c in df.columns]
    required = {"open", "high", "low", "close"}
    if not required.issubset(df.columns):
        raise RuntimeError(f"BTC-USD missing OHLC columns: {list(df.columns)}")
    df.index = pd.to_datetime(df.index, utc=True)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ret"] = df["close"].pct_change()
    return df.dropna(subset=["open", "high", "low", "close"])


def entry_signal(df: pd.DataFrame, lookback: int, buffer_bps: float) -> pd.Series:
    prior_high = df["close"].rolling(lookback).max().shift(1)
    return df["close"] > prior_high * (1.0 + buffer_bps / 10_000.0)


def simulate_position(
    df: pd.DataFrame,
    *,
    lookback: int,
    buffer_bps: float,
    fee_bps: float,
    spec: ExitSpec,
) -> pd.DataFrame:
    d = df.copy()
    d["entry_sig"] = entry_signal(d, lookback, buffer_bps)
    ma = d["close"].rolling(spec.ma_exit).mean() if spec.ma_exit else pd.Series(np.nan, index=d.index)

    trades: list[dict[str, Any]] = []
    in_pos = False
    entry_i = -1
    entry_date: pd.Timestamp | None = None
    entry_px = 0.0
    max_close = 0.0

    # Start at 1 so we can use previous close signal and current open fill.
    for i in range(1, len(d)):
        date = d.index[i]
        close = float(d["close"].iloc[i])
        open_px = float(d["open"].iloc[i])

        if not in_pos:
            if bool(d["entry_sig"].iloc[i - 1]):
                in_pos = True
                entry_i = i
                entry_date = date
                entry_px = open_px
                max_close = close
            continue

        max_close = max(max_close, close)
        hold_days = i - entry_i
        reasons: list[str] = []
        if hold_days >= spec.max_hold:
            reasons.append("max_hold")
        if spec.ma_exit and np.isfinite(ma.iloc[i]) and close < float(ma.iloc[i]):
            reasons.append(f"close_below_ma{spec.ma_exit}")
        if spec.trail_pct and close < max_close * (1.0 - spec.trail_pct):
            reasons.append(f"trail_{int(spec.trail_pct * 100)}")

        is_last = i == len(d) - 1
        if reasons or is_last:
            gross_ret = close / entry_px - 1.0
            net_ret = gross_ret - 2.0 * fee_bps / 10_000.0
            trades.append(
                {
                    "entry_date": entry_date,
                    "exit_date": date,
                    "entry_px": entry_px,
                    "exit_px": close,
                    "hold_days": hold_days,
                    "gross_ret": gross_ret,
                    "net_ret": net_ret,
                    "reason": "+".join(reasons) if reasons else "end",
                }
            )
            in_pos = False

    return pd.DataFrame(trades)


def trades_between(tr: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    if tr.empty:
        return tr.copy()
    x = tr.copy()
    x["entry_date"] = pd.to_datetime(x["entry_date"], utc=True)
    return x.loc[(x["entry_date"] >= start) & (x["entry_date"] <= end)].copy()


def slice_trades(tr: pd.DataFrame, mode: str) -> pd.DataFrame:
    if mode == "is":
        return trades_between(tr, IS_T0, IS_T1)
    if mode == "oos":
        return trades_between(tr, OOS_T0, OOS_T1)
    if mode == "oos_ex_2024":
        x = slice_trades(tr, "oos")
        return x.loc[pd.to_datetime(x["entry_date"], utc=True).dt.year != 2024].copy() if not x.empty else x
    raise ValueError(mode)


def stats(tr: pd.DataFrame) -> dict[str, float]:
    if tr.empty:
        return {"n": 0, "net": 0.0, "pf": float("nan"), "wr": float("nan"), "top3": 0.0, "avg": float("nan"), "med_hold": float("nan")}
    r = pd.to_numeric(tr["net_ret"], errors="coerce").dropna()
    n = int(len(r))
    if n == 0:
        return {"n": 0, "net": 0.0, "pf": float("nan"), "wr": float("nan"), "top3": 0.0, "avg": float("nan"), "med_hold": float("nan")}
    wins = r[r > 0]
    losses = r[r < 0]
    gross_w = float(wins.sum())
    gross_l = float(losses.sum())
    net = float(r.sum())
    pf = gross_w / abs(gross_l) if gross_l < 0 else float("nan")
    top3 = abs(float(r.nlargest(3).sum()) / net) * 100.0 if net != 0 else 0.0
    return {
        "n": n,
        "net": net,
        "pf": pf,
        "wr": 100.0 * len(wins) / n,
        "top3": top3,
        "avg": float(r.mean()),
        "med_hold": float(pd.to_numeric(tr["hold_days"], errors="coerce").median()),
    }


def fmt_pf(v: float) -> str:
    return f"{v:.2f}" if np.isfinite(v) else "n/a"


def month_add(ts: pd.Timestamp, months: int) -> pd.Timestamp:
    return ts + pd.DateOffset(months=months)


def walk_forward(tr: pd.DataFrame) -> dict[str, float]:
    start = IS_T0
    folds = 0
    ok = 0
    nets: list[float] = []
    pfs: list[float] = []
    while True:
        is1 = month_add(start, 24)
        oos0 = is1
        oos1 = month_add(oos0, 6)
        if oos1 > OOS_T1:
            break
        oos = trades_between(tr, oos0, oos1 - pd.Timedelta(seconds=1))
        s = stats(oos)
        folds += 1
        nets.append(s["net"])
        ok += int(s["net"] > 0)
        if np.isfinite(s["pf"]):
            pfs.append(s["pf"])
        start = month_add(start, 6)
    return {
        "folds": folds,
        "ok": ok,
        "ok_pct": 100.0 * ok / folds if folds else 0.0,
        "net": float(sum(nets)),
        "mean_pf": float(np.mean(pfs)) if pfs else float("nan"),
    }


def buy_hold_stats(df: pd.DataFrame) -> None:
    print("\n  Buy-and-hold benchmark")
    print(f"  {'slice':12} {'ret':>10} {'maxDD':>10}")
    print("-" * 36)
    for label, start, end in (
        ("IS", IS_T0, IS_T1),
        ("OOS", OOS_T0, OOS_T1),
    ):
        x = df.loc[(df.index >= start) & (df.index <= end)]
        if len(x) < 2:
            continue
        eq = x["close"] / float(x["close"].iloc[0])
        dd = eq / eq.cummax() - 1.0
        print(f"  {label:12} {((eq.iloc[-1] - 1) * 100):>9.2f}% {(dd.min() * 100):>9.2f}%")


def score_variant(df: pd.DataFrame, spec: ExitSpec, lookback: int, buffer_bps: float, fee_bps: float) -> dict[str, Any]:
    tr = simulate_position(df, lookback=lookback, buffer_bps=buffer_bps, fee_bps=fee_bps, spec=spec)
    is_s = stats(slice_trades(tr, "is"))
    oos_s = stats(slice_trades(tr, "oos"))
    ex_s = stats(slice_trades(tr, "oos_ex_2024"))
    wf = walk_forward(tr)
    ok = ex_s["pf"] >= 1.30 and ex_s["top3"] <= 80.0 and wf["ok_pct"] >= 60.0 and ex_s["n"] >= 12
    return {
        "label": spec.label,
        "max_hold": spec.max_hold,
        "ma_exit": spec.ma_exit,
        "trail_pct": spec.trail_pct,
        "is_n": is_s["n"],
        "is_net": is_s["net"],
        "is_pf": is_s["pf"],
        "oos_n": oos_s["n"],
        "oos_net": oos_s["net"],
        "oos_pf": oos_s["pf"],
        "ex_n": ex_s["n"],
        "ex_net": ex_s["net"],
        "ex_pf": ex_s["pf"],
        "ex_top3": ex_s["top3"],
        "ex_hold": ex_s["med_hold"],
        "wf_ok": wf["ok"],
        "wf_folds": wf["folds"],
        "wf_ok_pct": wf["ok_pct"],
        "wf_net": wf["net"],
        "wf_pf": wf["mean_pf"],
        "pass": "PASS" if ok else "FAIL",
        "trades": tr,
    }


def print_grid(rows: list[dict[str, Any]], top: int) -> None:
    rows_sorted = sorted(
        rows,
        key=lambda r: (
            r["pass"] == "PASS",
            float(r["ex_pf"]) if np.isfinite(float(r["ex_pf"])) else -1.0,
            -float(r["ex_top3"]),
            float(r["wf_ok_pct"]),
            float(r["ex_net"]),
        ),
        reverse=True,
    )
    print("\n" + "=" * 112)
    print(f"  TOP {top} POSITION EXIT VARIANTS")
    print("=" * 112)
    print(
        f"  {'exit':16} {'ex n':>5} {'ex net':>9} {'ex PF':>6} {'top3':>7} {'medH':>5} "
        f"{'WF':>8} {'WF net':>9} {'status':>7}"
    )
    print("-" * 112)
    for r in rows_sorted[:top]:
        print(
            f"  {r['label']:16} {int(r['ex_n']):>5} {float(r['ex_net']) * 100:>8.2f}% "
            f"{fmt_pf(float(r['ex_pf'])):>6} {float(r['ex_top3']):>6.1f}% {float(r['ex_hold']):>5.0f} "
            f"{int(r['wf_ok']):>2}/{int(r['wf_folds']):<2} {float(r['wf_net']) * 100:>8.2f}% {str(r['pass']):>7}"
        )


def print_detail(row: dict[str, Any], df: pd.DataFrame, lookback: int, buffer_bps: float, fee_bps: float) -> None:
    tr = row["trades"]
    print("\n" + "=" * 112)
    print(f"  DETAIL: entry close > prior {lookback}d high + {buffer_bps:.0f}bps | exit {row['label']} | fee {fee_bps:.1f}bps")
    print("=" * 112)
    print(f"  {'slice':12} {'n':>5} {'net':>10} {'PF':>6} {'WR':>7} {'avg':>9} {'top3':>8} {'medH':>5}")
    print("-" * 80)
    for label, mode in (("IS", "is"), ("OOS", "oos"), ("OOS ex-24", "oos_ex_2024")):
        s = stats(slice_trades(tr, mode))
        print(
            f"  {label:12} {s['n']:>5} {s['net'] * 100:>9.2f}% {fmt_pf(s['pf']):>6} "
            f"{s['wr']:>6.1f}% {s['avg'] * 100:>8.2f}% {s['top3']:>7.1f}% {s['med_hold']:>5.0f}"
        )

    print("\n  Yearly OOS by entry date")
    print(f"  {'year':>6} {'n':>5} {'net':>10} {'PF':>6}")
    print("-" * 34)
    oos = slice_trades(tr, "oos")
    if not oos.empty:
        for year, grp in oos.groupby(pd.to_datetime(oos["entry_date"], utc=True).dt.year):
            s = stats(grp)
            print(f"  {year:>6} {s['n']:>5} {s['net'] * 100:>9.2f}% {fmt_pf(s['pf']):>6}")

    print("\n  Fee stress on OOS ex-2024")
    print(f"  {'fee':>8} {'n':>5} {'net':>10} {'PF':>6} {'top3':>8}")
    print("-" * 46)
    spec = ExitSpec(max_hold=int(row["max_hold"]), ma_exit=int(row["ma_exit"]), trail_pct=float(row["trail_pct"]))
    for mult in (0.5, 1.0, 2.0, 4.0):
        fee = fee_bps * mult
        t2 = simulate_position(df, lookback=lookback, buffer_bps=buffer_bps, fee_bps=fee, spec=spec)
        s = stats(slice_trades(t2, "oos_ex_2024"))
        print(f"  {fee:>8.1f} {s['n']:>5} {s['net'] * 100:>9.2f}% {fmt_pf(s['pf']):>6} {s['top3']:>7.1f}%")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BTC breakout position exit probe")
    p.add_argument("--start", default="2014-09-17")
    p.add_argument("--end", default=None, help="Download end date (default: today)")
    p.add_argument("--is-start", default="2015-01-01")
    p.add_argument("--is-end", default="2020-12-31")
    p.add_argument("--oos-start", default="2021-01-01")
    p.add_argument("--oos-end", default=None)
    p.add_argument("--lookback", type=int, default=20)
    p.add_argument("--buffer-bps", type=float, default=300.0)
    p.add_argument("--fee-bps", type=float, default=10.0)
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--max-holds", default="5,10,20,40,80,120")
    p.add_argument("--ma-exits", default="0,10,20,50,100")
    p.add_argument("--trails", default="0,10,15,20,30")
    return p.parse_args()


def main() -> None:
    global IS_T0, IS_T1, OOS_T0, OOS_T1
    args = parse_args()
    end = args.end or pd.Timestamp.today(tz="UTC").strftime("%Y-%m-%d")
    IS_T0 = pd.Timestamp(args.is_start, tz="UTC")
    IS_T1 = pd.Timestamp(args.is_end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    OOS_T0 = pd.Timestamp(args.oos_start, tz="UTC")
    OOS_T1 = (
        pd.Timestamp(args.oos_end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        if args.oos_end
        else pd.Timestamp.today(tz="UTC").normalize() + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    )

    df = fetch_btc(args.start, end)
    max_holds = [int(x) for x in args.max_holds.split(",") if x.strip()]
    ma_exits = [int(x) for x in args.ma_exits.split(",") if x.strip()]
    trails = [float(x) / 100.0 for x in args.trails.split(",") if x.strip()]

    specs = [
        ExitSpec(max_hold=h, ma_exit=ma, trail_pct=tr)
        for h in max_holds
        for ma in ma_exits
        for tr in trails
        if not (ma == 0 and tr == 0 and h == 0)
    ]

    rows = [
        score_variant(df, spec, args.lookback, args.buffer_bps, args.fee_bps)
        for spec in specs
    ]

    print("=" * 112)
    print("  BTC BREAKOUT POSITION PROBE")
    print("=" * 112)
    print(f"  Data: {df.index[0].date()} -> {df.index[-1].date()} rows={len(df):,}")
    print(f"  IS: {IS_T0.date()} -> {IS_T1.date()}   OOS: {OOS_T0.date()} -> {OOS_T1.date()}")
    print(f"  Entry: close > prior {args.lookback}d high + {args.buffer_bps:.0f}bps")
    print(f"  Execution: signal close[t], enter next open, exit at close; round-trip fee={2 * args.fee_bps:.1f}bps")
    print("  Accept rule: ex-2024 PF>=1.30, top3<=80%, ex-2024 n>=12, WF profitable folds>=60%")
    buy_hold_stats(df)
    print_grid(rows, top=args.top)

    rows_sorted = sorted(
        rows,
        key=lambda r: (
            r["pass"] == "PASS",
            float(r["ex_pf"]) if np.isfinite(float(r["ex_pf"])) else -1.0,
            -float(r["ex_top3"]),
            float(r["wf_ok_pct"]),
            float(r["ex_net"]),
        ),
        reverse=True,
    )
    if rows_sorted:
        print_detail(rows_sorted[0], df, args.lookback, args.buffer_bps, args.fee_bps)
    print("\n" + "=" * 112)


if __name__ == "__main__":
    main()
