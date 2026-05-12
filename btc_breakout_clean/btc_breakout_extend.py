"""
btc_breakout_extend.py  —  Three structural improvements to the BTC breakout simulator
═══════════════════════════════════════════════════════════════════════════════════════
Imports the existing simulator (btc_breakout_paper_sim.py) and adds:

  1. MULTI-DAY HOLD SWEEP
     Extends the hold period from same-day close (N=1) to N=2,3,5,7 days.
     Each day after entry, a trailing stop can optionally protect gains.
     Key question: does holding longer recover the post-2022 edge, or does
     it just increase drawdown by staying exposed through reversals?
     Reports: net, PF, WR, MaxDD, avg-hold-days per N setting.

  2. TREND FILTER COMPARISON
     Gates entries on Close > 200d SMA (bull only), Close < 200d SMA (bear
     only), or unfiltered. For a momentum-breakout strategy on an asset with
     strong trend cycles, this should be the single highest-impact filter.
     Key question: are the 2023+ losses bear-market fakeouts that a trend
     filter would have avoided?

  3. IS / OOS ANALYSIS  (2018-2022 IS  →  2023-2026 OOS)
     Same clean split as the AUDJPY work. Runs the full sweep
     (hold × filter combinations) on IS only, picks the best IS cell,
     then reports OOS performance for that cell and its neighbors.
     Prevents choosing the multi-day N that happened to fit 2023+.

Usage:
    python btc_breakout_clean/btc_breakout_extend.py
    python btc_breakout_clean/btc_breakout_extend.py --only hold
    python btc_breakout_clean/btc_breakout_extend.py --only trend
    python btc_breakout_clean/btc_breakout_extend.py --only isoos
    python btc_breakout_clean/btc_breakout_extend.py --source yfinance

Reads OHLC from the same cache the existing simulator writes.
Not investment advice.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

# ── Import parent simulator ───────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

try:
    import btc_breakout_paper_sim as _sim
except ModuleNotFoundError:
    # fallback: look one directory up
    sys.path.insert(0, str(_HERE.parent / "btc_breakout_clean"))
    import btc_breakout_paper_sim as _sim

# ── Constants ─────────────────────────────────────────────────────────────
IS_END   = pd.Timestamp("2022-12-31", tz="UTC")
OOS_START = pd.Timestamp("2023-01-01", tz="UTC")

HOLD_PERIODS     = [1, 2, 3, 5, 7]      # days to test
TRAIL_STOP_ATR   = 2.0                   # trail stop = 2× 14d daily ATR (0 = disabled)
SMA_LONG         = 200                   # trend filter window

DEFAULT_LOOKBACK  = 15
DEFAULT_BUFFER    = 100.0
DEFAULT_MAX_BREAK = 225.0
DEFAULT_FEE_BPS   = 10.0
DEFAULT_VOL_TGT   = 0.015
DEFAULT_MAX_ALLOC = 0.75
INITIAL_EQUITY    = 10_000.0

W = 92


# ── Helpers ───────────────────────────────────────────────────────────────

def _sep(c="─", n=W): print(c * n)
def _hdr(t, c="═"):   print(); _sep(c); print(f"  {t}"); _sep(c)
def _sub(t):           print(); _sep(); print(f"  {t}"); _sep()

def _pf(v): return f"{v:.2f}" if np.isfinite(v) else " n/a"
def _sh(v): return f"{v:.2f}" if np.isfinite(v) else " n/a"
def _pc(v): return f"{v:.1f}%" if np.isfinite(v) else " n/a"


def _profit_factor(pnls: pd.Series) -> float:
    wins = float(pnls[pnls > 0].sum())
    loss = float(pnls[pnls < 0].sum())
    return wins / abs(loss) if loss < 0 else float("nan")


def _max_dd(equity: pd.Series) -> float:
    dd = equity / equity.cummax() - 1.0
    return float(dd.min()) if len(dd) else float("nan")


# ── Extended indicator builder ────────────────────────────────────────────

def add_indicators_extended(df: pd.DataFrame, lookback: int, buffer_bps: float,
                             max_break_bps: float | None) -> pd.DataFrame:
    """All indicators needed for multi-day hold and trend filter."""
    out = df.copy()
    out["ret"]       = out["close"].pct_change()
    out["vol20"]     = out["ret"].rolling(20).std()
    out["sma200"]    = out["close"].rolling(SMA_LONG).mean()
    out["atr14"]     = out["close"].pct_change().abs().rolling(14).mean()   # daily ATR proxy

    out["prior_high"]    = out["close"].rolling(lookback).max().shift(1)
    out["breakout_bps"]  = 10_000.0 * (out["close"] / out["prior_high"] - 1.0)
    out["raw_signal"]    = out["close"] > out["prior_high"] * (1.0 + buffer_bps / 10_000.0)
    if max_break_bps is not None:
        out["raw_signal"] &= out["breakout_bps"] <= max_break_bps
    out["raw_signal"] = out["raw_signal"].fillna(False)

    # Trend regime flags (computed at signal close, used for next-day entry gate)
    out["bull"] = out["close"] > out["sma200"]
    out["bear"] = out["close"] < out["sma200"]
    return out


# ── Multi-day simulator ───────────────────────────────────────────────────

def simulate_multi_day(
    df: pd.DataFrame,
    *,
    hold_days: int,
    trend_mode: str,          # "all" | "bull_only" | "bear_only"
    fee_bps: float,
    vol_target: float,
    max_alloc: float,
    sim_start: pd.Timestamp,
    trail_stop_atr: float = 0.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Multi-day hold simulator.

    hold_days: 1 = same-day (replicates original), N > 1 = hold N calendar
               trading days, exit at close on day N.
    trail_stop_atr: if > 0, exit early if close drops > trail_stop_atr × atr14
                    below the highest close seen since entry.
    trend_mode: "all"       — no filter
                "bull_only" — entry only when Close > SMA200 on signal day
                "bear_only" — entry only when Close < SMA200 on signal day
    """
    fee      = fee_bps / 10_000.0
    equity   = float(INITIAL_EQUITY)
    trades:  list[dict[str, Any]] = []
    curve:   list[dict[str, Any]] = []

    # State: are we in a position?
    in_pos       = False
    hold_count   = 0
    pos_entry_px = 0.0
    pos_size     = 0.0
    pos_notional = 0.0
    pos_entry_i  = 0
    peak_close   = 0.0

    for i in range(1, len(df)):
        row       = df.iloc[i]
        entry_day = df.index[i]
        if entry_day < sim_start:
            continue

        day_pnl = 0.0
        action  = "HOLD" if in_pos else "NO_SIGNAL"

        # ── Manage open position ─────────────────────────────────────────
        if in_pos:
            hold_count += 1
            cur_close   = float(row["close"])
            atr         = float(row["atr14"])
            peak_close  = max(peak_close, cur_close)

            trail_hit = (
                trail_stop_atr > 0
                and np.isfinite(atr) and atr > 0
                and cur_close < peak_close * (1.0 - trail_stop_atr * atr)
            )
            should_exit = hold_count >= hold_days or trail_hit

            if should_exit:
                exit_px   = cur_close
                exit_not  = pos_size * exit_px
                fees_exit = exit_not * fee
                gross     = exit_not - pos_notional
                day_pnl   = gross - fees_exit    # entry fees already deducted
                equity   += day_pnl
                action    = "EXIT" + ("_TRAIL" if trail_hit else "")
                trades.append({
                    "entry_date":    df.index[pos_entry_i].isoformat(),
                    "exit_date":     entry_day.isoformat(),
                    "hold_days":     hold_count,
                    "entry_px":      pos_entry_px,
                    "exit_px":       exit_px,
                    "size_frac":     pos_size * pos_entry_px / INITIAL_EQUITY,
                    "gross_pnl":     gross,
                    "fees_exit":     fees_exit,
                    "net_pnl":       day_pnl,
                    "equity_after":  equity,
                    "trail_stop":    trail_hit,
                })
                in_pos = False

        # ── Check for new entry (only if flat) ───────────────────────────
        if not in_pos:
            sig_i      = i - 1
            signal_row = df.iloc[sig_i]
            signal     = bool(signal_row["raw_signal"])

            if signal:
                # Apply trend filter
                bull = bool(signal_row["bull"])
                if trend_mode == "bull_only" and not bull:
                    signal = False
                elif trend_mode == "bear_only" and bull:
                    signal = False

            if signal:
                rv = float(signal_row["vol20"])
                size_frac = (
                    min(max_alloc, vol_target / rv)
                    if np.isfinite(rv) and rv > 0 else 0.0
                )
                if size_frac > 0:
                    entry_px   = float(row["open"])
                    notional   = INITIAL_EQUITY * size_frac
                    qty        = notional / entry_px
                    entry_fees = notional * fee
                    # Deduct entry fees immediately from equity
                    equity    -= entry_fees

                    in_pos       = True
                    hold_count   = 0
                    pos_entry_px = entry_px
                    pos_size     = qty
                    pos_notional = notional
                    pos_entry_i  = i
                    peak_close   = float(row["close"])
                    action       = "ENTRY"
                    day_pnl      = -entry_fees   # cost recognised on entry day

        curve.append({
            "date":     entry_day.isoformat(),
            "equity":   equity,
            "daily_pnl": day_pnl,
            "action":   action,
            "in_pos":   in_pos,
        })

    # Close any open position at end of data
    if in_pos and len(df) > 0:
        last_row  = df.iloc[-1]
        exit_px   = float(last_row["close"])
        exit_not  = pos_size * exit_px
        fees_exit = exit_not * fee
        gross     = exit_not - pos_notional
        day_pnl   = gross - fees_exit
        equity   += day_pnl
        trades.append({
            "entry_date":   df.index[pos_entry_i].isoformat(),
            "exit_date":    df.index[-1].isoformat(),
            "hold_days":    hold_count,
            "entry_px":     pos_entry_px,
            "exit_px":      exit_px,
            "size_frac":    pos_size * pos_entry_px / INITIAL_EQUITY,
            "gross_pnl":    gross,
            "fees_exit":    fees_exit,
            "net_pnl":      day_pnl,
            "equity_after": equity,
            "trail_stop":   False,
        })
        curve[-1]["equity"]    = equity
        curve[-1]["daily_pnl"] = day_pnl

    return pd.DataFrame(trades), pd.DataFrame(curve)


def _stats(trades_df: pd.DataFrame, curve_df: pd.DataFrame) -> dict[str, Any]:
    if trades_df.empty:
        return {k: float("nan") for k in ("n","net","pf","wr","dd","avg_hold")}
    pnl  = pd.to_numeric(trades_df["net_pnl"], errors="coerce").dropna()
    wins = pnl[pnl > 0]
    eq   = pd.to_numeric(curve_df["equity"], errors="coerce") if not curve_df.empty else pd.Series([INITIAL_EQUITY])
    hold = pd.to_numeric(trades_df["hold_days"], errors="coerce").mean() if "hold_days" in trades_df.columns else float("nan")
    return {
        "n":        int(len(pnl)),
        "net":      float(pnl.sum()),
        "pf":       _profit_factor(pnl),
        "wr":       100.0 * len(wins) / len(pnl) if len(pnl) else float("nan"),
        "dd":       100.0 * _max_dd(eq),
        "avg_hold": float(hold),
    }


def _yearly(trades_df: pd.DataFrame) -> dict[int, dict]:
    if trades_df.empty:
        return {}
    t = trades_df.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    t["net_pnl"]    = pd.to_numeric(t["net_pnl"], errors="coerce")
    result = {}
    for yr, grp in t.groupby(t["entry_date"].dt.year):
        pnl = grp["net_pnl"].dropna()
        wins = pnl[pnl > 0]
        result[int(yr)] = {
            "n":   int(len(pnl)),
            "net": float(pnl.sum()),
            "pf":  _profit_factor(pnl),
            "wr":  100.0 * len(wins) / len(pnl) if len(pnl) else float("nan"),
        }
    return result


# ══════════════════════════════════════════════════════════════════════════
# 1.  MULTI-DAY HOLD SWEEP
# ══════════════════════════════════════════════════════════════════════════

def analysis_hold_sweep(df: pd.DataFrame, args: argparse.Namespace) -> None:
    _hdr("1.  MULTI-DAY HOLD SWEEP  (trend_mode=all, no trail stop)")
    print(f"  Hold periods tested: {HOLD_PERIODS} trading days")
    print(f"  Signal: {DEFAULT_LOOKBACK}d breakout + {DEFAULT_BUFFER:.0f}bps buffer, max {DEFAULT_MAX_BREAK:.0f}bps")
    print(f"  Sizing: min({DEFAULT_MAX_ALLOC:.2f}x, {DEFAULT_VOL_TGT:.2%}/20d vol)  fee={DEFAULT_FEE_BPS:.0f}bps/side")
    print(f"  No compounding.  No trend filter.  No trail stop.")

    sim_start = pd.Timestamp(args.sim_start, tz="UTC")

    print()
    print(f"  {'Hold':>5}  {'n':>5}  {'Net$':>10}  {'PF':>6}  {'WR%':>6}  {'MaxDD%':>8}  {'AvgHold':>8}")
    _sep()

    hold_results: dict[int, dict] = {}
    for hold in HOLD_PERIODS:
        tr, cv = simulate_multi_day(
            df, hold_days=hold, trend_mode="all",
            fee_bps=DEFAULT_FEE_BPS, vol_target=DEFAULT_VOL_TGT,
            max_alloc=DEFAULT_MAX_ALLOC, sim_start=sim_start,
        )
        st = _stats(tr, cv)
        hold_results[hold] = {"st": st, "tr": tr}
        tag = "  ← baseline" if hold == 1 else ""
        print(
            f"  {hold:>5}  {st['n']:>5}  ${st['net']:>8,.0f}  "
            f"{_pf(st['pf']):>6}  {_pc(st['wr']):>6}  "
            f"{st['dd']:>7.2f}%  {st['avg_hold']:>7.1f}{tag}"
        )

    # Yearly grid for each hold period
    _sub("Yearly net$ by hold period")
    all_years: list[int] = []
    yearly_by_hold: dict[int, dict[int, dict]] = {}
    for hold in HOLD_PERIODS:
        tr = hold_results[hold]["tr"]
        yd = _yearly(tr)
        yearly_by_hold[hold] = yd
        all_years += list(yd.keys())
    all_years = sorted(set(all_years))

    print(f"  {'Year':<6}", end="")
    for h in HOLD_PERIODS:
        print(f"  {'hold='+str(h):>12}", end="")
    print()
    _sep()
    for yr in all_years:
        tag = " ← OOS" if yr >= OOS_START.year else ""
        print(f"  {yr:<6}", end="")
        for h in HOLD_PERIODS:
            v = yearly_by_hold[h].get(yr, {}).get("net", float("nan"))
            print(f"  {'${:>9,.0f}'.format(v) if np.isfinite(v) else '         n/a':>12}", end="")
        print(tag)

    # Trail stop comparison for best hold
    nets   = {h: hold_results[h]["st"]["net"] for h in HOLD_PERIODS}
    best_h = max(nets, key=nets.get)
    print()
    print(f"  Best hold period by net$: hold={best_h}  (net=${nets[best_h]:,.0f})")

    _sub(f"Trail stop sensitivity at hold={best_h}  (TRAIL_STOP_ATR multiples)")
    trail_vals = [0.0, 1.5, 2.0, 3.0]
    print(f"  {'Trail':>7}  {'n':>5}  {'Net$':>10}  {'PF':>6}  {'WR%':>6}  {'MaxDD%':>8}")
    _sep()
    for trail in trail_vals:
        tr, cv = simulate_multi_day(
            df, hold_days=best_h, trend_mode="all",
            fee_bps=DEFAULT_FEE_BPS, vol_target=DEFAULT_VOL_TGT,
            max_alloc=DEFAULT_MAX_ALLOC, sim_start=sim_start,
            trail_stop_atr=trail,
        )
        st = _stats(tr, cv)
        tag = "  (no trail)" if trail == 0 else ""
        print(
            f"  {trail:>6.1f}×  {st['n']:>5}  ${st['net']:>8,.0f}  "
            f"{_pf(st['pf']):>6}  {_pc(st['wr']):>6}  {st['dd']:>7.2f}%{tag}"
        )

    print()
    print("  ▶  Decision criteria:")
    print("     Hold > 1 net >> hold=1 net  AND  MaxDD < hold=1 MaxDD  → extend hold")
    print("     Hold > 1 net ≈ hold=1 net   OR   MaxDD increases        → same-day exit optimal")
    print("     Trail stop at best hold cuts DD without cutting net       → add trail stop")


# ══════════════════════════════════════════════════════════════════════════
# 2.  TREND FILTER COMPARISON
# ══════════════════════════════════════════════════════════════════════════

def analysis_trend_filter(df: pd.DataFrame, args: argparse.Namespace) -> None:
    _hdr("2.  TREND FILTER COMPARISON  (200d SMA gate)")
    print(f"  Modes: all (no filter) | bull_only (close > SMA200) | bear_only (close < SMA200)")
    print(f"  Hold periods: {HOLD_PERIODS}  (full sweep)")
    print(f"  Covers full simulation period {args.sim_start} → {df.index[-1].date()}")

    sim_start = pd.Timestamp(args.sim_start, tz="UTC")

    modes = ["all", "bull_only", "bear_only"]
    mode_labels = {"all": "No filter", "bull_only": "Bull only (>SMA200)", "bear_only": "Bear only (<SMA200)"}

    # Summary table: mode × hold
    print()
    print(f"  {'Mode':<22}  {'Hold':>5}  {'n':>5}  {'Net$':>10}  {'PF':>6}  {'WR%':>6}  {'MaxDD%':>8}")
    _sep()

    best_results: dict[str, dict] = {}    # mode → best hold result
    all_results:  dict[tuple[str,int], dict] = {}

    for mode in modes:
        mode_best_net = float("-inf")
        for hold in HOLD_PERIODS:
            tr, cv = simulate_multi_day(
                df, hold_days=hold, trend_mode=mode,
                fee_bps=DEFAULT_FEE_BPS, vol_target=DEFAULT_VOL_TGT,
                max_alloc=DEFAULT_MAX_ALLOC, sim_start=sim_start,
            )
            st = _stats(tr, cv)
            all_results[(mode, hold)] = {"st": st, "tr": tr}
            if np.isfinite(st["net"]) and st["net"] > mode_best_net:
                mode_best_net = st["net"]
                best_results[mode] = {"st": st, "tr": tr, "hold": hold}
            sep = "  ←" if hold == 1 else ""
            print(
                f"  {mode_labels[mode]:<22}  {hold:>5}  {st['n']:>5}  "
                f"${st['net']:>8,.0f}  {_pf(st['pf']):>6}  "
                f"{_pc(st['wr']):>6}  {st['dd']:>7.2f}%{sep}"
            )
        print()

    # Yearly breakdown at best hold per mode
    _sub("Yearly net$ at best hold per mode")
    all_years: list[int] = []
    for mode in modes:
        yd = _yearly(best_results[mode]["tr"])
        all_years += list(yd.keys())
    all_years = sorted(set(all_years))

    print(f"  {'Year':<6}", end="")
    for mode in modes:
        h = best_results[mode]["hold"]
        print(f"  {'No filter h='+str(h) if mode=='all' else ('Bull h='+str(h) if mode=='bull_only' else 'Bear h='+str(h)):>16}", end="")
    print()
    _sep()
    for yr in all_years:
        tag = " ← OOS" if yr >= OOS_START.year else ""
        print(f"  {yr:<6}", end="")
        for mode in modes:
            yd = _yearly(best_results[mode]["tr"])
            v  = yd.get(yr, {}).get("net", float("nan"))
            print(f"  {'${:>12,.0f}'.format(v) if np.isfinite(v) else '           n/a':>16}", end="")
        print(tag)

    # SMA coverage diagnostic
    _sub("SMA200 regime coverage")
    sma_ok = df["sma200"].notna()
    bull_pct = 100 * (df.loc[sma_ok, "bull"]).mean()
    bear_pct = 100 - bull_pct
    print(f"  Full period:  {bull_pct:.1f}% bull (>SMA200)  {bear_pct:.1f}% bear (<SMA200)")
    for period, (t0, t1) in [
        ("IS 2018–2022", (pd.Timestamp("2018-01-01", tz="UTC"), IS_END)),
        ("OOS 2023–now", (OOS_START, df.index[-1])),
    ]:
        seg = df.loc[(df.index >= t0) & (df.index <= t1) & sma_ok]
        if seg.empty:
            continue
        bp = 100 * seg["bull"].mean()
        print(f"  {period}:  {bp:.1f}% bull  {100-bp:.1f}% bear")

    print()
    print("  ▶  Decision criteria:")
    print("     Bull-only net >> All-filter net  →  strong trend filter; use bull_only")
    print("     Bear-only positive              →  consider hedging / short bias in bear")
    print("     Both filters degrade PF         →  signal doesn't depend on trend; use all")
    print("     2023 bear losses disappear      →  post-2022 weakness IS the trend-regime issue")


# ══════════════════════════════════════════════════════════════════════════
# 3.  IS / OOS ANALYSIS
# ══════════════════════════════════════════════════════════════════════════

def analysis_is_oos(df: pd.DataFrame, args: argparse.Namespace) -> None:
    _hdr(f"3.  IS / OOS ANALYSIS  (IS: {args.sim_start} → {IS_END.date()}  |  OOS: {OOS_START.date()} → {df.index[-1].date()})")
    print("  Grid: hold_days × trend_mode — optimized on IS only, reported on OOS.")
    print("  Neighbor stability: IS best cell ±1 hold step reported on OOS.")

    is_df  = df.loc[df.index <= IS_END]
    oos_df = df.loc[df.index >= OOS_START]
    sim_start_is  = pd.Timestamp(args.sim_start, tz="UTC")
    sim_start_oos = OOS_START

    modes = ["all", "bull_only"]    # bear_only rarely profitable; skip in grid
    mode_labels = {"all": "all", "bull_only": "bull"}

    # ── IS grid ──────────────────────────────────────────────────────────
    _sub("IS grid  (2018–2022)")
    print(f"  {'Mode':<10}  {'Hold':>5}  {'IS n':>6}  {'IS Net$':>10}  {'IS PF':>7}  {'IS WR%':>7}  {'IS DD%':>8}")
    _sep()

    grid_results: dict[tuple[str, int], dict] = {}
    for mode in modes:
        for hold in HOLD_PERIODS:
            tr, cv = simulate_multi_day(
                is_df, hold_days=hold, trend_mode=mode,
                fee_bps=DEFAULT_FEE_BPS, vol_target=DEFAULT_VOL_TGT,
                max_alloc=DEFAULT_MAX_ALLOC, sim_start=sim_start_is,
            )
            st = _stats(tr, cv)
            grid_results[(mode, hold)] = {"st": st, "tr": tr}
            print(
                f"  {mode_labels[mode]:<10}  {hold:>5}  {st['n']:>6}  "
                f"${st['net']:>8,.0f}  {_pf(st['pf']):>7}  "
                f"{_pc(st['wr']):>7}  {st['dd']:>7.2f}%"
            )
        print()

    # Best IS cell
    best_key = max(
        grid_results,
        key=lambda k: (
            grid_results[k]["st"]["net"] if np.isfinite(grid_results[k]["st"]["net"]) else float("-inf")
        ),
    )
    best_mode, best_hold = best_key
    best_is = grid_results[best_key]["st"]
    print(f"  IS optimum: mode={best_mode}  hold={best_hold}  IS net=${best_is['net']:,.0f}  PF={_pf(best_is['pf'])}")

    # ── OOS at best IS gates ──────────────────────────────────────────────
    _sub("OOS results  (2023–2026)  — IS-selected gates applied forward")
    print(f"  {'Mode':<10}  {'Hold':>5}  {'OOS n':>6}  {'OOS Net$':>10}  {'OOS PF':>8}  "
          f"{'OOS WR%':>8}  {'OOS DD%':>8}  {'ex-2024 PF':>11}")
    _sep()

    for mode in modes:
        for hold in HOLD_PERIODS:
            tr_oos, cv_oos = simulate_multi_day(
                oos_df, hold_days=hold, trend_mode=mode,
                fee_bps=DEFAULT_FEE_BPS, vol_target=DEFAULT_VOL_TGT,
                max_alloc=DEFAULT_MAX_ALLOC, sim_start=sim_start_oos,
            )
            st_oos = _stats(tr_oos, cv_oos)

            # ex-2024
            oos_ex = oos_df[oos_df.index.year != 2024]
            if not oos_ex.empty and len(oos_ex) > 100:
                tr_ex, cv_ex = simulate_multi_day(
                    oos_ex, hold_days=hold, trend_mode=mode,
                    fee_bps=DEFAULT_FEE_BPS, vol_target=DEFAULT_VOL_TGT,
                    max_alloc=DEFAULT_MAX_ALLOC, sim_start=sim_start_oos,
                )
                st_ex = _stats(tr_ex, cv_ex)
                pf_ex = st_ex["pf"]
            else:
                pf_ex = float("nan")

            tag = "  ← IS best" if (mode, hold) == best_key else ""
            print(
                f"  {mode_labels[mode]:<10}  {hold:>5}  {st_oos['n']:>6}  "
                f"${st_oos['net']:>8,.0f}  {_pf(st_oos['pf']):>8}  "
                f"{_pc(st_oos['wr']):>8}  {st_oos['dd']:>7.2f}%  "
                f"{_pf(pf_ex):>11}{tag}"
            )
        print()

    # ── Yearly breakdown: IS vs OOS at best cell ──────────────────────────
    _sub(f"IS best cell ({best_mode} hold={best_hold}) — yearly Δequity IS + OOS")
    tr_oos_best, _ = simulate_multi_day(
        oos_df, hold_days=best_hold, trend_mode=best_mode,
        fee_bps=DEFAULT_FEE_BPS, vol_target=DEFAULT_VOL_TGT,
        max_alloc=DEFAULT_MAX_ALLOC, sim_start=sim_start_oos,
    )
    is_yearly  = _yearly(grid_results[best_key]["tr"])
    oos_yearly = _yearly(tr_oos_best)
    all_years  = sorted(set(is_yearly) | set(oos_yearly))

    print(f"  {'Year':<6}  {'Period':<8}  {'n':>5}  {'Net$':>10}  {'PF':>6}  {'WR%':>6}")
    _sep()
    for yr in all_years:
        if yr in is_yearly:
            r = is_yearly[yr]
            print(f"  {yr:<6}  {'IS':<8}  {r['n']:>5}  ${r['net']:>8,.0f}  {_pf(r['pf']):>6}  {_pc(r['wr']):>6}")
        if yr in oos_yearly:
            r = oos_yearly[yr]
            print(f"  {yr:<6}  {'OOS':<8}  {r['n']:>5}  ${r['net']:>8,.0f}  {_pf(r['pf']):>6}  {_pc(r['wr']):>6}")

    print()
    print("  ▶  Decision criteria:")
    print("     OOS ex-2024 PF > 1.40  AND  OOS PF stable across hold values  → deploy")
    print("     OOS ex-2024 PF < 1.20                                          → paper only")
    print("     Bull-only OOS > all-filter OOS                                  → add SMA gate")
    print("     OOS DD much higher than IS DD                                   → reduce size")


# ══════════════════════════════════════════════════════════════════════════
# Data loading
# ══════════════════════════════════════════════════════════════════════════

def _load_df(args: argparse.Namespace) -> pd.DataFrame:
    """Load and prepare OHLC with extended indicators."""
    source = args.source if args.source != "compare" else "dukascopy"

    sim_cfg = _sim.SimConfig(
        source=source,
        data_start=args.data_start,
        sim_start=pd.Timestamp(args.sim_start, tz="UTC"),
        end=args.end,
        equity=INITIAL_EQUITY,
        include_current=args.include_current,
        cache_path=Path(args.cache_path),
        dukascopy_path=Path(args.dukascopy_path),
        refresh_cache=args.refresh_cache,
        show_trades=0,
        write_files=False,
        out_dir=Path("."),
    )
    raw = _sim.fetch_source_data(sim_cfg)
    raw = _sim.normalize_ohlc(raw)
    return add_indicators_extended(
        raw,
        lookback=args.lookback,
        buffer_bps=args.buffer_bps,
        max_break_bps=args.max_breakout_bps if args.max_breakout_bps > 0 else None,
    )


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

def main() -> None:
    p = argparse.ArgumentParser(description="BTC breakout extension analysis")
    p.add_argument("--source", choices=("yfinance","dukascopy","compare"), default="dukascopy")
    p.add_argument("--data-start",        default="2018-01-01")
    p.add_argument("--sim-start",         default="2018-01-01")
    p.add_argument("--end",               default=None)
    p.add_argument("--lookback",          type=int,   default=DEFAULT_LOOKBACK)
    p.add_argument("--buffer-bps",        type=float, default=DEFAULT_BUFFER)
    p.add_argument("--max-breakout-bps",  type=float, default=DEFAULT_MAX_BREAK)
    p.add_argument("--fee-bps",           type=float, default=DEFAULT_FEE_BPS)
    p.add_argument("--vol-target",        type=float, default=DEFAULT_VOL_TGT)
    p.add_argument("--max-alloc",         type=float, default=DEFAULT_MAX_ALLOC)
    p.add_argument("--include-current",   action="store_true")
    p.add_argument("--cache-path",        default="btc_breakout_clean/cache/btc_usd_yfinance_daily.csv")
    p.add_argument("--dukascopy-path",    default="btc_breakout_clean/cache/BTCUSD_dukascopy_h1.csv")
    p.add_argument("--refresh-cache",     action="store_true")
    p.add_argument("--only", choices=["hold","trend","isoos"])
    args = p.parse_args()

    print("=" * W)
    print("  BTC BREAKOUT EXTENSION  —  hold sweep · trend filter · IS/OOS")
    print(f"  Source: {args.source}  |  Lookback={args.lookback}d  Buffer={args.buffer_bps:.0f}bps  MaxBreak={args.max_breakout_bps:.0f}bps")
    print(f"  IS: {args.sim_start} → {IS_END.date()}   OOS: {OOS_START.date()} → present")
    print("=" * W)

    print(f"\n  Loading data ({args.source}) ...", flush=True)
    df = _load_df(args)
    print(f"  Loaded {len(df):,} daily bars  {df.index[0].date()} → {df.index[-1].date()}")

    run_all = args.only is None

    if run_all or args.only == "hold":
        analysis_hold_sweep(df, args)

    if run_all or args.only == "trend":
        analysis_trend_filter(df, args)

    if run_all or args.only == "isoos":
        analysis_is_oos(df, args)

    print()
    _sep("═")
    print("  EXTENSION ANALYSIS COMPLETE")
    print()
    print("  Deployment decision rubric:")
    print("  ┌─ OOS ex-2024 PF > 1.40 at IS-selected gates  ──────── YES → paper trade")
    print("  │   AND trend filter improves OOS PF                           at ×0.5 size, 6 months")
    print("  ├─ OOS ex-2024 PF 1.20–1.40                    ──── borderline → extend paper, add pairs")
    print("  └─ OOS ex-2024 PF < 1.20                       ──── STOP → redesign signal")
    print("       Next signal candidates: volume confirmation, open gap filter,")
    print("       multi-asset (ETH/SOL co-breakout), or higher-timeframe trend entry")
    _sep("═")


if __name__ == "__main__":
    main()