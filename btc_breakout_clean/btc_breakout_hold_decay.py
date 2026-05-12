#!/usr/bin/env python3
"""
btc_breakout_hold_decay.py

BTC fixed-hold breakout decay test.

Question:
  Does the BTC breakout edge appear shortly after the signal, or only after
  months of BTC beta exposure?

Entry:
  close[t] > prior N-day high * (1 + buffer)
  default: N=20, buffer=300 bps.

Execution:
  signal at close[t], enter next day's open, hold a fixed number of days,
  exit at close. Positions are non-overlapping.

Usage:
  python3 last/btc_breakout_hold_decay.py
  python3 last/btc_breakout_hold_decay.py --fee-bps 20
  python3 last/btc_breakout_hold_decay.py --holds 1,2,3,5,10,20
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
class SimResult:
    hold_days: int
    daily: pd.DataFrame
    trades: pd.DataFrame
    skipped_signals: int = 0
    sizing_label: str = "1.00x"


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
    return df.dropna(subset=["open", "high", "low", "close"])


def entry_signal(df: pd.DataFrame, lookback: int, buffer_bps: float) -> pd.Series:
    prior_high = df["close"].rolling(lookback).max().shift(1)
    return df["close"] > prior_high * (1.0 + buffer_bps / 10_000.0)


def simulate_hold(
    df: pd.DataFrame,
    *,
    lookback: int,
    buffer_bps: float,
    hold_days: int,
    fee_bps: float,
    allocation: float = 1.0,
    vol_target: float = 0.0,
    max_alloc: float = 1.0,
    skip_vol_pct: float = 0.0,
    sizing_label: str | None = None,
) -> SimResult:
    d = df.copy()
    sig = entry_signal(d, lookback, buffer_bps)
    btc_ret = d["close"].pct_change()
    rvol20 = btc_ret.rolling(20).std()
    vol_rank = rvol20.rolling(252).apply(
        lambda x: (x[:-1] < x[-1]).sum() / max(len(x) - 1, 1),
        raw=True,
    )
    fee = fee_bps / 10_000.0
    rets = pd.Series(0.0, index=d.index)
    exposed = pd.Series(False, index=d.index)
    sizes = pd.Series(0.0, index=d.index)
    trades: list[dict[str, Any]] = []

    in_pos = False
    entry_i = -1
    entry_date: pd.Timestamp | None = None
    entry_px = 0.0
    entry_fees = 0.0
    entry_size = 0.0
    skipped = 0

    def _entry_size(signal_i: int) -> float:
        if skip_vol_pct and np.isfinite(vol_rank.iloc[signal_i]) and vol_rank.iloc[signal_i] > skip_vol_pct:
            return 0.0
        if vol_target > 0.0:
            rv = float(rvol20.iloc[signal_i])
            if not np.isfinite(rv) or rv <= 0.0:
                return 0.0
            return max(0.0, min(max_alloc, vol_target / rv))
        return max(0.0, min(allocation, max_alloc))

    for i in range(1, len(d)):
        date = d.index[i]
        open_px = float(d["open"].iloc[i])
        close = float(d["close"].iloc[i])
        prev_close = float(d["close"].iloc[i - 1])

        if not in_pos:
            if bool(sig.iloc[i - 1]):
                pos_size = _entry_size(i - 1)
                if pos_size <= 0.0:
                    skipped += 1
                    continue
                in_pos = True
                entry_i = i
                entry_date = date
                entry_px = open_px
                entry_size = pos_size
                entry_fees = fee * entry_size
                # Entry day return: next open -> same-day close, less entry fee.
                day_ret = entry_size * (close / open_px - 1.0) - entry_fees
                age = 1
                if hold_days <= 1 or i == len(d) - 1:
                    day_ret -= fee * entry_size
                    gross = close / entry_px - 1.0
                    net = entry_size * gross - 2.0 * fee * entry_size
                    trades.append(
                        {
                            "entry_date": entry_date,
                            "exit_date": date,
                            "entry_px": entry_px,
                            "exit_px": close,
                            "hold_days": age,
                            "size": entry_size,
                            "gross_ret": gross,
                            "net_ret": net,
                        }
                    )
                    in_pos = False
                rets.iloc[i] = day_ret
                exposed.iloc[i] = True
                sizes.iloc[i] = entry_size
            continue

        exposed.iloc[i] = True
        sizes.iloc[i] = entry_size
        day_ret = entry_size * (close / prev_close - 1.0)
        age = i - entry_i + 1
        if age >= hold_days or i == len(d) - 1:
            day_ret -= fee * entry_size
            gross = close / entry_px - 1.0
            net = entry_size * gross - entry_fees - fee * entry_size
            trades.append(
                {
                    "entry_date": entry_date,
                    "exit_date": date,
                    "entry_px": entry_px,
                    "exit_px": close,
                    "hold_days": age,
                    "size": entry_size,
                    "gross_ret": gross,
                    "net_ret": net,
                }
            )
            in_pos = False
        rets.iloc[i] = day_ret

    daily = pd.DataFrame(
        {"strategy_ret": rets, "exposed": exposed, "size": sizes, "close": d["close"]},
        index=d.index,
    )
    label = sizing_label or (f"vol{vol_target:.2%}_cap{max_alloc:.2f}" if vol_target else f"{allocation:.2f}x")
    return SimResult(
        hold_days=hold_days,
        daily=daily,
        trades=pd.DataFrame(trades),
        skipped_signals=skipped,
        sizing_label=label,
    )


def date_mask(index: pd.DatetimeIndex, mode: str) -> pd.Series:
    if mode == "is":
        return pd.Series((index >= IS_T0) & (index <= IS_T1), index=index)
    if mode == "oos":
        return pd.Series((index >= OOS_T0) & (index <= OOS_T1), index=index)
    if mode == "oos_ex_2024":
        return pd.Series((index >= OOS_T0) & (index <= OOS_T1) & (index.year != 2024), index=index)
    raise ValueError(mode)


def trades_for_period(trades: pd.DataFrame, mode: str) -> pd.DataFrame:
    if trades.empty:
        return trades.copy()
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    m = date_mask(pd.DatetimeIndex(t["entry_date"]), mode).to_numpy()
    return t.loc[m].copy()


def trade_stats(trades: pd.DataFrame) -> dict[str, float]:
    if trades.empty:
        return {"n": 0, "pf": float("nan"), "wr": float("nan"), "top3": 0.0, "avg_trade": float("nan")}
    r = pd.to_numeric(trades["net_ret"], errors="coerce").dropna()
    n = int(len(r))
    if n == 0:
        return {"n": 0, "pf": float("nan"), "wr": float("nan"), "top3": 0.0, "avg_trade": float("nan")}
    wins = r[r > 0]
    losses = r[r < 0]
    gross_w = float(wins.sum())
    gross_l = float(losses.sum())
    net = float(r.sum())
    pf = gross_w / abs(gross_l) if gross_l < 0 else float("nan")
    top3 = abs(float(r.nlargest(3).sum()) / net) * 100.0 if net != 0 else 0.0
    return {
        "n": n,
        "pf": pf,
        "wr": 100.0 * len(wins) / n,
        "top3": top3,
        "avg_trade": float(r.mean()),
    }


def period_stats(res: SimResult, mode: str) -> dict[str, float]:
    d = res.daily.loc[date_mask(res.daily.index, mode)].copy()
    if d.empty:
        return {
            "n": 0,
            "ret": 0.0,
            "maxdd": float("nan"),
            "exposure": 0.0,
            "avg_size": 0.0,
            "pf": float("nan"),
            "wr": float("nan"),
            "top3": 0.0,
        }
    eq = (1.0 + d["strategy_ret"]).cumprod()
    dd = eq / eq.cummax() - 1.0
    ts = trade_stats(trades_for_period(res.trades, mode))
    avg_size = float(d.loc[d["exposed"], "size"].mean()) if bool(d["exposed"].any()) else 0.0
    return {
        "n": ts["n"],
        "ret": float(eq.iloc[-1] - 1.0),
        "maxdd": float(dd.min()),
        "exposure": 100.0 * float(d["exposed"].mean()),
        "avg_size": avg_size,
        "pf": ts["pf"],
        "wr": ts["wr"],
        "top3": ts["top3"],
        "avg_trade": ts["avg_trade"],
    }


def fmt_pf(v: float) -> str:
    return f"{v:.2f}" if np.isfinite(v) else "n/a"


def month_add(ts: pd.Timestamp, months: int) -> pd.Timestamp:
    return ts + pd.DateOffset(months=months)


def walk_forward(res: SimResult) -> dict[str, float]:
    start = IS_T0
    folds = 0
    ok = 0
    rets: list[float] = []
    pfs: list[float] = []
    while True:
        is1 = month_add(start, 24)
        oos0 = is1
        oos1 = month_add(oos0, 6) - pd.Timedelta(seconds=1)
        if oos1 > OOS_T1:
            break
        d = res.daily.loc[(res.daily.index >= oos0) & (res.daily.index <= oos1)]
        if d.empty:
            start = month_add(start, 6)
            continue
        eq = (1.0 + d["strategy_ret"]).cumprod()
        ret = float(eq.iloc[-1] - 1.0)
        tr = res.trades.copy()
        if not tr.empty:
            tr["entry_date"] = pd.to_datetime(tr["entry_date"], utc=True)
            tr = tr.loc[(tr["entry_date"] >= oos0) & (tr["entry_date"] <= oos1)]
        ts = trade_stats(tr)
        folds += 1
        ok += int(ret > 0)
        rets.append(ret)
        if np.isfinite(ts["pf"]):
            pfs.append(ts["pf"])
        start = month_add(start, 6)
    return {
        "folds": folds,
        "ok": ok,
        "ok_pct": 100.0 * ok / folds if folds else 0.0,
        "compound_ret_sum": float(sum(rets)),
        "mean_pf": float(np.mean(pfs)) if pfs else float("nan"),
    }


def selected_buy_hold_equity(df: pd.DataFrame, mask: pd.Series) -> pd.Series:
    selected = pd.Series(mask, index=df.index).astype(bool)
    daily_ret = df["close"].pct_change().fillna(0.0)
    starts = selected & ~selected.shift(1, fill_value=False)
    selected_ret = daily_ret.loc[selected].copy()
    selected_ret.loc[starts.loc[selected]] = 0.0
    return (1.0 + selected_ret).cumprod()


def buy_hold_stats(df: pd.DataFrame) -> None:
    print("\n  Buy-and-hold benchmark")
    print(f"  {'slice':12} {'ret':>10} {'maxDD':>10}")
    print("-" * 36)
    for label, mask in (
        ("IS", (df.index >= IS_T0) & (df.index <= IS_T1)),
        ("OOS", (df.index >= OOS_T0) & (df.index <= OOS_T1)),
        ("OOS ex-24", (df.index >= OOS_T0) & (df.index <= OOS_T1) & (df.index.year != 2024)),
    ):
        eq = selected_buy_hold_equity(df, mask)
        if len(eq) < 2:
            continue
        dd = eq / eq.cummax() - 1.0
        print(f"  {label:12} {((eq.iloc[-1] - 1.0) * 100):>9.2f}% {(dd.min() * 100):>9.2f}%")


def print_hold_table(results: list[SimResult], min_trades: int) -> None:
    rows: list[dict[str, float]] = []
    for res in results:
        is_s = period_stats(res, "is")
        oos_s = period_stats(res, "oos")
        ex_s = period_stats(res, "oos_ex_2024")
        wf = walk_forward(res)
        ok = ex_s["pf"] >= 1.30 and ex_s["top3"] <= 80.0 and wf["ok_pct"] >= 60.0 and ex_s["n"] >= min_trades
        rows.append(
            {
                "hold": res.hold_days,
                "is_ret": is_s["ret"],
                "oos_ret": oos_s["ret"],
                "ex_n": ex_s["n"],
                "ex_ret": ex_s["ret"],
                "ex_pf": ex_s["pf"],
                "ex_top3": ex_s["top3"],
                "ex_dd": ex_s["maxdd"],
                "ex_exp": ex_s["exposure"],
                "wf_ok": wf["ok"],
                "wf_folds": wf["folds"],
                "wf_ok_pct": wf["ok_pct"],
                "wf_ret": wf["compound_ret_sum"],
                "status": "PASS" if ok else "FAIL",
            }
        )

    rows = sorted(rows, key=lambda r: (r["status"] == "PASS", r["ex_pf"], -r["ex_top3"], r["wf_ok_pct"]), reverse=True)
    print("\n" + "=" * 110)
    print("  FIXED-HOLD DECAY")
    print("=" * 110)
    print(
        f"  {'hold':>4} {'IS ret':>9} {'OOS ret':>9} {'ex n':>5} {'ex ret':>9} "
        f"{'ex PF':>6} {'top3':>7} {'ex DD':>8} {'exp':>6} {'WF':>8} {'status':>7}"
    )
    print("-" * 110)
    for r in rows:
        print(
            f"  {int(r['hold']):>4} {r['is_ret'] * 100:>8.2f}% {r['oos_ret'] * 100:>8.2f}% "
            f"{int(r['ex_n']):>5} {r['ex_ret'] * 100:>8.2f}% {fmt_pf(r['ex_pf']):>6} "
            f"{r['ex_top3']:>6.1f}% {r['ex_dd'] * 100:>7.2f}% {r['ex_exp']:>5.1f}% "
            f"{int(r['wf_ok']):>2}/{int(r['wf_folds']):<2} {r['status']:>7}"
        )


def print_yearly(res: SimResult) -> None:
    print(f"\n  Yearly OOS detail for hold={res.hold_days}")
    print(f"  {'year':>6} {'n':>5} {'ret':>9} {'PF':>6} {'DD':>8} {'exp':>6}")
    print("-" * 52)
    oos_daily = res.daily.loc[date_mask(res.daily.index, "oos")]
    for year, grp in oos_daily.groupby(oos_daily.index.year):
        mode_idx = grp.index
        eq = (1.0 + grp["strategy_ret"]).cumprod()
        dd = eq / eq.cummax() - 1.0
        tr = res.trades.copy()
        if not tr.empty:
            tr["entry_date"] = pd.to_datetime(tr["entry_date"], utc=True)
            tr = tr.loc[tr["entry_date"].dt.year == year]
        ts = trade_stats(tr)
        print(
            f"  {year:>6} {ts['n']:>5} {(eq.iloc[-1] - 1.0) * 100:>8.2f}% "
            f"{fmt_pf(ts['pf']):>6} {dd.min() * 100:>7.2f}% {100 * grp['exposed'].mean():>5.1f}%"
        )


def print_risk_grid(
    df: pd.DataFrame,
    *,
    lookback: int,
    buffer_bps: float,
    hold_days: int,
    fee_bps: float,
) -> None:
    """Risk-reduction grid for the chosen short-hold setup."""
    configs: list[dict[str, Any]] = []
    for alloc in (1.0, 0.5, 0.25):
        configs.append(
            {
                "label": f"fixed {alloc:.2f}x",
                "allocation": alloc,
                "vol_target": 0.0,
                "max_alloc": alloc,
                "skip_vol_pct": 0.0,
            }
        )
    for vt in (0.005, 0.01, 0.015):
        for cap in (0.25, 0.50, 1.00):
            configs.append(
                {
                    "label": f"vol {vt:.1%} cap {cap:.2f}x",
                    "allocation": 1.0,
                    "vol_target": vt,
                    "max_alloc": cap,
                    "skip_vol_pct": 0.0,
                }
            )
    for skip in (0.80, 0.90):
        configs.append(
            {
                "label": f"fixed 1x skip vol>{int(skip * 100)}%",
                "allocation": 1.0,
                "vol_target": 0.0,
                "max_alloc": 1.0,
                "skip_vol_pct": skip,
            }
        )

    rows: list[dict[str, Any]] = []
    for cfg in configs:
        res = simulate_hold(
            df,
            lookback=lookback,
            buffer_bps=buffer_bps,
            hold_days=hold_days,
            fee_bps=fee_bps,
            allocation=cfg["allocation"],
            vol_target=cfg["vol_target"],
            max_alloc=cfg["max_alloc"],
            skip_vol_pct=cfg["skip_vol_pct"],
            sizing_label=cfg["label"],
        )
        ex = period_stats(res, "oos_ex_2024")
        wf = walk_forward(res)
        rows.append(
            {
                "label": cfg["label"],
                "n": ex["n"],
                "ret": ex["ret"],
                "pf": ex["pf"],
                "dd": ex["maxdd"],
                "exposure": ex["exposure"],
                "avg_size": ex["avg_size"],
                "top3": ex["top3"],
                "wf_ok": wf["ok"],
                "wf_folds": wf["folds"],
                "skipped": res.skipped_signals,
            }
        )

    rows.sort(key=lambda r: (r["pf"], -abs(r["dd"]), r["ret"]), reverse=True)
    print("\n" + "=" * 110)
    print(f"  RISK SIZING GRID - hold={hold_days}, OOS ex-2024")
    print("=" * 110)
    print(
        f"  {'sizing':28} {'n':>5} {'ret':>9} {'PF':>6} {'DD':>8} "
        f"{'exp':>6} {'avgSz':>7} {'top3':>7} {'WF':>8} {'skip':>5}"
    )
    print("-" * 110)
    for r in rows:
        print(
            f"  {r['label']:28} {int(r['n']):>5} {r['ret'] * 100:>8.2f}% "
            f"{fmt_pf(r['pf']):>6} {r['dd'] * 100:>7.2f}% {r['exposure']:>5.1f}% "
            f"{r['avg_size']:>6.2f}x {r['top3']:>6.1f}% "
            f"{int(r['wf_ok']):>2}/{int(r['wf_folds']):<2} {int(r['skipped']):>5}"
        )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BTC breakout fixed-hold decay")
    p.add_argument("--start", default="2014-09-17")
    p.add_argument("--end", default=None, help="Download end date (default: today)")
    p.add_argument("--is-start", default="2015-01-01")
    p.add_argument("--is-end", default="2020-12-31")
    p.add_argument("--oos-start", default="2021-01-01")
    p.add_argument("--oos-end", default=None)
    p.add_argument("--lookback", type=int, default=20)
    p.add_argument("--buffer-bps", type=float, default=300.0)
    p.add_argument("--fee-bps", type=float, default=10.0, help="Per-side fee/slippage bps")
    p.add_argument("--holds", default="1,2,3,5,10")
    p.add_argument("--min-trades", type=int, default=30)
    p.add_argument("--risk-grid-hold", type=int, default=1, help="Hold length for risk sizing grid; 0 disables")
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
    holds = [int(x) for x in args.holds.split(",") if x.strip()]
    results = [
        simulate_hold(
            df,
            lookback=args.lookback,
            buffer_bps=args.buffer_bps,
            hold_days=h,
            fee_bps=args.fee_bps,
        )
        for h in holds
    ]

    print("=" * 110)
    print("  BTC BREAKOUT HOLD DECAY")
    print("=" * 110)
    print(f"  Data: {df.index[0].date()} -> {df.index[-1].date()} rows={len(df):,}")
    print(f"  IS: {IS_T0.date()} -> {IS_T1.date()}   OOS: {OOS_T0.date()} -> {OOS_T1.date()}")
    print(f"  Entry: close > prior {args.lookback}d high + {args.buffer_bps:.0f}bps")
    print(f"  Execution: enter next open, fixed hold, non-overlapping positions")
    print(f"  Costs: {args.fee_bps:.1f}bps per side ({2 * args.fee_bps:.1f}bps round trip)")
    print("  Risk grid: fixed fractions, daily-vol target sizing, caps, and high-vol skips")
    print(f"  Accept rule: OOS ex-2024 PF>=1.30, top3<=80%, min trades>={args.min_trades}, WF>=60%")
    buy_hold_stats(df)
    print_hold_table(results, args.min_trades)
    if results:
        # Show detail for the shortest passing hold, otherwise the best PF hold.
        passing = [
            r for r in results
            if period_stats(r, "oos_ex_2024")["pf"] >= 1.30
            and period_stats(r, "oos_ex_2024")["top3"] <= 80.0
            and period_stats(r, "oos_ex_2024")["n"] >= args.min_trades
            and walk_forward(r)["ok_pct"] >= 60.0
        ]
        target = sorted(passing, key=lambda r: r.hold_days)[0] if passing else max(results, key=lambda r: period_stats(r, "oos_ex_2024")["pf"])
        print_yearly(target)
    if args.risk_grid_hold > 0:
        print_risk_grid(
            df,
            lookback=args.lookback,
            buffer_bps=args.buffer_bps,
            hold_days=args.risk_grid_hold,
            fee_bps=args.fee_bps,
        )
    print("\n" + "=" * 110)


if __name__ == "__main__":
    main()
