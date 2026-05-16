#!/usr/bin/env python3
"""
Minimal local dashboard — what Telegram does *not* show:
  • Portfolio equity curve since 2026
  • Per-sleeve return % since 2026 (not YTD $ share)
  • PnL attribution, drawdown, exposure, last trade, 2026 trade list

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
    LIVE_SLEEVE_EQUITY,
    LIVE_SYMBOLS,
    live_strategy_config,
    live_symbol_equity,
    live_symbol_source,
)
from btc_breakout_paper_sim import dukascopy_cache_path
from run_binance_paper_daily import run_symbol, signal_status, summarize_signal_year

VIEW_START = pd.Timestamp("2026-01-01", tz="UTC")
MIN_BAR_RET = 0.12  # minimum |return %| so flat sleeves stay visible on chart

# Dashboard palette (dark UI, warm accent — avoid default Streamlit blues)
ACCENT = "#d4a855"
BG_CHART = "#101014"
GRID = "#2a2a34"
AXIS = "#6b7280"
TEXT_DIM = "#9ca3af"
UP = "#4ade80"
DOWN = "#fb7185"
FLAT_BAR = "#71717a"


def short_sym(symbol: str) -> str:
    s = symbol.upper()
    if s.endswith("USDT"):
        return s[:-4]
    if s.endswith("USD"):
        return s[:-3]
    return s


def _args(*, refresh_cache: bool = False) -> argparse.Namespace:
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


def load_symbol_from_disk(symbol: str) -> dict[str, Any] | None:
    sym = symbol.upper()
    state_dir = HERE / "paper_portfolio" / sym
    equity_path = state_dir / "equity.csv"
    state_path = state_dir / "state.json"
    if not equity_path.exists() or not state_path.exists():
        return None
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
    signal_date = str(latest.get("signal_date") or curve.iloc[-1].get("signal_date", ""))
    year_summary = summarize_signal_year(trades, curve, signal_date=signal_date)

    open_position = None
    pending_entry = None
    if not curve.empty:
        row = curve.iloc[-1]
        if bool(row.get("pending_entry")) and not bool(row.get("in_position")):
            pending_entry = {
                "signal_date": str(row.get("pending_signal_date", ""))[:10],
                "size_frac": float(row.get("pending_size_frac") or 0.0),
            }

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
    dukas = [s for s in LIVE_SYMBOLS if live_symbol_source(s) == "dukascopy"]
    if not any(dukascopy_cache_path(s).exists() for s in dukas):
        if not refresh_cache and not full_replay:
            st.caption("First full replay downloads Dukascopy history (5–15 min). Run daily bot once to cache.")
    mode = "full replay" if full_replay or refresh_cache else "saved state"
    bar = st.progress(0.0, text=f"Loading 0/{n} ({mode})…")
    out: list[dict[str, Any]] = []
    for i, sym in enumerate(LIVE_SYMBOLS):
        bar.progress((i + 1) / n, text=f"Loading {sym} ({i + 1}/{n}, {mode})…")
        out.append(load_symbol(sym, refresh_cache, full_replay=full_replay))
    bar.empty()
    return out


def eq_series(curve: pd.DataFrame) -> pd.Series:
    if curve.empty:
        return pd.Series(dtype=float)
    c = curve.copy()
    c["date"] = pd.to_datetime(c["date"], utc=True)
    return c.set_index("date")["equity"].astype(float).sort_index()


def port_equity(results: list[dict[str, Any]]) -> pd.Series:
    parts = []
    for r in results:
        s = eq_series(r["curve"])
        if not s.empty:
            parts.append(s.rename(r["symbol"]))
    if not parts:
        return pd.Series(dtype=float)
    wide = pd.concat(parts, axis=1).ffill()
    for r in results:
        wide[r["symbol"]] = wide[r["symbol"]].fillna(float(r["equity"]))
    return wide.sum(axis=1).sort_index()


def eq_at(series: pd.Series, ts: pd.Timestamp) -> float | None:
    sub = series[series.index <= ts]
    return float(sub.iloc[-1]) if len(sub) else None


def ret_pct_since(series: pd.Series, start: pd.Timestamp) -> float:
    base = eq_at(series, start)
    if not base or series.empty:
        return 0.0
    return 100.0 * (float(series.iloc[-1]) / base - 1.0)


def max_dd_since(series: pd.Series, start: pd.Timestamp) -> float:
    s = series[series.index >= start]
    if s.empty:
        return 0.0
    dd = s / s.cummax() - 1.0
    return 100.0 * float(dd.min())


def exposure_since(curve: pd.DataFrame, start: pd.Timestamp) -> float:
    if curve.empty or "in_position" not in curve.columns:
        return 0.0
    c = curve.copy()
    c["date"] = pd.to_datetime(c["date"], utc=True)
    c = c[c["date"] >= start]
    if c.empty:
        return 0.0
    return 100.0 * float(c["in_position"].astype(bool).mean())


def bps_to_signal(latest: dict[str, Any], buffer_bps: float) -> str:
    bps = latest.get("breakout_bps")
    if bps is None:
        return "—"
    bps = float(bps)
    if bps >= buffer_bps:
        return f"{bps:.0f} (≥buf)"
    return f"+{buffer_bps - bps:.0f}"


def last_trade_2026(trades: pd.DataFrame) -> str:
    if trades.empty:
        return "—"
    t = trades.copy()
    t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
    t = t[t["exit_date"] >= VIEW_START].sort_values("exit_date", ascending=False)
    if t.empty:
        return "—"
    row = t.iloc[0]
    d = str(row["exit_date"])[:10]
    pnl = float(row["net_pnl"])
    sign = "+" if pnl >= 0 else ""
    return f"{d} {sign}${pnl:,.0f}"


def trades_2026_count(trades: pd.DataFrame) -> int:
    if trades.empty:
        return 0
    t = trades.copy()
    t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
    return int((t["exit_date"] >= VIEW_START).sum())


def state_label(r: dict[str, Any]) -> str:
    if r.get("open_position"):
        op = r["open_position"]
        if op.get("dynamic_hold"):
            return f"LONG {op['hold_day']}/{op['hold_min']}-{op['hold_max']}"
        return f"LONG {op['hold_day']}/{op['hold_days']}"
    pe = r.get("pending_entry")
    if pe and float(pe.get("size_frac") or 0) > 0:
        return f"PEND {pe['signal_date']}"
    return "FLAT"


def pnl_since(series: pd.Series, start: pd.Timestamp) -> float:
    base = eq_at(series, start)
    if base is None or series.empty:
        return 0.0
    return float(series.iloc[-1]) - base


def portfolio_pct_chart(pct: pd.Series) -> alt.Chart:
    df = pct.reset_index()
    df.columns = ["date", "pct"]
    line = (
        alt.Chart(df)
        .mark_line(color=ACCENT, strokeWidth=2.75, interpolate="monotone")
        .encode(
            x=alt.X("date:T", title=None, axis=alt.Axis(format="%b %d", labelAngle=0, tickCount=8)),
            y=alt.Y("pct:Q", title="% vs Jan 1", scale=alt.Scale(zero=False)),
            tooltip=[
                alt.Tooltip("date:T", title="date", format="%Y-%m-%d"),
                alt.Tooltip("pct:Q", title="portfolio %", format="+.2f"),
            ],
        )
    )
    rule = alt.Chart(pd.DataFrame({"y": [0.0]})).mark_rule(color=GRID, strokeDash=[5, 4]).encode(y="y:Q")
    return (
        alt.layer(rule, line)
        .properties(height=240)
        .configure(background=BG_CHART)
        .configure_view(strokeWidth=0)
        .configure_axis(grid=True, gridColor=GRID, domainColor=GRID, tickColor=GRID, labelColor=AXIS, titleColor=TEXT_DIM)
    )


def sleeve_stats(results: list[dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for r in results:
        s = eq_series(r["curve"])
        ret = ret_pct_since(s, VIEW_START)
        pnl = pnl_since(s, VIEW_START)
        if ret > 0.01:
            sign = "up"
        elif ret < -0.01:
            sign = "down"
        else:
            sign = "flat"
        if abs(ret) < MIN_BAR_RET:
            ret_plot = MIN_BAR_RET if ret >= 0 else -MIN_BAR_RET
        else:
            ret_plot = ret
        rows.append(
            {
                "symbol": r["symbol"],
                "label": short_sym(r["symbol"]),
                "pnl_2026": pnl,
                "ret_2026": ret,
                "ret_plot": ret_plot,
                "ret_label": f"{ret:+.2f}%",
                "pnl_label": f"${pnl:+,.0f}",
                "sign": sign,
            }
        )
    return pd.DataFrame(rows).sort_values("ret_2026", ascending=True)


def ret_bar_chart(stats: pd.DataFrame) -> alt.Chart:
    lo = float(stats["ret_plot"].min())
    hi = float(stats["ret_plot"].max())
    pad = max(0.35, (hi - lo) * 0.12)
    x_domain = [min(lo, 0) - pad, max(hi, 0) + pad]

    bars = (
        alt.Chart(stats)
        .mark_bar(size=22, cornerRadiusEnd=3)
        .encode(
            y=alt.Y(
                "label:N",
                sort=alt.EncodingSortField(field="ret_2026", order="ascending"),
                title=None,
                axis=alt.Axis(labelLimit=80),
            ),
            x=alt.X(
                "ret_plot:Q",
                title="return % since 2026-01-01",
                scale=alt.Scale(domain=x_domain, zero=True),
            ),
            color=alt.Color(
                "sign:N",
                scale=alt.Scale(
                    domain=["up", "flat", "down"],
                    range=[UP, FLAT_BAR, DOWN],
                ),
                legend=alt.Legend(
                    title=None,
                    orient="top",
                    direction="horizontal",
                    labelExpr="datum.value == 'up' ? 'gain' : datum.value == 'down' ? 'loss' : 'flat'",
                ),
            ),
            tooltip=[
                alt.Tooltip("symbol:N", title="sleeve"),
                alt.Tooltip("ret_2026:Q", title="return %", format="+.2f"),
                alt.Tooltip("pnl_2026:Q", title="PnL $", format="+,.0f"),
            ],
        )
    )
    labels = (
        alt.Chart(stats)
        .mark_text(align="left", dx=4, fontSize=12, fontWeight=600)
        .encode(
            y=alt.Y("label:N", sort=alt.EncodingSortField(field="ret_2026", order="ascending")),
            x=alt.X("ret_plot:Q"),
            text=alt.Text("ret_label:N"),
            color=alt.Color(
                "sign:N",
                scale=alt.Scale(
                    domain=["up", "flat", "down"],
                    range=[UP, FLAT_BAR, DOWN],
                ),
                legend=None,
            ),
        )
    )
    pnl_labels = (
        alt.Chart(stats)
        .mark_text(align="right", dx=-6, fontSize=11, color=TEXT_DIM)
        .encode(
            y=alt.Y("label:N", sort=alt.EncodingSortField(field="ret_2026", order="ascending")),
            x=alt.X("ret_plot:Q"),
            text=alt.Text("pnl_label:N"),
        )
    )
    zero = alt.Chart(pd.DataFrame({"x": [0]})).mark_rule(color=AXIS, strokeWidth=1).encode(x="x:Q")
    return (
        (bars + labels + pnl_labels + zero)
        .properties(height=max(240, 44 * len(stats)))
        .configure(background=BG_CHART)
        .configure_view(strokeWidth=0)
        .configure_axis(grid=False, domainColor=GRID, tickColor=GRID, labelColor=AXIS, titleColor=TEXT_DIM)
    )


def color_signed(val: float) -> str:
    if pd.isna(val):
        return ""
    if val > 0:
        return f"color: {UP}; font-weight: 600"
    if val < 0:
        return f"color: {DOWN}; font-weight: 600"
    return f"color: {TEXT_DIM}; font-weight: 500"


def main() -> None:
    st.set_page_config(page_title="Paper · 2026+", layout="wide", initial_sidebar_state="collapsed")

    st.markdown(
        f"""
        <style>
            .block-container {{ padding-top: 2.75rem; max-width: 100%; }}
            header[data-testid="stHeader"] {{
                background: rgba(16, 16, 20, 0.92);
                border-bottom: 1px solid #2a2a34;
            }}
            h1 {{ font-weight: 650 !important; letter-spacing: -0.02em; color: #f4f4f8 !important;
                  margin-bottom: 0 !important; padding-top: 0 !important; }}
            h2 {{ font-size: 1.05rem !important; font-weight: 600 !important;
                  color: #ececf1 !important; border-left: 3px solid {ACCENT};
                  padding-left: 0.6rem; margin-top: 1.35rem !important; margin-bottom: 0.5rem !important; }}
            div[data-testid="stExpander"] summary {{ color: #c8c8d0 !important; }}
            div[data-testid="stHorizontalBlock"]:has(div[data-testid="column"]:first-child button) {{
                align-items: center;
            }}
            div[data-testid="column"]:has(button) {{
                display: flex;
                align-items: center;
                justify-content: flex-start;
            }}
            div[data-testid="column"]:has(button) button {{
                margin-top: 0;
            }}
        </style>
        """,
        unsafe_allow_html=True,
    )

    btn_col, title_col = st.columns([1, 11], gap="small", vertical_alignment="center")
    with btn_col:
        if st.button("↻", key="refresh", help="Full replay (slow)", use_container_width=True):
            st.session_state["full_replay"] = True
            load_symbol.clear()
            st.rerun()
    with title_col:
        st.title("Paper book · 2026+")

    full_replay = bool(st.session_state.pop("full_replay", False))
    try:
        with st.spinner("Loading portfolio…" if not full_replay else "Full replay (several minutes)…"):
            results = load_all(refresh_cache=False, full_replay=full_replay)
    except Exception as exc:
        st.error(str(exc))
        st.caption("Run `./btc_breakout_clean/run_dashboard.sh` from repo root (Python 3.10+).")
        return

    if not full_replay and all(r.get("from_disk") for r in results):
        st.caption("Loaded from saved paper state · ↻ runs full replay")
    elif full_replay:
        st.caption("Full replay complete")

    port = port_equity(results)
    last_bar = results[0]["latest"]["signal_date"][:10]
    port_base_2026 = eq_at(port, VIEW_START)
    port_ret = ret_pct_since(port, VIEW_START)
    port_pnl = pnl_since(port, VIEW_START)
    port_dd = max_dd_since(port, VIEW_START)

    st.markdown(
        f"**{last_bar}** UTC close · **2026 PnL {port_pnl:+,.0f}** ({port_ret:+.2f}%) · "
        f"max DD since Jan-1 **{port_dd:.2f}%**"
    )

    # --- Chart: portfolio % since 2026 (Altair — warm accent, no default blue line_chart) ---
    view = port[port.index >= VIEW_START]
    if not view.empty and port_base_2026:
        pct = (view / port_base_2026 - 1.0) * 100.0
        st.altair_chart(portfolio_pct_chart(pct), use_container_width=True)

    # --- Per-sleeve return % since 2026 (non-flat only on chart) ---
    stats = sleeve_stats(results)
    chart_stats = stats.loc[stats["sign"] != "flat"].copy()
    if not chart_stats.empty:
        st.subheader("Sleeves since 2026")
        st.altair_chart(ret_bar_chart(chart_stats), use_container_width=True)

    # --- Scan table: one row per sleeve ---
    rows = []
    stats_by_sym = stats.set_index("symbol")
    for r in results:
        sym = r["symbol"]
        latest = r["latest"]
        strat = r["strat_cfg"]
        curve = r["curve"]
        trades = r["trades"]
        op = r.get("open_position")
        st_row = stats_by_sym.loc[sym]
        row = {
            "": state_label(r),
            "symbol": sym,
            "2026": "▲" if st_row["sign"] == "up" else ("▼" if st_row["sign"] == "down" else "—"),
            "pnl_2026_$": round(float(st_row["pnl_2026"]), 0),
            "ret_2026_%": round(float(st_row["ret_2026"]), 2),
            "trades_26": trades_2026_count(trades),
            "exposure_%": round(exposure_since(curve, VIEW_START), 1),
            "last_exit": last_trade_2026(trades),
            "to_signal_bps": bps_to_signal(latest, float(strat.buffer_bps)),
        }
        if op:
            row["unreal_%"] = round(op["unrealized_pct"], 1)
            row["exit_on"] = op["exit_target"]
        else:
            row["unreal_%"] = None
            row["exit_on"] = None
        rows.append(row)

    scan = pd.DataFrame(rows).sort_values("ret_2026_%", ascending=False)
    styled = scan.style.map(color_signed, subset=["pnl_2026_$", "ret_2026_%"])

    st.caption("Today’s gate / status (Telegram text) in expander below.")
    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        column_config={
            "": st.column_config.TextColumn("state", width="small"),
            "2026": st.column_config.TextColumn("", width="small"),
            "pnl_2026_$": st.column_config.NumberColumn("PnL 2026 $", format="%.0f"),
            "ret_2026_%": st.column_config.NumberColumn("ret 2026 %", format="%.2f"),
            "trades_26": st.column_config.NumberColumn("trades", format="%d"),
            "exposure_%": st.column_config.NumberColumn("in mkt %", format="%.1f"),
            "unreal_%": st.column_config.NumberColumn("unreal %", format="%.1f"),
        },
    )

    with st.expander("Telegram-style status (per sleeve)"):
        for r in results:
            st.text(
                f"{r['symbol']}: {signal_status(r['latest'], r['strat_cfg'], r.get('open_position'), r.get('pending_entry'))}"
            )

    # --- 2026 trades (TG never lists history) ---
    st.subheader("Exits since 2026")
    parts = []
    for r in results:
        t = r["trades"].copy()
        if t.empty:
            continue
        t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
        t = t[t["exit_date"] >= VIEW_START].sort_values("exit_date", ascending=False)
        if not t.empty:
            p = t[["entry_date", "exit_date", "hold_days", "net_pnl", "open_to_exit_pct", "size_frac"]].copy()
            p.insert(0, "symbol", r["symbol"])
            parts.append(p)
    if parts:
        all_t = pd.concat(parts, ignore_index=True).sort_values("exit_date", ascending=False)
        all_t["net_pnl"] = pd.to_numeric(all_t["net_pnl"]).round(0)
        all_t["open_to_exit_pct"] = pd.to_numeric(all_t["open_to_exit_pct"]).round(1)
        all_t["size_frac"] = (pd.to_numeric(all_t["size_frac"]) * 100).round(0)
        trade_styled = all_t.head(20).style.map(color_signed, subset=["net_pnl", "open_to_exit_pct"])
        st.dataframe(trade_styled, use_container_width=True, hide_index=True)
    else:
        st.write("No exits yet in 2026.")

    st.caption(f"Updated {pd.Timestamp.utcnow():%Y-%m-%d %H:%M UTC} · 2026 view only")


if __name__ == "__main__":
    main()
