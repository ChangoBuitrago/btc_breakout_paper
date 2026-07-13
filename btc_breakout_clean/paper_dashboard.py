#!/usr/bin/env python3
"""
Minimal paper trading dashboard — single page, no tabs.
./btc_breakout_clean/run_dashboard.sh
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import altair as alt
import pandas as pd
import streamlit as st

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import (
    LIVE_MAX_CONCURRENT_ENTRIES,
    LIVE_SLEEVE_EQUITY,
    LIVE_SYMBOLS,
    live_strategy_config,
    live_symbol_equity,
    live_symbol_ibkr_instrument,
    live_symbol_source,
)
from btc_breakout_paper_sim import (
    dukascopy_cache_path,
    effective_hold_max,
    effective_hold_min,
    uses_dynamic_hold,
)
from run_binance_paper_daily import run_symbol, summarize_signal_year

VIEW_START = pd.Timestamp("2026-01-01", tz="UTC")

ACCENT = "#d4a855"
BG = "#101014"
GRID = "#2a2a34"
AXIS = "#6b7280"
DIM = "#9ca3af"
UP = "#4ade80"
DOWN = "#fb7185"
FLAT = "#71717a"

STATE_DIR = HERE / "paper_portfolio"


# ── helpers ──────────────────────────────────────────────────────────────────

def short_sym(symbol: str) -> str:
    s = symbol.upper()
    for suffix in ("USDT", "USD"):
        if s.endswith(suffix):
            return s[: -len(suffix)]
    return s


def _args(refresh_cache: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        symbol=None,
        symbols=",".join(LIVE_SYMBOLS),
        base_url="https://api.binance.com",
        start="2018-01-01",
        end=None,
        equity=LIVE_SLEEVE_EQUITY,
        trend_mode=None,
        state_dir=str(HERE / "paper_portfolio"),
        refresh_cache=refresh_cache,
        no_write=True,
        telegram_token=None,
        telegram_chat_id=None,
        no_telegram=True,
        quiet=True,
    )


def _position_from_curve(
    curve: pd.DataFrame, strat_cfg: Any
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if curve.empty:
        return None, None
    row = curve.iloc[-1]

    pending_entry = None
    if bool(row.get("pending_entry")) and not bool(row.get("in_position")):
        pending_entry = {
            "signal_date": str(row.get("pending_signal_date", ""))[:10],
            "size_frac": float(row.get("pending_size_frac") or 0.0),
        }

    open_position = None
    if bool(row.get("in_position")):
        hold_day = 0
        entry_equity: float | None = None
        for i in range(len(curve) - 1, -1, -1):
            r = curve.iloc[i]
            if bool(r.get("in_position")):
                hold_day += 1
            else:
                entry_equity = float(r["equity"])
                break

        current_equity = float(row["equity"])
        unrealized_pct = 0.0
        if entry_equity and entry_equity > 0:
            unrealized_pct = 100.0 * (current_equity / entry_equity - 1.0)

        hold_min = effective_hold_min(strat_cfg)
        hold_max = effective_hold_max(strat_cfg)
        open_position = {
            "hold_day": hold_day,
            "hold_min": hold_min,
            "hold_max": hold_max,
            "dynamic_hold": uses_dynamic_hold(strat_cfg),
            "unrealized_pct": unrealized_pct,
        }
    return open_position, pending_entry


def load_symbol_from_disk(symbol: str) -> dict[str, Any] | None:
    sym = symbol.upper()
    state_dir = HERE / "paper_portfolio" / sym
    equity_path = state_dir / "equity.csv"
    state_path = state_dir / "state.json"
    if not equity_path.exists():
        return None
    state: dict[str, Any] = {}
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
    curve = pd.read_csv(equity_path)
    trades_path = state_dir / "trades.csv"
    trades = pd.read_csv(trades_path) if trades_path.exists() else pd.DataFrame()
    latest = state.get("latest_signal") or {}
    summary = state.get("summary") or {}
    strat_cfg = live_strategy_config(sym)
    equity = live_symbol_equity(sym, LIVE_SLEEVE_EQUITY)
    if not latest and not curve.empty:
        row = curve.iloc[-1]
        latest = {
            "signal_date": str(row.get("signal_date") or row.get("date", "")),
            "signal": bool(row.get("signal")),
            "breakout_bps": None,
            "regime_on": True,
            "bull": True,
            "next_size_frac": 0.0,
        }
    signal_date = str(latest.get("signal_date") or "")
    if not signal_date and not curve.empty:
        signal_date = str(curve.iloc[-1].get("date", ""))
    year_summary = summarize_signal_year(trades, curve, signal_date=signal_date)
    open_position, pending_entry = _position_from_curve(curve, strat_cfg)
    return {
        "symbol": sym,
        "event": "DISK",
        "summary": summary,
        "latest": latest,
        "year_summary": year_summary,
        "state_path": state_path,
        "equity": equity,
        "strat_cfg": strat_cfg,
        "open_position": open_position,
        "pending_entry": pending_entry,
        "curve": curve,
        "trades": trades,
        "from_disk": True,
    }


@st.cache_data(show_spinner=False, ttl=3600)
def load_symbol(symbol: str, refresh_cache: bool, *, full_replay: bool = False) -> dict[str, Any]:
    if not full_replay and not refresh_cache:
        disk = load_symbol_from_disk(symbol)
        if disk is not None:
            return disk
    args = _args(refresh_cache=refresh_cache)
    return run_symbol(args, symbol, Path(args.state_dir), Path(args.state_dir) / "run_log.csv")


def load_all(refresh_cache: bool, *, full_replay: bool = False) -> list[dict[str, Any]]:
    n = len(LIVE_SYMBOLS)
    mode = "full replay" if full_replay or refresh_cache else "saved state"
    bar = st.progress(0.0, text=f"Loading 0/{n} ({mode})…")
    out: list[dict[str, Any]] = []
    for i, sym in enumerate(LIVE_SYMBOLS):
        bar.progress((i + 1) / n, text=f"Loading {sym} ({i + 1}/{n}, {mode})…")
        try:
            out.append(load_symbol(sym, refresh_cache, full_replay=full_replay))
        except Exception as exc:
            out.append(
                {
                    "symbol": sym.upper(),
                    "event": "ERROR",
                    "summary": {},
                    "latest": {},
                    "year_summary": {"pnl": 0.0},
                    "state_path": HERE / "paper_portfolio" / sym.upper() / "state.json",
                    "equity": live_symbol_equity(sym, LIVE_SLEEVE_EQUITY),
                    "strat_cfg": live_strategy_config(sym),
                    "open_position": None,
                    "pending_entry": None,
                    "curve": pd.DataFrame(),
                    "trades": pd.DataFrame(),
                    "from_disk": False,
                    "load_error": str(exc),
                }
            )
    bar.empty()
    return out


# ── portfolio math ────────────────────────────────────────────────────────────

def eq_series(curve: pd.DataFrame) -> pd.Series:
    if curve.empty or "date" not in curve.columns or "equity" not in curve.columns:
        return pd.Series(dtype=float)
    c = curve.copy()
    c["date"] = pd.to_datetime(c["date"], utc=True, errors="coerce")
    c = c.dropna(subset=["date"])
    if c.empty:
        return pd.Series(dtype=float)
    return c.set_index("date")["equity"].astype(float).sort_index()


def port_equity_series(results: list[dict[str, Any]]) -> pd.Series:
    parts = []
    for r in results:
        s = eq_series(r["curve"])
        if not s.empty:
            parts.append(s.rename(r["symbol"]))
    if not parts:
        return pd.Series(dtype=float)
    wide = pd.concat(parts, axis=1).ffill()
    for r in results:
        sym = r["symbol"]
        if sym not in wide.columns:
            # Symbol has no equity history yet (e.g. IBKR data starts later);
            # backfill the entire column with the sleeve's initial equity.
            wide[sym] = float(r["equity"])
        else:
            wide[sym] = wide[sym].fillna(float(r["equity"]))
    return wide.sum(axis=1).sort_index()


def eq_at(series: pd.Series, ts: pd.Timestamp) -> float | None:
    if series.empty or not hasattr(series.index, "tzinfo"):
        return None
    sub = series[series.index <= ts]
    return float(sub.iloc[-1]) if len(sub) else None


def ret_pct(series: pd.Series, start: pd.Timestamp) -> float:
    base = eq_at(series, start)
    if not base or series.empty:
        return 0.0
    return 100.0 * (float(series.iloc[-1]) / base - 1.0)


def pnl_since(series: pd.Series, start: pd.Timestamp) -> float:
    base = eq_at(series, start)
    if base is None or series.empty:
        return 0.0
    return float(series.iloc[-1]) - base


def max_dd(series: pd.Series, start: pd.Timestamp) -> float:
    s = series[series.index >= start]
    if s.empty:
        return 0.0
    return 100.0 * float((s / s.cummax() - 1.0).min())


def exposure_pct(curve: pd.DataFrame, start: pd.Timestamp) -> float:
    if curve.empty or "in_position" not in curve.columns:
        return 0.0
    c = curve.copy()
    c["date"] = pd.to_datetime(c["date"], utc=True)
    c = c[c["date"] >= start]
    return 0.0 if c.empty else 100.0 * float(c["in_position"].astype(bool).mean())


def latest_bar_date(results: list[dict[str, Any]]) -> str:
    for r in results:
        latest = r.get("latest") or {}
        if latest.get("signal_date"):
            return str(latest["signal_date"])[:10]
        curve = r.get("curve")
        if curve is not None and not curve.empty:
            row = curve.iloc[-1]
            return str(row.get("date", row.get("signal_date", "")))[:10]
    return "n/a"


# ── charts ────────────────────────────────────────────────────────────────────

def equity_curve_chart(port: pd.Series) -> alt.Chart:
    view = port[port.index >= VIEW_START]
    if view.empty:
        return alt.Chart(pd.DataFrame({"date": [], "pct": []})).mark_line()
    base = float(view.iloc[0])
    pct = (view / base - 1.0) * 100.0
    df = pct.reset_index()
    df.columns = ["date", "pct"]
    zero = alt.Chart(pd.DataFrame({"y": [0.0]})).mark_rule(color=GRID, strokeDash=[4, 3]).encode(y="y:Q")
    line = (
        alt.Chart(df)
        .mark_line(color=ACCENT, strokeWidth=2.5, interpolate="monotone")
        .encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%b %d", labelAngle=0, tickCount=8)),
            y=alt.Y("pct:Q", title="% return (2026)", scale=alt.Scale(zero=False)),
            tooltip=[
                alt.Tooltip("date:T", format="%Y-%m-%d"),
                alt.Tooltip("pct:Q", title="return %", format="+.2f"),
            ],
        )
    )
    return (
        alt.layer(zero, line)
        .properties(height=220)
        .configure(background=BG)
        .configure_view(strokeWidth=0)
        .configure_axis(grid=True, gridColor=GRID, domainColor=GRID, tickColor=GRID, labelColor=AXIS, titleColor=DIM)
    )


def sleeve_bar_chart(rows: list[dict[str, Any]]) -> alt.Chart:
    df = pd.DataFrame(rows).sort_values("ret", ascending=True)
    lo, hi = float(df["ret"].min()), float(df["ret"].max())
    pad = max(0.4, (hi - lo) * 0.15)
    bars = (
        alt.Chart(df)
        .mark_bar(size=18, cornerRadiusEnd=3)
        .encode(
            y=alt.Y("label:N", sort=None, title=None),
            x=alt.X("ret:Q", title="return % (2026)", scale=alt.Scale(domain=[min(lo, 0) - pad, max(hi, 0) + pad], zero=True)),
            color=alt.Color(
                "sign:N",
                scale=alt.Scale(domain=["up", "flat", "down"], range=[UP, FLAT, DOWN]),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("label:N", title="sleeve"),
                alt.Tooltip("ret:Q", title="return %", format="+.2f"),
                alt.Tooltip("pnl:Q", title="PnL $", format="+,.0f"),
            ],
        )
    )
    labels = (
        alt.Chart(df)
        .mark_text(align="left", dx=4, fontSize=11, fontWeight=600)
        .encode(
            y=alt.Y("label:N", sort=None),
            x="ret:Q",
            text="ret_label:N",
            color=alt.Color("sign:N", scale=alt.Scale(domain=["up", "flat", "down"], range=[UP, FLAT, DOWN]), legend=None),
        )
    )
    zero = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(color=AXIS, strokeWidth=1).encode(x="x:Q")
    return (
        (bars + labels + zero)
        .properties(height=max(200, 40 * len(df)))
        .configure(background=BG)
        .configure_view(strokeWidth=0)
        .configure_axis(grid=False, domainColor=GRID, tickColor=GRID, labelColor=AXIS, titleColor=DIM)
    )


# ── table builders ────────────────────────────────────────────────────────────

def build_sleeve_rows(results: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """Returns (sleeve_table_rows, bar_chart_rows)."""
    sleeve_rows = []
    bar_rows = []
    for r in results:
        sym = r["symbol"]
        s = eq_series(r["curve"])
        ret = ret_pct(s, VIEW_START)
        pnl = pnl_since(s, VIEW_START)
        op = r.get("open_position")
        pe = r.get("pending_entry")
        latest = r.get("latest") or {}
        strat = r["strat_cfg"]
        curve = r["curve"]
        trades = r["trades"]

        # state label
        if op:
            state = f"LONG {op['hold_day']}/{op['hold_min']}-{op['hold_max']}"
        elif pe and float(pe.get("size_frac") or 0) > 0:
            state = f"PEND {pe['signal_date']}"
        else:
            state = "FLAT"

        # bps to signal
        bps = latest.get("breakout_bps")
        buf = float(strat.buffer_bps)
        if bps is None:
            to_sig = "—"
        elif float(bps) >= buf:
            to_sig = f"{float(bps):.0f} bps (HIT)"
        else:
            to_sig = f"+{buf - float(bps):.0f} bps"

        # last 2026 trade
        last_exit = "—"
        n_trades = 0
        if not trades.empty:
            t = trades.copy()
            t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
            t26 = t[t["exit_date"] >= VIEW_START].sort_values("exit_date", ascending=False)
            n_trades = len(t26)
            if not t26.empty:
                row = t26.iloc[0]
                d = str(row["exit_date"])[:10]
                p = float(row["net_pnl"])
                sign = "+" if p >= 0 else ""
                reason = str(row.get("exit_reason", "") or "")
                suffix = " (sim end)" if reason == "force_exit" else f" [{reason[:8]}]" if reason else ""
                last_exit = f"{d}{suffix} {sign}${p:,.0f}"

        sleeve_rows.append(
            {
                "asset": short_sym(sym),
                "symbol": sym,
                "ibkr": live_symbol_ibkr_instrument(sym),
                "state": state,
                "ret_2026": round(ret, 2),
                "pnl_2026": round(pnl, 0),
                "trades_26": n_trades,
                "exposure": round(exposure_pct(curve, VIEW_START), 1),
                "to_signal": to_sig,
                "unreal_pct": round(op["unrealized_pct"], 1) if op else None,
                "last_exit": last_exit,
            }
        )
        sign = "up" if ret > 0.01 else ("down" if ret < -0.01 else "flat")
        bar_rows.append(
            {
                "label": short_sym(sym),
                "ret": ret,
                "pnl": pnl,
                "ret_label": f"{ret:+.2f}%",
                "sign": sign,
            }
        )
    return (
        sorted(sleeve_rows, key=lambda x: x["ret_2026"], reverse=True),
        sorted(bar_rows, key=lambda x: x["ret"]),
    )


def build_open_positions(results: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for r in results:
        op = r.get("open_position")
        if not op:
            continue
        rows.append(
            {
                "asset": short_sym(r["symbol"]),
                "hold_day": op["hold_day"],
                "hold_range": f"{op['hold_min']}–{op['hold_max']}",
                "can_exit_in": max(0, op["hold_min"] - op["hold_day"]),
                "force_exit_in": max(0, op["hold_max"] - op["hold_day"]),
                "unreal_pct": op["unrealized_pct"],
            }
        )
    return pd.DataFrame(rows)


def build_trades_table(results: list[dict[str, Any]], limit: int = 40) -> pd.DataFrame:
    parts = []
    for r in results:
        t = r["trades"].copy()
        if t.empty:
            continue
        t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
        t = t[t["exit_date"] >= VIEW_START].sort_values("exit_date", ascending=False)
        if t.empty:
            continue
        cols = ["entry_date", "exit_date", "hold_days", "net_pnl", "open_to_exit_pct"]
        if "exit_reason" in t.columns:
            cols.append("exit_reason")
        p = t[cols].copy()
        p.insert(0, "asset", short_sym(r["symbol"]))
        parts.append(p)
    if not parts:
        return pd.DataFrame()
    all_t = pd.concat(parts, ignore_index=True).sort_values("exit_date", ascending=False)
    all_t["net_pnl"] = pd.to_numeric(all_t["net_pnl"]).round(0)
    all_t["open_to_exit_pct"] = pd.to_numeric(all_t["open_to_exit_pct"]).round(1)
    return all_t.head(limit)


def color_signed(val: float) -> str:
    if pd.isna(val):
        return ""
    if val > 0:
        return f"color: {UP}; font-weight: 600"
    if val < 0:
        return f"color: {DOWN}; font-weight: 600"
    return f"color: {DIM}"


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    st.set_page_config(page_title="Paper · 2026", layout="wide", initial_sidebar_state="collapsed")
    st.markdown(
        f"""
        <style>
            .block-container {{ padding-top: 2.5rem; max-width: 100%; }}
            header[data-testid="stHeader"] {{
                background: rgba(16,16,20,0.95);
                border-bottom: 1px solid {GRID};
            }}
            h1 {{ font-weight: 650 !important; letter-spacing: -0.02em; color: #f4f4f8 !important;
                  margin-bottom: 0 !important; }}
            h2 {{ font-size: 1rem !important; font-weight: 600 !important; color: #ececf1 !important;
                  border-left: 3px solid {ACCENT}; padding-left: 0.55rem;
                  margin-top: 1.5rem !important; margin-bottom: 0.4rem !important; }}
            div[data-testid="stMetric"] {{
                background: #18181f; border: 1px solid {GRID}; border-radius: 8px;
                padding: 0.6rem 0.8rem;
            }}
            div[data-testid="stMetricLabel"] {{ color: {DIM} !important; font-size: 0.78rem !important; }}
            div[data-testid="stMetricValue"] {{ color: #f4f4f8 !important; font-size: 1.3rem !important; }}
            .note {{ color: {DIM}; font-size: 0.82rem; line-height: 1.5;
                     background: #18181f; border: 1px solid {GRID}; border-radius: 6px;
                     padding: 0.5rem 0.8rem; margin-top: 0.5rem; }}
        </style>
        """,
        unsafe_allow_html=True,
    )

    # ── header row ───────────────────────────────────────────────────────────
    hcol1, hcol2 = st.columns([9, 1], gap="small", vertical_alignment="center")
    with hcol1:
        st.title("Paper book · IBKR NRA LLC · 2026")
    with hcol2:
        if st.button("↻ Replay", help="Full replay — slow (several minutes)", width="stretch"):
            st.session_state["full_replay"] = True
            load_symbol.clear()
            st.rerun()

    full_replay = bool(st.session_state.pop("full_replay", False))
    with st.spinner("Loading…" if not full_replay else "Full replay (several minutes)…"):
        try:
            results = load_all(refresh_cache=False, full_replay=full_replay)
        except Exception as exc:
            st.error(str(exc))
            st.caption("Run `./btc_breakout_clean/run_dashboard.sh` from repo root.")
            return

    source = "saved state" if all(r.get("from_disk") for r in results) else "replay"
    st.caption(
        f"Data: {source} · as of {latest_bar_date(results)} · "
        f"{len(LIVE_SYMBOLS)} IBKR-executable sleeves · max {LIVE_MAX_CONCURRENT_ENTRIES} concurrent (FCFS) · "
        f"${LIVE_SLEEVE_EQUITY:,.0f}/sleeve"
    )

    # ── portfolio metrics ─────────────────────────────────────────────────────
    port = port_equity_series(results)
    p_ret = ret_pct(port, VIEW_START)
    p_pnl = pnl_since(port, VIEW_START)
    p_dd = max_dd(port, VIEW_START)

    n_long = sum(1 for r in results if r.get("open_position"))
    n_pend = sum(
        1 for r in results
        if r.get("pending_entry") and float(r["pending_entry"].get("size_frac") or 0) > 0
    )
    n_enter = sum(
        1 for r in results
        if not r.get("open_position")
        and r.get("latest", {}).get("signal")
        and float(r.get("latest", {}).get("next_size_frac") or 0) > 0
    )

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Book 2026", f"{p_ret:+.2f}%", f"${p_pnl:+,.0f}")
    m2.metric("Max DD 2026", f"{p_dd:.2f}%")
    m3.metric("Open positions", f"{n_long} / {LIVE_MAX_CONCURRENT_ENTRIES}")
    m4.metric("Entering soon", str(n_enter + n_pend))
    m5.metric("Book value", f"${sum(float(r['equity']) for r in results):,.0f}")

    st.divider()

    # ── equity curve + sleeve bar ─────────────────────────────────────────────
    st.markdown("## Performance")
    sleeve_rows, bar_rows = build_sleeve_rows(results)

    col_curve, col_bar = st.columns([3, 2], gap="large")
    with col_curve:
        st.altair_chart(equity_curve_chart(port), width="stretch")
    with col_bar:
        if bar_rows:
            st.altair_chart(sleeve_bar_chart(bar_rows), width="stretch")

    # ── sleeve status table ───────────────────────────────────────────────────
    st.markdown("## Sleeves")
    sleeve_df = pd.DataFrame(sleeve_rows).drop(columns=["symbol"])
    styled = sleeve_df.style.map(color_signed, subset=["ret_2026", "pnl_2026", "unreal_pct"])
    st.dataframe(
        styled,
        width="stretch",
        hide_index=True,
        column_config={
            "asset": st.column_config.TextColumn("Asset", width="small"),
            "ibkr": st.column_config.TextColumn("IBKR instrument", width="medium"),
            "state": st.column_config.TextColumn("State", width="medium"),
            "ret_2026": st.column_config.NumberColumn("Return %", format="%.2f"),
            "pnl_2026": st.column_config.NumberColumn("PnL $", format="$%d"),
            "trades_26": st.column_config.NumberColumn("Trades", format="%d", width="small"),
            "exposure": st.column_config.NumberColumn("In mkt %", format="%.1f"),
            "to_signal": st.column_config.TextColumn("Bps to signal"),
            "unreal_pct": st.column_config.NumberColumn("Unreal %", format="%.1f"),
            "last_exit": st.column_config.TextColumn("Last exit (2026)"),
        },
    )

    # ── open positions ────────────────────────────────────────────────────────
    open_df = build_open_positions(results)
    if not open_df.empty:
        st.markdown("## Open positions")
        open_styled = open_df.style.map(color_signed, subset=["unreal_pct"])
        st.dataframe(
            open_styled,
            width="stretch",
            hide_index=True,
            column_config={
                "asset": st.column_config.TextColumn("Asset", width="small"),
                "hold_day": st.column_config.NumberColumn("Hold day", format="%d", width="small"),
                "hold_range": st.column_config.TextColumn("Min–max", width="small"),
                "can_exit_in": st.column_config.NumberColumn("Can fade-exit in", format="%d d", width="medium"),
                "force_exit_in": st.column_config.NumberColumn("Hard exit in", format="%d d", width="medium"),
                "unreal_pct": st.column_config.NumberColumn("Unrealized %", format="%.1f"),
            },
        )

    # ── recent trades ─────────────────────────────────────────────────────────
    st.markdown("## 2026 trades")
    trades_df = build_trades_table(results, limit=40)
    if trades_df.empty:
        st.write("No exits yet in 2026.")
    else:
        t_styled = trades_df.style.map(color_signed, subset=["net_pnl", "open_to_exit_pct"])
        st.dataframe(
            t_styled,
            width="stretch",
            hide_index=True,
            column_config={
                "asset": st.column_config.TextColumn("Asset", width="small"),
                "entry_date": st.column_config.TextColumn("Entry"),
                "exit_date": st.column_config.TextColumn("Exit"),
                "hold_days": st.column_config.NumberColumn("Hold d", format="%d", width="small"),
                "net_pnl": st.column_config.NumberColumn("PnL $", format="$%d"),
                "open_to_exit_pct": st.column_config.NumberColumn("PnL %", format="%.1f"),
                "exit_reason": st.column_config.TextColumn("Exit reason"),
            },
        )

    # ── Pine vs Python note ───────────────────────────────────────────────────
    st.markdown(
        """
        <div class="note">
        <b>IBKR NRA LLC paper book</b> — each sleeve maps to an instrument executable on one IBKR LLC US
        corporate account (W-8BEN-E). Data source: <b>IBKR TWS/Gateway daily bars</b> when TWS is running;
        automatic fallback to Binance klines (crypto), Dukascopy H1 (metals), or yfinance (BNO ETF) when offline.
        Brent exposure via <b>BNO ETF</b>; crypto via Paxos; metals via IDEALPRO.
        Portfolio cap (max 4 concurrent entries) is modeled; TradingView Pine does not enforce it.
        </div>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
