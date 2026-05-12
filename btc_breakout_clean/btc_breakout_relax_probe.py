#!/usr/bin/env python3
"""
BTC breakout relaxation probe.

Starts from the constrained paper-sim baseline and relaxes one variable at a
time to see whether we can get materially more trades without destroying PF,
drawdown, or yearly stability.
"""

from __future__ import annotations

import argparse
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from btc_breakout_paper_sim import (
    SimConfig,
    fetch_source_data,
    fmt_pf,
    max_drawdown,
    profit_factor,
)

warnings.filterwarnings("ignore")


@dataclass(frozen=True)
class Variant:
    name: str
    lookback: int
    buffer_bps: float
    hold_days: int
    vol_target: float
    max_alloc: float
    trend_filter: str = "none"
    max_breakout_bps: float | None = None
    max_gap_bps: float | None = None


BASELINE = Variant(
    name="baseline",
    lookback=20,
    buffer_bps=300.0,
    hold_days=1,
    vol_target=0.015,
    max_alloc=0.75,
)
MIN_SCORING_TPY = 12.0


def parse_csv_ints(s: str) -> list[int]:
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def parse_csv_floats(s: str) -> list[float]:
    return [float(x.strip()) for x in s.split(",") if x.strip()]


def parse_optional_floats(s: str) -> list[float | None]:
    values: list[float | None] = []
    for raw in s.split(","):
        token = raw.strip().lower()
        if not token:
            continue
        values.append(None if token in {"none", "off", "na"} else float(token))
    return values


def fmt_limit(value: float | None) -> str:
    return "none" if value is None else f"{value:.0f}"


def add_signal(df: pd.DataFrame, variant: Variant) -> pd.DataFrame:
    out = df.copy()
    out["ret"] = out["close"].pct_change()
    out["vol20"] = out["ret"].rolling(20).std()
    out["ma50"] = out["close"].rolling(50).mean()
    out["ma50_slope"] = out["ma50"] - out["ma50"].shift(5)
    out["prior_high"] = out["close"].rolling(variant.lookback).max().shift(1)
    out["breakout_bps"] = 10_000.0 * (out["close"] / out["prior_high"] - 1.0)
    out["signal"] = out["close"] > out["prior_high"] * (1.0 + variant.buffer_bps / 10_000.0)
    if variant.max_breakout_bps is not None:
        out["signal"] &= out["breakout_bps"] <= variant.max_breakout_bps
    if variant.trend_filter == "ma50_slope":
        out["signal"] &= (out["close"] > out["ma50"]) & (out["ma50_slope"] > 0)
    out["signal"] = out["signal"].fillna(False)
    return out


def simulate_variant(
    df: pd.DataFrame,
    variant: Variant,
    *,
    sim_start: pd.Timestamp,
    initial_equity: float,
    fee_bps: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    d = add_signal(df, variant)
    fee = fee_bps / 10_000.0
    equity = float(initial_equity)
    trades: list[dict[str, Any]] = []
    curve: list[dict[str, Any]] = []
    next_entry_i = 1

    for i in range(1, len(d)):
        date = d.index[i]
        day_pnl = 0.0
        action = "NO_SIGNAL"
        if date >= sim_start and i >= next_entry_i and bool(d["signal"].iloc[i - 1]):
            signal_close = float(d["close"].iloc[i - 1])
            entry_px = float(d["open"].iloc[i])
            entry_gap_bps = 10_000.0 * (entry_px / signal_close - 1.0)
            if variant.max_gap_bps is not None and entry_gap_bps > variant.max_gap_bps:
                action = "SKIP_GAP"
            else:
                rv = float(d["vol20"].iloc[i - 1])
                size_frac = min(variant.max_alloc, variant.vol_target / rv) if np.isfinite(rv) and rv > 0 else 0.0
                exit_i = min(i + variant.hold_days - 1, len(d) - 1)
                if size_frac > 0.0 and exit_i >= i:
                    exit_px = float(d["close"].iloc[exit_i])
                    notional = initial_equity * size_frac
                    qty = notional / entry_px
                    exit_notional = qty * exit_px
                    fees = notional * fee + exit_notional * fee
                    gross_pnl = exit_notional - notional
                    day_pnl = gross_pnl - fees
                    equity += day_pnl
                    action = "TRADE"
                    next_entry_i = exit_i + 1
                    trades.append(
                        {
                            "signal_date": d.index[i - 1],
                            "entry_date": date,
                            "exit_date": d.index[exit_i],
                            "hold_days": exit_i - i + 1,
                            "entry_px": entry_px,
                            "exit_px": exit_px,
                            "signal_close": signal_close,
                            "breakout_bps": float(d["breakout_bps"].iloc[i - 1]),
                            "entry_gap_bps": entry_gap_bps,
                            "size_frac": size_frac,
                            "gross_pnl": gross_pnl,
                            "fees": fees,
                            "net_pnl": day_pnl,
                            "open_to_exit_pct": 100.0 * (exit_px / entry_px - 1.0),
                        }
                    )

        curve.append({"date": date, "equity": equity, "daily_pnl": day_pnl, "action": action})

    return pd.DataFrame(trades), pd.DataFrame(curve)


def yearly_stats(trades: pd.DataFrame) -> tuple[int, int, float, float]:
    if trades.empty:
        return 0, 0, 0.0, 0.0
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    t["net_pnl"] = pd.to_numeric(t["net_pnl"], errors="coerce")
    yearly = t.groupby(t["entry_date"].dt.year)["net_pnl"].sum()
    years = int(len(yearly))
    pos_years = int((yearly > 0).sum())
    worst_year = float(yearly.min()) if len(yearly) else 0.0
    best_year_share = float(yearly.max() / yearly.sum()) if yearly.sum() > 0 else 0.0
    return years, pos_years, worst_year, best_year_share


def pnl_from(trades: pd.DataFrame, start: str) -> float:
    if trades.empty:
        return 0.0
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    t["net_pnl"] = pd.to_numeric(t["net_pnl"], errors="coerce")
    return float(t.loc[t["entry_date"] >= pd.Timestamp(start, tz="UTC"), "net_pnl"].sum())


def trade_count_from(trades: pd.DataFrame, start: str) -> int:
    if trades.empty:
        return 0
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    return int((t["entry_date"] >= pd.Timestamp(start, tz="UTC")).sum())


def trade_stats_between(trades: pd.DataFrame, start: str, end: str) -> dict[str, Any]:
    if trades.empty:
        return {"trades": 0, "pnl": 0.0, "pf": float("nan")}
    t = trades.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    t["net_pnl"] = pd.to_numeric(t["net_pnl"], errors="coerce")
    start_ts = pd.Timestamp(start, tz="UTC")
    end_ts = pd.Timestamp(end, tz="UTC")
    p = t[(t["entry_date"] >= start_ts) & (t["entry_date"] <= end_ts)]
    pnls = p["net_pnl"]
    return {
        "trades": int(len(p)),
        "pnl": float(pnls.sum()) if len(pnls) else 0.0,
        "pf": profit_factor(pnls) if len(pnls) else float("nan"),
    }


def summarize_variant(
    df: pd.DataFrame,
    variant: Variant,
    *,
    sim_start: pd.Timestamp,
    initial_equity: float,
    fee_bps: float,
) -> dict[str, Any]:
    trades, curve = simulate_variant(df, variant, sim_start=sim_start, initial_equity=initial_equity, fee_bps=fee_bps)
    if curve.empty:
        raise RuntimeError("Empty simulation curve")

    equity = pd.to_numeric(curve["equity"], errors="coerce")
    total_ret = float(equity.iloc[-1] / initial_equity - 1.0)
    years_span = max((pd.to_datetime(curve["date"], utc=True).iloc[-1] - sim_start).days / 365.25, 1e-9)
    pnls = pd.to_numeric(trades["net_pnl"], errors="coerce") if not trades.empty else pd.Series(dtype=float)
    wins = pnls[pnls > 0]
    top3 = abs(float(pnls.nlargest(3).sum()) / float(pnls.sum())) * 100.0 if len(pnls) and float(pnls.sum()) != 0 else 0.0
    y_count, y_pos, worst_year, best_year_share = yearly_stats(trades)
    trades_per_year = len(trades) / years_span
    pf = profit_factor(pnls) if len(pnls) else float("nan")
    dd = 100.0 * max_drawdown(equity)
    score = score_row(trades_per_year, pf, dd, total_ret, y_pos, y_count, best_year_share)
    early = trade_stats_between(trades, "2018-01-01", "2021-12-31")
    middle = trade_stats_between(trades, "2022-01-01", "2024-12-31")
    recent = trade_stats_between(trades, "2025-01-01", "2026-12-31")

    return {
        "name": variant.name,
        "lookback": variant.lookback,
        "buffer": variant.buffer_bps,
        "hold": variant.hold_days,
        "filter": variant.trend_filter,
        "max_breakout": variant.max_breakout_bps,
        "max_gap": variant.max_gap_bps,
        "vol_target": variant.vol_target,
        "cap": variant.max_alloc,
        "trades": int(len(trades)),
        "tpy": trades_per_year,
        "ret": 100.0 * total_ret,
        "apr": 100.0 * total_ret / years_span,
        "dd": dd,
        "pf": pf,
        "win": 100.0 * len(wins) / len(pnls) if len(pnls) else float("nan"),
        "top3": top3,
        "pos_years": y_pos,
        "years": y_count,
        "worst_year": worst_year,
        "best_year_share": 100.0 * best_year_share,
        "recent_pnl": pnl_from(trades, "2025-01-01"),
        "recent_trades": trade_count_from(trades, "2025-01-01"),
        "early": early,
        "middle": middle,
        "recent": recent,
        "score": score,
    }


def score_row(tpy: float, pf: float, dd: float, ret: float, pos_years: int, years: int, best_year_share: float) -> float:
    if not np.isfinite(pf) or pf < 1.15:
        return -1_000.0
    stability = pos_years / years if years else 0.0
    tpy_score = min(tpy, 60.0) * 0.35
    sparse_penalty = 4.0 * max(0.0, MIN_SCORING_TPY - tpy)
    return (
        ret
        + 8.0 * (pf - 1.0)
        + tpy_score
        + 10.0 * stability
        - 1.2 * abs(dd)
        - 8.0 * max(0.0, best_year_share - 0.45)
        - sparse_penalty
    )


def print_table(title: str, rows: list[dict[str, Any]], limit: int) -> None:
    rows = sorted(rows, key=lambda r: r["score"], reverse=True)
    print("\n" + "=" * 150)
    print(f"  {title}")
    print("=" * 150)
    print(
        f"  {'name':16} {'filt':>10} {'xmax':>5} {'gmax':>5} {'lb':>3} {'buf':>5} {'hold':>4} "
        f"{'tr':>5} {'t/y':>6} {'ret':>8} {'APR':>7} {'DD':>8} {'PF':>5} {'yrs+':>6} "
        f"{'25+ pnl':>10} {'25+ tr':>6} {'score':>8}"
    )
    print("-" * 150)
    for r in rows[:limit]:
        print(
            f"  {r['name'][:16]:16} {r['filter'][:10]:>10} {fmt_limit(r['max_breakout']):>5} {fmt_limit(r['max_gap']):>5} "
            f"{r['lookback']:>3} {r['buffer']:>5.0f} {r['hold']:>4} {r['trades']:>5} {r['tpy']:>5.1f} "
            f"{r['ret']:>7.2f}% {r['apr']:>6.2f}% "
            f"{r['dd']:>7.2f}% {fmt_pf(r['pf']):>5} {r['pos_years']:>2}/{r['years']:<2} "
            f"${r['recent_pnl']:>9.0f} {r['recent_trades']:>6} {r['score']:>8.2f}"
        )


def print_regime_split(rows: list[dict[str, Any]], limit: int) -> None:
    rows = sorted(rows, key=lambda r: r["score"], reverse=True)
    print("\n" + "=" * 150)
    print("  REGIME SPLIT FOR TOP CANDIDATES")
    print("=" * 150)
    print(
        f"  {'name':16} {'filt':>10} {'xmax':>5} {'gmax':>5} {'lb':>3} {'buf':>5} "
        f"{'18-21 tr':>8} {'18-21 pnl':>10} {'PF':>5} "
        f"{'22-24 tr':>8} {'22-24 pnl':>10} {'PF':>5} "
        f"{'25-26 tr':>8} {'25-26 pnl':>10} {'PF':>5}"
    )
    print("-" * 150)
    for r in rows[:limit]:
        early = r["early"]
        middle = r["middle"]
        recent = r["recent"]
        print(
            f"  {r['name'][:16]:16} {r['filter'][:10]:>10} {fmt_limit(r['max_breakout']):>5} {fmt_limit(r['max_gap']):>5} "
            f"{r['lookback']:>3} {r['buffer']:>5.0f} "
            f"{early['trades']:>8} ${early['pnl']:>9.0f} {fmt_pf(early['pf']):>5} "
            f"{middle['trades']:>8} ${middle['pnl']:>9.0f} {fmt_pf(middle['pf']):>5} "
            f"{recent['trades']:>8} ${recent['pnl']:>9.0f} {fmt_pf(recent['pf']):>5}"
        )


def build_sim_config(args: argparse.Namespace) -> SimConfig:
    return SimConfig(
        source=args.source,
        data_start=args.data_start,
        sim_start=pd.Timestamp(args.sim_start, tz="UTC"),
        end=args.end,
        equity=args.equity,
        include_current=False,
        cache_path=Path(args.cache_path),
        dukascopy_path=Path(args.dukascopy_path),
        refresh_cache=False,
        show_trades=0,
        write_files=False,
        out_dir=Path("btc_breakout_clean/paper_btc_breakout"),
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BTC breakout relaxation probe")
    p.add_argument("--source", choices=("dukascopy", "yfinance"), default="dukascopy")
    p.add_argument("--data-start", default="2018-01-01")
    p.add_argument("--sim-start", default="2018-01-01")
    p.add_argument("--end", default=None)
    p.add_argument("--equity", type=float, default=10_000.0)
    p.add_argument("--fee-bps", type=float, default=10.0)
    p.add_argument("--cache-path", default="btc_breakout_clean/cache/btc_usd_yfinance_daily.csv")
    p.add_argument("--dukascopy-path", default="btc_breakout_clean/cache/BTCUSD_dukascopy_h1.csv")
    p.add_argument("--buffers", default="50,100,150,200,250,300,400")
    p.add_argument("--lookbacks", default="5,10,15,20,30")
    p.add_argument("--holds", default="1,2,3,5")
    p.add_argument("--vol-targets", default="0.01,0.015")
    p.add_argument("--caps", default="0.35,0.5,0.75,1.0")
    p.add_argument("--trend-filters", default="none,ma50_slope")
    p.add_argument("--shape-lookbacks", default="10,15,20")
    p.add_argument("--shape-buffers", default="100")
    p.add_argument("--max-breakout-bps", default="none,150,175,200,225,250,300,400,500")
    p.add_argument("--max-gap-bps", default="none,0,25,50,100")
    p.add_argument("--top", type=int, default=12)
    return p.parse_args()


def main() -> None:
    args = parse_args()
    sim_cfg = build_sim_config(args)
    raw = fetch_source_data(sim_cfg)
    sim_start = pd.Timestamp(args.sim_start, tz="UTC")
    fee_bps = args.fee_bps
    equity = args.equity

    baseline = summarize_variant(raw, BASELINE, sim_start=sim_start, initial_equity=equity, fee_bps=fee_bps)
    print_table("BASELINE - most constrained rule", [baseline], 1)

    buffer_rows = [
        summarize_variant(
            raw,
            Variant(f"buffer_{int(buf)}", BASELINE.lookback, buf, BASELINE.hold_days, BASELINE.vol_target, BASELINE.max_alloc),
            sim_start=sim_start,
            initial_equity=equity,
            fee_bps=fee_bps,
        )
        for buf in parse_csv_floats(args.buffers)
    ]
    print_table("RELAX BUFFER ONLY", buffer_rows, args.top)

    lookback_rows = [
        summarize_variant(
            raw,
            Variant(f"lookback_{lb}", lb, BASELINE.buffer_bps, BASELINE.hold_days, BASELINE.vol_target, BASELINE.max_alloc),
            sim_start=sim_start,
            initial_equity=equity,
            fee_bps=fee_bps,
        )
        for lb in parse_csv_ints(args.lookbacks)
    ]
    print_table("RELAX LOOKBACK ONLY", lookback_rows, args.top)

    hold_rows = [
        summarize_variant(
            raw,
            Variant(f"hold_{h}", BASELINE.lookback, BASELINE.buffer_bps, h, BASELINE.vol_target, BASELINE.max_alloc),
            sim_start=sim_start,
            initial_equity=equity,
            fee_bps=fee_bps,
        )
        for h in parse_csv_ints(args.holds)
    ]
    print_table("RELAX HOLD ONLY", hold_rows, args.top)

    size_rows = [
        summarize_variant(
            raw,
            Variant(f"vt{vt:.3f}_cap{cap:.2f}", BASELINE.lookback, BASELINE.buffer_bps, BASELINE.hold_days, vt, cap),
            sim_start=sim_start,
            initial_equity=equity,
            fee_bps=fee_bps,
        )
        for vt in parse_csv_floats(args.vol_targets)
        for cap in parse_csv_floats(args.caps)
    ]
    print_table("RELAX SIZING ONLY", size_rows, args.top)

    combo_rows = [
        summarize_variant(
            raw,
            Variant(f"lb{lb}_b{int(buf)}", lb, buf, BASELINE.hold_days, BASELINE.vol_target, BASELINE.max_alloc),
            sim_start=sim_start,
            initial_equity=equity,
            fee_bps=fee_bps,
        )
        for lb in parse_csv_ints(args.lookbacks)
        for buf in parse_csv_floats(args.buffers)
    ]
    print_table("COMBINED ENTRY RELAXATION - lookback x buffer", combo_rows, args.top)

    trend_rows = [
        summarize_variant(
            raw,
            Variant(
                f"lb{lb}_b{int(buf)}_{flt}",
                lb,
                buf,
                BASELINE.hold_days,
                BASELINE.vol_target,
                BASELINE.max_alloc,
                flt,
            ),
            sim_start=sim_start,
            initial_equity=equity,
            fee_bps=fee_bps,
        )
        for lb in parse_csv_ints(args.lookbacks)
        for buf in parse_csv_floats(args.buffers)
        for flt in [x.strip() for x in args.trend_filters.split(",") if x.strip()]
    ]
    print_table("RECENT-REGIME FILTER TEST - entry x trend filter", trend_rows, args.top)
    print_regime_split(trend_rows, min(args.top, 12))

    shape_rows = [
        summarize_variant(
            raw,
            Variant(
                name=f"lb{lb}_b{int(buf)}_x{fmt_limit(max_breakout)}_g{fmt_limit(max_gap)}",
                lookback=lb,
                buffer_bps=buf,
                hold_days=BASELINE.hold_days,
                vol_target=BASELINE.vol_target,
                max_alloc=BASELINE.max_alloc,
                max_breakout_bps=max_breakout,
                max_gap_bps=max_gap,
            ),
            sim_start=sim_start,
            initial_equity=equity,
            fee_bps=fee_bps,
        )
        for lb in parse_csv_ints(args.shape_lookbacks)
        for buf in parse_csv_floats(args.shape_buffers)
        for max_breakout in parse_optional_floats(args.max_breakout_bps)
        for max_gap in parse_optional_floats(args.max_gap_bps)
    ]
    print_table("BREAKOUT SHAPE FILTER TEST - exhaustion x next-open gap", shape_rows, args.top)
    print_regime_split(shape_rows, min(args.top, 12))

    best = sorted(shape_rows + trend_rows + combo_rows + buffer_rows + lookback_rows + hold_rows + size_rows, key=lambda r: r["score"], reverse=True)[0]
    print("\n" + "=" * 150)
    print("  CURRENT BEST RELAXED CANDIDATE")
    print("=" * 150)
    print(
        f"  {best['name']}: filter={best['filter']}, lookback={best['lookback']}, buffer={best['buffer']:.0f}bps, "
        f"hold={best['hold']}, max_breakout={fmt_limit(best['max_breakout'])}bps, max_gap={fmt_limit(best['max_gap'])}bps, "
        f"trades/year={best['tpy']:.1f}, PF={fmt_pf(best['pf'])}, DD={best['dd']:.2f}%, APR={best['apr']:.2f}%, "
        f"2025+ pnl=${best['recent_pnl']:.0f} on {best['recent_trades']} trades"
    )
    print("=" * 150)


if __name__ == "__main__":
    main()
