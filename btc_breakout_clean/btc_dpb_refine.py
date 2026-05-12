#!/usr/bin/env python3
"""
btc_dpb_refine.py

Focused refinement for the only BTC branch that survived the first probe:
DPB_pivot_compat = long-only breakout/volatility exposure.

Question:
  Can we reduce trade count / fragility and improve OOS ex-2024 PF by requiring
  a stronger breakout?

What it scans:
  - mode:
      any_break_long  = long after upside OR downside N-day breakout (pivot-compatible)
      up_break_long   = long after upside N-day breakout only
  - lookback: N-day high/low window
  - buffer_bps: required breakout buffer beyond high/low

Accept rule (default):
  OOS ex-2024 PF >= 1.30
  OOS ex-2024 top3 <= 80%
  walk-forward profitable folds >= 60%

Usage:
  python3 last/btc_dpb_refine.py
  python3 last/btc_dpb_refine.py --fee-bps 20
  python3 last/btc_dpb_refine.py --max-history
"""

from __future__ import annotations

import argparse
import warnings
from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

IS_T0 = pd.Timestamp("2018-01-01", tz="UTC")
IS_T1 = pd.Timestamp("2022-12-31 23:59:59", tz="UTC")
OOS_T0 = pd.Timestamp("2023-01-01", tz="UTC")
OOS_T1 = pd.Timestamp.today(tz="UTC").normalize() + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)

Mode = Literal["any_break_long", "up_break_long"]


@dataclass(frozen=True)
class Variant:
    mode: Mode
    lookback: int
    buffer_bps: float


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


def signal_dpb(df: pd.DataFrame, v: Variant) -> pd.Series:
    high = df["close"].rolling(v.lookback).max().shift(1)
    low = df["close"].rolling(v.lookback).min().shift(1)
    buf = v.buffer_bps / 10_000.0
    up = df["close"] > high * (1.0 + buf)
    down = df["close"] < low * (1.0 - buf)
    if v.mode == "any_break_long":
        sig = up | down
    elif v.mode == "up_break_long":
        sig = up
    else:
        raise ValueError(v.mode)
    return sig.astype(float)


def build_trades(df: pd.DataFrame, signal: pd.Series, fee_bps: float) -> pd.DataFrame:
    sig = signal.reindex(df.index).fillna(0.0).astype(float)
    traded = sig != 0.0
    pnl = sig * df["ret"].shift(-1) - traded.astype(float) * (fee_bps / 10_000.0)
    return pd.DataFrame(
        {
            "close": df["close"],
            "signal": sig,
            "pnl": pnl,
            "traded": traded,
        },
        index=df.index,
    ).dropna(subset=["pnl"])


def slice_df(df: pd.DataFrame, mode: str) -> pd.DataFrame:
    if mode == "is":
        return df.loc[(df.index >= IS_T0) & (df.index <= IS_T1)]
    if mode == "oos":
        return df.loc[(df.index >= OOS_T0) & (df.index <= OOS_T1)]
    if mode == "oos_ex_2024":
        x = slice_df(df, "oos")
        return x.loc[x.index.year != 2024]
    raise ValueError(mode)


def stats(tr: pd.DataFrame) -> dict[str, float]:
    pnl = pd.to_numeric(tr.loc[tr["traded"], "pnl"], errors="coerce").dropna()
    n = int(len(pnl))
    if n == 0:
        return {"n": 0, "net": 0.0, "pf": float("nan"), "wr": float("nan"), "top3": 0.0, "avg": float("nan")}
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
        "top3": top3,
        "avg": float(pnl.mean() * 10_000.0),
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
        oos = tr.loc[(tr.index >= oos0) & (tr.index < oos1)]
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


def score_row(v: Variant, tr: pd.DataFrame) -> dict[str, float | str | int]:
    is_s = stats(slice_df(tr, "is"))
    oos_s = stats(slice_df(tr, "oos"))
    ex_s = stats(slice_df(tr, "oos_ex_2024"))
    wf = walk_forward(tr)
    passes = ex_s["pf"] >= 1.30 and ex_s["top3"] <= 80.0 and wf["ok_pct"] >= 60.0
    return {
        "mode": v.mode,
        "lookback": v.lookback,
        "buffer_bps": v.buffer_bps,
        "is_pf": is_s["pf"],
        "is_net": is_s["net"],
        "oos_pf": oos_s["pf"],
        "oos_net": oos_s["net"],
        "ex_n": ex_s["n"],
        "ex_pf": ex_s["pf"],
        "ex_net": ex_s["net"],
        "ex_top3": ex_s["top3"],
        "wf_ok": wf["ok"],
        "wf_folds": wf["folds"],
        "wf_ok_pct": wf["ok_pct"],
        "wf_net": wf["net"],
        "wf_pf": wf["mean_pf"],
        "pass": "PASS" if passes else "FAIL",
    }


def print_grid(rows: list[dict[str, float | str | int]], *, top: int) -> None:
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
    print("\n" + "=" * 100)
    print(f"  TOP {top} DPB REFINEMENTS")
    print("=" * 100)
    print(
        f"  {'mode':15} {'lb':>4} {'buf':>6} "
        f"{'ex n':>5} {'ex net':>9} {'ex PF':>6} {'top3':>7} "
        f"{'WF':>8} {'WF net':>9} {'status':>7}"
    )
    print("-" * 100)
    for r in rows_sorted[:top]:
        print(
            f"  {str(r['mode']):15} {int(r['lookback']):>4} {float(r['buffer_bps']):>6.0f} "
            f"{int(r['ex_n']):>5} {float(r['ex_net']) * 100:>8.2f}% {fmt_pf(float(r['ex_pf'])):>6} "
            f"{float(r['ex_top3']):>6.1f}% "
            f"{int(r['wf_ok']):>2}/{int(r['wf_folds']):<2} {float(r['wf_net']) * 100:>8.2f}% "
            f"{str(r['pass']):>7}"
        )


def print_detail(df: pd.DataFrame, v: Variant, fee_bps: float) -> None:
    tr = build_trades(df, signal_dpb(df, v), fee_bps)
    print("\n" + "=" * 100)
    print(f"  DETAIL: {v.mode} lookback={v.lookback} buffer={v.buffer_bps:.0f}bps fee={fee_bps:.1f}bps")
    print("=" * 100)
    print(f"  {'slice':12} {'n':>5} {'net':>10} {'PF':>6} {'WR':>7} {'avg bp':>8} {'top3':>8}")
    print("-" * 72)
    for label, mode in (("IS", "is"), ("OOS", "oos"), ("OOS ex-24", "oos_ex_2024")):
        s = stats(slice_df(tr, mode))
        print(
            f"  {label:12} {s['n']:>5} {s['net'] * 100:>9.2f}% {fmt_pf(s['pf']):>6} "
            f"{s['wr']:>6.1f}% {s['avg']:>8.1f} {s['top3']:>7.1f}%"
        )

    print("\n  Yearly OOS")
    print(f"  {'year':>6} {'n':>5} {'net':>10} {'PF':>6}")
    print("-" * 34)
    oos = slice_df(tr, "oos")
    for year, grp in oos.groupby(oos.index.year):
        s = stats(grp)
        print(f"  {year:>6} {s['n']:>5} {s['net'] * 100:>9.2f}% {fmt_pf(s['pf']):>6}")

    print("\n  Fee stress on OOS ex-2024")
    print(f"  {'fee':>8} {'n':>5} {'net':>10} {'PF':>6} {'top3':>8}")
    print("-" * 46)
    for mult in (0.5, 1.0, 2.0, 4.0):
        fee = fee_bps * mult
        t2 = build_trades(df, signal_dpb(df, v), fee)
        s = stats(slice_df(t2, "oos_ex_2024"))
        print(f"  {fee:>8.1f} {s['n']:>5} {s['net'] * 100:>9.2f}% {fmt_pf(s['pf']):>6} {s['top3']:>7.1f}%")

    wf = walk_forward(tr)
    print(
        f"\n  WF fixed params: {wf['ok']}/{wf['folds']} profitable "
        f"({wf['ok_pct']:.0f}%), agg_net={wf['net'] * 100:.2f}%, mean_pf={fmt_pf(wf['mean_pf'])}"
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Refine BTC long-only DPB")
    p.add_argument("--start", default="2018-01-01")
    p.add_argument("--end", default=None, help="Download end date (default: today)")
    p.add_argument(
        "--max-history",
        action="store_true",
        help="Use yfinance max BTC history and split IS=2015-2020 / OOS=2021-today",
    )
    p.add_argument("--is-start", default=None)
    p.add_argument("--is-end", default=None)
    p.add_argument("--oos-start", default=None)
    p.add_argument("--oos-end", default=None)
    p.add_argument("--fee-bps", type=float, default=10.0)
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--lookbacks", default="10,20,30,50,80,100,150")
    p.add_argument("--buffers", default="0,25,50,100,200,300")
    return p.parse_args()


def main() -> None:
    global IS_T0, IS_T1, OOS_T0, OOS_T1
    args = parse_args()

    if args.max_history:
        args.start = "2014-09-17"  # earliest BTC-USD yfinance history
        args.is_start = args.is_start or "2015-01-01"
        args.is_end = args.is_end or "2020-12-31"
        args.oos_start = args.oos_start or "2021-01-01"

    end = args.end or pd.Timestamp.today(tz="UTC").strftime("%Y-%m-%d")

    if args.is_start:
        IS_T0 = pd.Timestamp(args.is_start, tz="UTC")
    if args.is_end:
        IS_T1 = pd.Timestamp(args.is_end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    if args.oos_start:
        OOS_T0 = pd.Timestamp(args.oos_start, tz="UTC")
    if args.oos_end:
        OOS_T1 = pd.Timestamp(args.oos_end, tz="UTC") + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    else:
        OOS_T1 = pd.Timestamp.today(tz="UTC").normalize() + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)

    df = fetch_btc(args.start, end)
    lookbacks = [int(x) for x in args.lookbacks.split(",") if x.strip()]
    buffers = [float(x) for x in args.buffers.split(",") if x.strip()]
    variants = [
        Variant(mode=mode, lookback=lb, buffer_bps=buf)
        for mode in ("any_break_long", "up_break_long")
        for lb in lookbacks
        for buf in buffers
    ]

    rows: list[dict[str, float | str | int]] = []
    for v in variants:
        tr = build_trades(df, signal_dpb(df, v), args.fee_bps)
        rows.append(score_row(v, tr))

    print("=" * 100)
    print("  BTC DPB REFINEMENT — long-only breakout variants")
    print("=" * 100)
    print(f"  Data: {df.index[0].date()} -> {df.index[-1].date()} rows={len(df):,}")
    print(f"  IS: {IS_T0.date()} -> {IS_T1.date()}   OOS: {OOS_T0.date()} -> {OOS_T1.date()}")
    print(f"  fee={args.fee_bps:.1f}bps  variants={len(variants)}")
    print("  Accept rule: ex-2024 PF>=1.30, top3<=80%, WF profitable folds>=60%")
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
        best = rows_sorted[0]
        print_detail(
            df,
            Variant(
                mode=str(best["mode"]),  # type: ignore[arg-type]
                lookback=int(best["lookback"]),
                buffer_bps=float(best["buffer_bps"]),
            ),
            args.fee_bps,
        )
    print("\n" + "=" * 100)


if __name__ == "__main__":
    main()
