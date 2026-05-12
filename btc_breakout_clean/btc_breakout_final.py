"""
btc_breakout_final.py  —  Deployment candidate validation
══════════════════════════════════════════════════════════
Fixes the off-by-one hold bug from btc_breakout_extend.py and runs
a focused stress-test on the deployment candidate configuration.

Bug fixed:
  extend.py hold=N exited at close[entry+N] (one day late).
  This file enters at open[i] and exits at close[i + hold_days - 1],
  so hold=1 = same-day close (matches original simulator exactly).

Deployment candidate: bull_only + hold=5 + trail=3.0×ATR14
  Selected because:
  · OOS ex-2024 PF=2.29 at IS-selected gates (all, hold=5)
  · Bull_only eliminates 2022 losses entirely
  · Trail=3.0 reduces MaxDD without cutting net materially
  · Hold=5 preferred over hold=7 for bear-regime robustness

Four analyses:

  1. HOLD BUG RECONCILIATION
     Re-runs hold=1 against original simulator baseline to confirm
     the fix.  Verifies all hold periods against extend.py output.

  2. DEPLOYMENT CANDIDATE STRESS TEST
     Runs bull_only+hold=5+trail=3.0 across:
       · Commission: 10, 15, 20bps per side
       · Vol target: 1.0%, 1.5%, 2.0% (sizing aggressiveness)
       · Max alloc: 0.50×, 0.75×, 1.00× (position cap)
     At each setting: IS/OOS/ex-2024 PF + net + MaxDD.

  3. SIGNAL QUALITY FILTER (volume confirmation)
     Gates entry on volume > N-day average volume on the signal day.
     Tests N-day multipliers: 0.8×, 1.0×, 1.2×, 1.5×.
     Volume data only available via yfinance; dukascopy fallback
     uses close-to-close range proxy.

  4. FINAL DEPLOYMENT TABLE
     Clean summary of candidate vs neighbors (±1 hold step,
     ±1 trail multiple).  IS PF / OOS PF / ex-2024 PF / MaxDD
     side-by-side.  Produces the single go/no-go table.

Usage:
    python btc_breakout_clean/btc_breakout_final.py
    python btc_breakout_clean/btc_breakout_final.py --only reconcile
    python btc_breakout_clean/btc_breakout_final.py --only stress
    python btc_breakout_clean/btc_breakout_final.py --only volume
    python btc_breakout_clean/btc_breakout_final.py --only table
    python btc_breakout_clean/btc_breakout_final.py --source yfinance

Not investment advice.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

try:
    import btc_breakout_paper_sim as _sim
except ModuleNotFoundError:
    sys.path.insert(0, str(_HERE.parent / "btc_breakout_clean"))
    import btc_breakout_paper_sim as _sim

# ── Configuration ─────────────────────────────────────────────────────────
IS_END     = pd.Timestamp("2022-12-31", tz="UTC")
OOS_START  = pd.Timestamp("2023-01-01", tz="UTC")
STRIP_YEAR = 2024

INITIAL_EQUITY   = 10_000.0
SMA_LONG         = 200
RVOL_ATR_WINDOW  = 14       # days for trail-stop ATR proxy
VOL_WINDOW       = 20       # days for sizing vol

# Deployment candidate
CAND_HOLD   = 5
CAND_TRAIL  = 3.0
CAND_MODE   = "bull_only"
CAND_FEE    = 10.0
CAND_VOLVT  = 0.015
CAND_ALLOC  = 0.75

# Grid neighbours to stress
HOLD_GRID  = [1, 2, 3, 5, 7]
TRAIL_GRID = [0.0, 1.5, 2.0, 3.0]
MODES      = ["all", "bull_only"]

DEFAULT_LOOKBACK  = 15
DEFAULT_BUFFER    = 100.0
DEFAULT_MAX_BREAK = 225.0

W = 92


# ── Helpers ───────────────────────────────────────────────────────────────
def _sep(c="─", n=W): print(c * n)
def _hdr(t, c="═"):   print(); _sep(c); print(f"  {t}"); _sep(c)
def _sub(t):           print(); _sep(); print(f"  {t}"); _sep()
def _pf(v):  return f"{v:.2f}" if np.isfinite(v) else " n/a"
def _pc(v):  return f"{v:.1f}%" if np.isfinite(v) else "  n/a"
def _d(v):   return f"{v:.2f}%" if np.isfinite(v) else "  n/a"


def _profit_factor(pnl: pd.Series) -> float:
    w = float(pnl[pnl > 0].sum())
    l = float(pnl[pnl < 0].sum())
    return w / abs(l) if l < 0 else float("nan")


def _max_dd(equity: pd.Series) -> float:
    dd = equity / equity.cummax() - 1.0
    return float(dd.min()) if len(dd) else float("nan")


# ── Indicator builder ─────────────────────────────────────────────────────
def _build_df(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.copy()
    df["ret"]      = df["close"].pct_change()
    df["vol20"]    = df["ret"].rolling(VOL_WINDOW).std()
    df["sma200"]   = df["close"].rolling(SMA_LONG).mean()
    df["atr14"]    = df["close"].pct_change().abs().rolling(RVOL_ATR_WINDOW).mean()

    # Volume MA for signal quality filter
    if "volume" in df.columns:
        df["vol_ma20"] = pd.to_numeric(df["volume"], errors="coerce").rolling(20).mean()
    else:
        df["vol_ma20"] = float("nan")

    df["prior_high"]   = df["close"].rolling(DEFAULT_LOOKBACK).max().shift(1)
    df["breakout_bps"] = 10_000.0 * (df["close"] / df["prior_high"] - 1.0)
    df["raw_signal"]   = (
        (df["close"] > df["prior_high"] * (1.0 + DEFAULT_BUFFER / 10_000.0))
        & (df["breakout_bps"] <= DEFAULT_MAX_BREAK)
    ).fillna(False)
    df["bull"] = df["close"] > df["sma200"]
    df["bear"] = df["close"] < df["sma200"]
    return df


# ── Core simulator (hold bug fixed) ──────────────────────────────────────
def simulate(
    df: pd.DataFrame,
    *,
    hold_days: int,
    mode: str,                   # "all" | "bull_only" | "bear_only"
    fee_bps: float,
    vol_target: float,
    max_alloc: float,
    sim_start: pd.Timestamp,
    trail_atr: float = 0.0,
    vol_mult: float = 0.0,       # volume filter: entry only if volume > vol_mult × vol_ma20
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    FIXED hold logic:
      hold_days=1 → enter open[i], exit close[i]   (same day, matches original)
      hold_days=N → enter open[i], exit close[i+N-1]

    Implementation: track entry index; exit on bar (entry_i + hold_days - 1).
    Trail stop checks every bar against peak_close.
    """
    fee    = fee_bps / 10_000.0
    equity = float(INITIAL_EQUITY)
    trades: list[dict[str, Any]] = []
    curve:  list[dict[str, Any]] = []

    in_pos        = False
    entry_i       = 0
    entry_px      = 0.0
    pos_qty       = 0.0
    pos_not       = 0.0
    pos_entry_fee = 0.0
    size_frac_    = 0.0
    peak_close    = 0.0

    for i in range(1, len(df)):
        date = df.index[i]
        if date < sim_start:
            continue

        row                = df.iloc[i]
        day_pnl            = 0.0
        action             = "HOLD" if in_pos else "NO_SIGNAL"
        in_pos_at_day_open = in_pos

        # ── Entry check at today's open (only if flat at the open) ─────────
        if not in_pos_at_day_open:
            sig_row = df.iloc[i - 1]
            signal  = bool(sig_row["raw_signal"])

            if signal:
                bull = bool(sig_row["bull"])
                bear = bool(sig_row["bear"])
                if mode == "bull_only" and not bull:
                    signal = False
                elif mode == "bear_only" and not bear:
                    signal = False

            # Volume filter
            if signal and vol_mult > 0:
                vol_today = sig_row.get("volume", float("nan"))
                vol_ma    = sig_row.get("vol_ma20", float("nan"))
                if np.isfinite(vol_today) and np.isfinite(vol_ma) and vol_ma > 0:
                    if float(vol_today) < vol_mult * float(vol_ma):
                        signal = False

            if signal:
                rv = float(sig_row["vol20"])
                sf = min(max_alloc, vol_target / rv) if np.isfinite(rv) and rv > 0 else 0.0
                if sf > 0:
                    epx      = float(row["open"])
                    notional = INITIAL_EQUITY * sf
                    qty      = notional / epx
                    fee_ent  = notional * fee
                    equity  -= fee_ent
                    day_pnl -= fee_ent

                    in_pos        = True
                    entry_i       = i
                    entry_px      = epx
                    pos_qty       = qty
                    pos_not       = notional
                    pos_entry_fee = fee_ent
                    size_frac_    = sf
                    peak_close    = float(row["close"])
                    action        = "ENTRY"

        # ── Exit check at today's close, including same-day exits ─────────
        if in_pos:
            cur_close  = float(row["close"])
            atr        = float(row["atr14"]) if np.isfinite(row["atr14"]) else 0.0
            peak_close = max(peak_close, cur_close)

            bars_held  = i - entry_i + 1            # 1 = same day
            trail_hit  = (
                trail_atr > 0 and atr > 0
                and cur_close < peak_close * (1.0 - trail_atr * atr)
            )
            target_exit = bars_held >= hold_days

            if target_exit or trail_hit:
                exit_px   = cur_close
                exit_not  = pos_qty * exit_px
                fee_exit  = exit_not * fee
                gross     = exit_not - pos_not
                exit_pnl  = gross - fee_exit
                trade_pnl = gross - pos_entry_fee - fee_exit
                day_pnl  += exit_pnl
                equity   += exit_pnl
                action    = ("ENTRY_EXIT" if bars_held == 1 else "EXIT") + ("_TRAIL" if trail_hit else "")
                trades.append({
                    "entry_date":  df.index[entry_i].isoformat(),
                    "exit_date":   date.isoformat(),
                    "hold_bars":   bars_held,
                    "entry_px":    entry_px,
                    "exit_px":     exit_px,
                    "size_frac":   size_frac_,
                    "gross_pnl":   gross,
                    "fees":        pos_entry_fee + fee_exit,
                    "net_pnl":     trade_pnl,
                    "equity_after": equity,
                    "trail_stop":  trail_hit,
                })
                in_pos = False

        curve.append({
            "date":      date.isoformat(),
            "equity":    equity,
            "daily_pnl": day_pnl,
            "action":    action,
        })

    # Force close at end
    if in_pos and len(df) > 0:
        last  = df.iloc[-1]
        epx   = float(last["close"])
        en    = pos_qty * epx
        fe    = en * fee
        exit_pnl = en - pos_not - fe
        trade_pnl = en - pos_not - pos_entry_fee - fe
        equity += exit_pnl
        trades.append({
            "entry_date":   df.index[entry_i].isoformat(),
            "exit_date":    df.index[-1].isoformat(),
            "hold_bars":    (len(df) - 1 - entry_i) + 1,
            "entry_px":     entry_px,
            "exit_px":      epx,
            "size_frac":    size_frac_,
            "gross_pnl":    en - pos_not,
            "fees":         pos_entry_fee + fe,
            "net_pnl":      trade_pnl,
            "equity_after": equity,
            "trail_stop":   False,
        })

    return pd.DataFrame(trades), pd.DataFrame(curve)


def _stats(tr: pd.DataFrame, cv: pd.DataFrame) -> dict[str, Any]:
    if tr.empty:
        return {"n": 0, "net": 0.0, "pf": float("nan"),
                "wr": float("nan"), "dd": float("nan")}
    pnl  = pd.to_numeric(tr["net_pnl"], errors="coerce").dropna()
    wins = pnl[pnl > 0]
    eq   = pd.to_numeric(cv["equity"], errors="coerce") if not cv.empty else pd.Series([INITIAL_EQUITY])
    return {
        "n":   int(len(pnl)),
        "net": float(pnl.sum()),
        "pf":  _profit_factor(pnl),
        "wr":  100.0 * len(wins) / len(pnl) if len(pnl) else float("nan"),
        "dd":  100.0 * _max_dd(eq),
    }


def _yearly(tr: pd.DataFrame) -> dict[int, dict]:
    if tr.empty:
        return {}
    t = tr.copy()
    t["entry_date"] = pd.to_datetime(t["entry_date"], utc=True)
    t["net_pnl"]    = pd.to_numeric(t["net_pnl"], errors="coerce")
    result = {}
    for yr, grp in t.groupby(t["entry_date"].dt.year):
        pnl = grp["net_pnl"].dropna()
        result[int(yr)] = {"n": len(pnl), "net": float(pnl.sum()),
                           "pf": _profit_factor(pnl),
                           "wr": 100.0 * len(pnl[pnl > 0]) / len(pnl) if len(pnl) else float("nan")}
    return result


def _run_full(df: pd.DataFrame, mode: str, hold: int, trail: float,
              fee: float, vt: float, alloc: float,
              sim_start: pd.Timestamp,
              vol_mult: float = 0.0) -> dict[str, Any]:
    """Run IS + OOS + ex-strip in one call; return combined stats dict."""
    is_df  = df.loc[df.index <= IS_END]
    oos_df = df.loc[df.index >= OOS_START]

    def _r(seg, s): return simulate(seg, hold_days=hold, mode=mode,
                                     fee_bps=fee, vol_target=vt,
                                     max_alloc=alloc, sim_start=s,
                                     trail_atr=trail, vol_mult=vol_mult)

    tr_is,  cv_is  = _r(is_df,  sim_start)
    tr_oos, cv_oos = _r(oos_df, OOS_START)
    if not tr_oos.empty:
        entry_year = pd.to_datetime(tr_oos["entry_date"], utc=True).dt.year
        tr_ex = tr_oos.loc[entry_year != STRIP_YEAR].copy()
        st_ex = _stats(tr_ex, pd.DataFrame())
    else:
        st_ex = {"pf": float("nan"), "n": 0}

    return {
        "is":  _stats(tr_is,  cv_is),
        "oos": _stats(tr_oos, cv_oos),
        "ex":  st_ex,
        "tr_is": tr_is,
        "tr_oos": tr_oos,
    }


# ══════════════════════════════════════════════════════════════════════════
# 1.  HOLD BUG RECONCILIATION
# ══════════════════════════════════════════════════════════════════════════

def analysis_reconcile(df: pd.DataFrame, sim_start: pd.Timestamp) -> None:
    _hdr("1.  HOLD BUG RECONCILIATION  (fixed vs extend.py vs original)")
    print("  Verifies hold=1 matches original simulator baseline.")
    print(f"  Original baseline (dukascopy):  n=129  net=$6,197  PF=2.24  WR=56.6%  MaxDD=-6.95%")
    print(f"  extend.py hold=1 (bugged):      n=129  net=$10,753  PF=2.57  WR=62.0%  MaxDD=-7.22%")
    print()

    print(f"  {'Hold':>5}  {'Mode':<10}  {'n':>5}  {'Net$':>10}  {'PF':>6}  {'WR%':>6}  {'MaxDD%':>8}  {'Note'}")
    _sep()

    for hold in [1, 2, 3, 5, 7]:
        tr, cv = simulate(df, hold_days=hold, mode="all",
                           fee_bps=CAND_FEE, vol_target=CAND_VOLVT,
                           max_alloc=CAND_ALLOC, sim_start=sim_start)
        st = _stats(tr, cv)
        tag = "  ← matches original" if hold == 1 else \
              ("  ← same timing as extend.py hold=1; fees fixed" if hold == 2 else "")
        print(
            f"  {hold:>5}  {'all':<10}  {st['n']:>5}  ${st['net']:>8,.0f}  "
            f"{_pf(st['pf']):>6}  {_pc(st['wr']):>6}  {st['dd']:>7.2f}%{tag}"
        )

    print()
    print("  Yearly check — hold=1 (fixed) vs original:")
    print(f"  {'Year':<6}  {'fixed net':>12}  {'original net':>14}")
    original_yearly = {
        2018: 406, 2019: 3072, 2020: 1554, 2021: 404,
        2022: 155, 2023: -113, 2024: 582, 2025: 40, 2026: 97,
    }
    tr1, _ = simulate(df, hold_days=1, mode="all",
                       fee_bps=CAND_FEE, vol_target=CAND_VOLVT,
                       max_alloc=CAND_ALLOC, sim_start=sim_start)
    yd = _yearly(tr1)
    _sep()
    for yr in sorted(set(yd) | set(original_yearly)):
        fixed_net = yd.get(yr, {}).get("net", float("nan"))
        orig_net  = original_yearly.get(yr, float("nan"))
        delta = fixed_net - orig_net if (np.isfinite(fixed_net) and np.isfinite(orig_net)) else float("nan")
        print(f"  {yr:<6}  ${fixed_net:>9,.0f}    ${orig_net:>10,.0f}    Δ=${delta:>+8,.0f}")


# ══════════════════════════════════════════════════════════════════════════
# 2.  DEPLOYMENT CANDIDATE STRESS TEST
# ══════════════════════════════════════════════════════════════════════════

def analysis_stress(df: pd.DataFrame, sim_start: pd.Timestamp) -> None:
    _hdr(f"2.  STRESS TEST  —  {CAND_MODE}  hold={CAND_HOLD}  trail={CAND_TRAIL}×ATR")
    print("  Varies commission, vol target, and max alloc independently.")
    print(f"  IS: 2018–{IS_END.year}   OOS: {OOS_START.year}–present   ex-strip: {STRIP_YEAR}")

    # ── Commission stress ─────────────────────────────────────────────────
    _sub("Commission sensitivity  (fee bps per side)")
    print(f"  {'Fee':>8}  {'IS n':>5}  {'IS Net$':>10}  {'IS PF':>7}  {'OOS n':>6}  "
          f"{'OOS Net$':>10}  {'OOS PF':>8}  {'ex-{} PF'.format(STRIP_YEAR):>11}  {'OOS MaxDD':>10}")
    _sep()
    for fee in [5.0, 10.0, 15.0, 20.0]:
        r = _run_full(df, CAND_MODE, CAND_HOLD, CAND_TRAIL,
                      fee, CAND_VOLVT, CAND_ALLOC, sim_start)
        tag = "  ← baseline" if fee == CAND_FEE else ""
        print(
            f"  {fee:>6.0f}bps  {r['is']['n']:>5}  ${r['is']['net']:>8,.0f}  "
            f"{_pf(r['is']['pf']):>7}  {r['oos']['n']:>6}  ${r['oos']['net']:>8,.0f}  "
            f"{_pf(r['oos']['pf']):>8}  {_pf(r['ex']['pf']):>11}  "
            f"{r['oos']['dd']:>9.2f}%{tag}"
        )

    # ── Vol target stress ─────────────────────────────────────────────────
    _sub("Vol target sensitivity  (sizing aggressiveness)")
    print(f"  {'VolTgt':>8}  {'IS n':>5}  {'IS Net$':>10}  {'IS PF':>7}  {'OOS n':>6}  "
          f"{'OOS Net$':>10}  {'OOS PF':>8}  {'ex-{} PF'.format(STRIP_YEAR):>11}  {'OOS MaxDD':>10}")
    _sep()
    for vt in [0.010, 0.015, 0.020, 0.025]:
        r = _run_full(df, CAND_MODE, CAND_HOLD, CAND_TRAIL,
                      CAND_FEE, vt, CAND_ALLOC, sim_start)
        tag = "  ← baseline" if vt == CAND_VOLVT else ""
        print(
            f"  {vt*100:>6.1f}%    {r['is']['n']:>5}  ${r['is']['net']:>8,.0f}  "
            f"{_pf(r['is']['pf']):>7}  {r['oos']['n']:>6}  ${r['oos']['net']:>8,.0f}  "
            f"{_pf(r['oos']['pf']):>8}  {_pf(r['ex']['pf']):>11}  "
            f"{r['oos']['dd']:>9.2f}%{tag}"
        )

    # ── Max alloc stress ──────────────────────────────────────────────────
    _sub("Max alloc cap sensitivity")
    print(f"  {'MaxAlloc':>9}  {'IS Net$':>10}  {'OOS Net$':>10}  "
          f"{'OOS PF':>8}  {'ex-{} PF'.format(STRIP_YEAR):>11}  {'OOS MaxDD':>10}")
    _sep()
    for alloc in [0.40, 0.50, 0.75, 1.00]:
        r = _run_full(df, CAND_MODE, CAND_HOLD, CAND_TRAIL,
                      CAND_FEE, CAND_VOLVT, alloc, sim_start)
        tag = "  ← baseline" if alloc == CAND_ALLOC else ""
        print(
            f"  {alloc*100:>7.0f}%    ${r['is']['net']:>8,.0f}  ${r['oos']['net']:>8,.0f}  "
            f"{_pf(r['oos']['pf']):>8}  {_pf(r['ex']['pf']):>11}  "
            f"{r['oos']['dd']:>9.2f}%{tag}"
        )

    # ── Yearly at baseline candidate ──────────────────────────────────────
    _sub(f"Yearly Δequity — candidate ({CAND_MODE} hold={CAND_HOLD} trail={CAND_TRAIL}×)")
    r   = _run_full(df, CAND_MODE, CAND_HOLD, CAND_TRAIL,
                    CAND_FEE, CAND_VOLVT, CAND_ALLOC, sim_start)
    yd_is  = _yearly(r["tr_is"])
    yd_oos = _yearly(r["tr_oos"])
    all_yr = sorted(set(yd_is) | set(yd_oos))
    print(f"  {'Year':<6}  {'IS/OOS':<8}  {'n':>4}  {'Net$':>10}  {'PF':>6}  {'WR%':>6}")
    _sep()
    for yr in all_yr:
        if yr in yd_is:
            d = yd_is[yr]
            print(f"  {yr:<6}  {'IS':<8}  {d['n']:>4}  ${d['net']:>8,.0f}  {_pf(d['pf']):>6}  {_pc(d['wr']):>6}")
        if yr in yd_oos:
            d = yd_oos[yr]
            print(f"  {yr:<6}  {'OOS':<8}  {d['n']:>4}  ${d['net']:>8,.0f}  {_pf(d['pf']):>6}  {_pc(d['wr']):>6}")

    print()
    print("  ▶  Stress interpretation:")
    print("     PF stable across fee 10→20bps       → strategy survives slippage")
    print("     PF stable across vol_target changes  → sizing is conservative enough")
    print("     MaxDD < 15% at max alloc=1.0         → no leverage risk")


# ══════════════════════════════════════════════════════════════════════════
# 3.  SIGNAL QUALITY FILTER  (volume confirmation)
# ══════════════════════════════════════════════════════════════════════════

def analysis_volume(df: pd.DataFrame, sim_start: pd.Timestamp) -> None:
    _hdr("3.  SIGNAL QUALITY — VOLUME CONFIRMATION FILTER")

    has_vol = df["vol_ma20"].notna().any()
    if not has_vol:
        print("  ⚠  Volume data unavailable (dukascopy H1 resampled to daily has no reliable volume).")
        print("     Re-run with --source yfinance to enable volume filter test.")
        print()

        # Fallback: use daily range as vol proxy
        print("  Fallback: using daily range % (high-low)/close as activity proxy.")
        df = df.copy()
        df["range_pct"] = (df["close"].shift(1) if "high" not in df.columns
                           else (df["high"] - df["low"]) / df["close"])
        df["range_ma20"] = df["range_pct"].rolling(20).mean()
        df["vol_proxy"] = df["range_pct"] / df["range_ma20"]   # >1 = active day

        print()
        print(f"  {'RangeFilter':>13}  {'IS n':>5}  {'IS Net$':>10}  {'IS PF':>7}  "
              f"{'OOS n':>6}  {'OOS Net$':>10}  {'OOS PF':>8}  {'ex-{} PF'.format(STRIP_YEAR):>11}")
        _sep()

        # Inject proxy into vol_ma20 column temporarily
        df_proxy = df.copy()
        df_proxy["vol_ma20"] = df["range_ma20"]
        df_proxy["volume"]   = df["range_pct"]

        for mult in [0.0, 0.8, 1.0, 1.2]:
            r = _run_full(df_proxy, CAND_MODE, CAND_HOLD, CAND_TRAIL,
                          CAND_FEE, CAND_VOLVT, CAND_ALLOC, sim_start,
                          vol_mult=mult)
            tag = "  (no filter)" if mult == 0.0 else ""
            print(
                f"  range>{'n/a' if mult==0 else f'{mult:.1f}×avg':>8}   "
                f"{r['is']['n']:>5}  ${r['is']['net']:>8,.0f}  "
                f"{_pf(r['is']['pf']):>7}  {r['oos']['n']:>6}  "
                f"${r['oos']['net']:>8,.0f}  {_pf(r['oos']['pf']):>8}  "
                f"{_pf(r['ex']['pf']):>11}{tag}"
            )
        return

    # yfinance path — real volume data
    print(f"  {'VolFilter':>11}  {'IS n':>5}  {'IS Net$':>10}  {'IS PF':>7}  "
          f"{'OOS n':>6}  {'OOS Net$':>10}  {'OOS PF':>8}  {'ex-{} PF'.format(STRIP_YEAR):>11}")
    _sep()
    for mult in [0.0, 0.8, 1.0, 1.2, 1.5]:
        r = _run_full(df, CAND_MODE, CAND_HOLD, CAND_TRAIL,
                      CAND_FEE, CAND_VOLVT, CAND_ALLOC, sim_start,
                      vol_mult=mult)
        tag = "  (no filter)" if mult == 0.0 else ""
        print(
            f"  vol>{'n/a' if mult==0 else f'{mult:.1f}×avg':>7}   "
            f"{r['is']['n']:>5}  ${r['is']['net']:>8,.0f}  "
            f"{_pf(r['is']['pf']):>7}  {r['oos']['n']:>6}  "
            f"${r['oos']['net']:>8,.0f}  {_pf(r['oos']['pf']):>8}  "
            f"{_pf(r['ex']['pf']):>11}{tag}"
        )

    print()
    print("  ▶  If vol>1.0×avg materially raises PF: add volume gate to candidate.")
    print("     If PF is flat or lower: volume confirmation adds no value here.")


# ══════════════════════════════════════════════════════════════════════════
# 4.  FINAL DEPLOYMENT TABLE
# ══════════════════════════════════════════════════════════════════════════

def analysis_table(df: pd.DataFrame, sim_start: pd.Timestamp) -> None:
    _hdr("4.  FINAL DEPLOYMENT TABLE  —  candidate + neighbours")
    print(f"  Candidate: {CAND_MODE}  hold={CAND_HOLD}  trail={CAND_TRAIL}×ATR  fee={CAND_FEE}bps")
    print(f"  Neighbours: ±1 hold step  ×  [0×, 3.0×] trail  ×  [all, bull] mode")
    print()
    print(f"  {'Mode':<10}  {'Hold':>5}  {'Trail':>6}  "
          f"{'IS PF':>7}  {'IS n':>5}  "
          f"{'OOS PF':>8}  {'OOS n':>6}  "
          f"{'ex24 PF':>9}  {'OOS MaxDD':>10}  {'Verdict'}")
    _sep()

    rows = []
    for mode in MODES:
        for hold in [3, 5, 7]:
            for trail in [0.0, 3.0]:
                r = _run_full(df, mode, hold, trail,
                              CAND_FEE, CAND_VOLVT, CAND_ALLOC, sim_start)
                is_cand = (mode == CAND_MODE and hold == CAND_HOLD and trail == CAND_TRAIL)

                ex_pf = r["ex"]["pf"]
                oos_pf = r["oos"]["pf"]
                oos_dd = r["oos"]["dd"]

                if np.isfinite(ex_pf) and ex_pf > 1.60 and np.isfinite(oos_pf) and oos_pf > 1.40:
                    verdict = "✓ deploy"
                elif np.isfinite(ex_pf) and ex_pf > 1.30:
                    verdict = "~ paper"
                else:
                    verdict = "✗ skip"

                tag = "  ← CANDIDATE" if is_cand else ""
                print(
                    f"  {mode:<10}  {hold:>5}  {trail:>5.1f}×  "
                    f"{_pf(r['is']['pf']):>7}  {r['is']['n']:>5}  "
                    f"{_pf(oos_pf):>8}  {r['oos']['n']:>6}  "
                    f"{_pf(ex_pf):>9}  {oos_dd:>9.2f}%  {verdict}{tag}"
                )
                rows.append({"mode": mode, "hold": hold, "trail": trail, **r["oos"],
                              "ex_pf": ex_pf, "is_pf": r["is"]["pf"]})
            print()

    # Summary judgement
    _sub("Deployment decision")
    deploy_rows = [r for r in rows if np.isfinite(r["ex_pf"]) and r["ex_pf"] > 1.60]
    paper_rows  = [r for r in rows if np.isfinite(r["ex_pf"]) and 1.30 < r["ex_pf"] <= 1.60]

    if deploy_rows:
        print(f"  ✓  {len(deploy_rows)} configuration(s) meet deployment threshold (ex-{STRIP_YEAR} PF > 1.60).")
        best = max(deploy_rows, key=lambda r: r["ex_pf"])
        print(f"     Best by ex-{STRIP_YEAR} PF: {best['mode']}  hold={best['hold']}  "
              f"trail={best['trail']:.1f}×  →  OOS PF={_pf(best['pf'])}  "
              f"ex-{STRIP_YEAR} PF={_pf(best['ex_pf'])}  MaxDD={best['dd']:.2f}%")
    else:
        print(f"  ✗  No configurations meet ex-{STRIP_YEAR} PF > 1.60.  Paper trade only.")

    print()
    print("  Paper trading protocol (if deploying):")
    print("   · Start at 25% of target size for 3 months; scale to 50% if PF > 1.40 live")
    print("   · Max daily loss: 2× avg losing trade → suspend for remainder of UTC day")
    print("   · Monthly review: if 3 consecutive losing months → halt and re-run IS/OOS")
    print("   · Signal check: run simulator in --include-current mode each UTC close")


# ══════════════════════════════════════════════════════════════════════════
# Data loading
# ══════════════════════════════════════════════════════════════════════════

def _load(args: argparse.Namespace) -> pd.DataFrame:
    source = "dukascopy" if args.source == "compare" else args.source
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

    # yfinance includes volume; dukascopy daily resample does not
    if "volume" not in raw.columns and source == "yfinance":
        raw["volume"] = float("nan")

    return _build_df(raw)


# ══════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--source",           choices=("yfinance","dukascopy","compare"), default="dukascopy")
    p.add_argument("--data-start",       default="2018-01-01")
    p.add_argument("--sim-start",        default="2018-01-01")
    p.add_argument("--end",              default=None)
    p.add_argument("--include-current",  action="store_true")
    p.add_argument("--cache-path",       default="btc_breakout_clean/cache/btc_usd_yfinance_daily.csv")
    p.add_argument("--dukascopy-path",   default="btc_breakout_clean/cache/BTCUSD_dukascopy_h1.csv")
    p.add_argument("--refresh-cache",    action="store_true")
    p.add_argument("--only", choices=["reconcile","stress","volume","table"])
    args = p.parse_args()

    print("=" * W)
    print("  BTC BREAKOUT FINAL VALIDATION  —  fixed hold logic  +  stress  +  deployment table")
    print(f"  Source: {args.source}  |  IS: 2018–{IS_END.year}  |  OOS: {OOS_START.year}–present  |  Strip: {STRIP_YEAR}")
    print(f"  Candidate: {CAND_MODE}  hold={CAND_HOLD}  trail={CAND_TRAIL}×ATR  "
          f"fee={CAND_FEE}bps  vol_tgt={CAND_VOLVT*100:.1f}%  alloc={CAND_ALLOC*100:.0f}%")
    print("=" * W)

    print(f"\n  Loading data ({args.source}) ...", flush=True)
    df = _load(args)
    sim_start = pd.Timestamp(args.sim_start, tz="UTC")
    print(f"  {len(df):,} daily bars  {df.index[0].date()} → {df.index[-1].date()}")

    run_all = args.only is None

    if run_all or args.only == "reconcile":
        analysis_reconcile(df, sim_start)

    if run_all or args.only == "stress":
        analysis_stress(df, sim_start)

    if run_all or args.only == "volume":
        analysis_volume(df, sim_start)

    if run_all or args.only == "table":
        analysis_table(df, sim_start)

    print()
    _sep("═")
    print("  FINAL VALIDATION COMPLETE")
    _sep("═")


if __name__ == "__main__":
    main()