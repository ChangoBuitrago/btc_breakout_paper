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
from btc_breakout_paper_sim import (
    dukascopy_cache_path,
    effective_hold_max,
    effective_hold_min,
    uses_dynamic_hold,
)
from run_binance_paper_daily import run_symbol, signal_status, summarize_signal_year
from signal_forecast import forecast_display, forecast_for_symbol, forecast_sort_key

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


def _position_from_curve(curve: pd.DataFrame, strat_cfg: Any) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
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
        for i in range(len(curve) - 1, -1, -1):
            if bool(curve.iloc[i].get("in_position")):
                hold_day += 1
            else:
                break
        hold_min = effective_hold_min(strat_cfg)
        hold_max = effective_hold_max(strat_cfg)
        dynamic = uses_dynamic_hold(strat_cfg)
        open_position = {
            "hold_day": hold_day,
            "hold_min": hold_min,
            "hold_max": hold_max,
            "hold_days": hold_max,
            "dynamic_hold": dynamic,
            "unrealized_pct": 0.0,
            "exit_target": "—",
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


@st.cache_data(show_spinner=False, ttl=3600)
def load_forecast(
    symbol: str,
    latest_json: str,
    strat_key: tuple[Any, ...],
    position_json: str,
    trades_mtime: float,
) -> dict[str, Any]:
    """Cached forecast from daily history + saved trades (no full replay)."""
    latest = json.loads(latest_json)
    pos = json.loads(position_json)
    strat_cfg = live_strategy_config(symbol)
    _ = strat_key
    state_dir = HERE / "paper_portfolio" / symbol.upper()
    trades_path = state_dir / "trades.csv"
    _ = trades_mtime
    trades = pd.read_csv(trades_path) if trades_path.exists() else pd.DataFrame()
    try:
        return forecast_for_symbol(
            symbol,
            latest=latest,
            strat_cfg=strat_cfg,
            trades=trades,
            open_position=pos.get("open_position"),
            pending_entry=pos.get("pending_entry"),
            start="2018-01-01",
        )
    except Exception as exc:
        return {
            "state": "error",
            "next_in_label": str(exc)[:80],
            "quality_tier": "n/a",
            "blockers": [str(exc)[:80]],
            "regime_on": False,
        }


def _forecast_cache_key(r: dict[str, Any]) -> tuple[Any, ...]:
    strat = r["strat_cfg"]
    trades_path = HERE / "paper_portfolio" / r["symbol"].upper() / "trades.csv"
    mtime = trades_path.stat().st_mtime if trades_path.exists() else 0.0
    pos_json = json.dumps(
        {"open_position": r.get("open_position"), "pending_entry": r.get("pending_entry")},
        default=str,
    )
    return (
        json.dumps(r["latest"], default=str),
        (
            strat.lookback,
            strat.buffer_bps,
            strat.trend_mode,
            strat.hold_min,
            strat.hold_max,
            strat.dynamic_hold,
        ),
        pos_json,
        mtime,
    )


def get_forecast(r: dict[str, Any]) -> dict[str, Any]:
    latest_json, strat_key, pos_json, mtime = _forecast_cache_key(r)
    return load_forecast(r["symbol"], latest_json, strat_key, pos_json, mtime)


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


def count_book_states(results: list[dict[str, Any]]) -> tuple[int, int, int]:
    n_long = n_enter = n_pending = 0
    for r in results:
        if r.get("open_position"):
            n_long += 1
        elif r.get("pending_entry") and float(r["pending_entry"].get("size_frac") or 0) > 0:
            n_pending += 1
        elif r.get("latest", {}).get("signal") and float(r["latest"].get("next_size_frac") or 0) > 0:
            n_enter += 1
    return n_long, n_enter, n_pending


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


def build_forecast_rows(
    results: list[dict[str, Any]],
    progress: Any | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    n = len(results)
    for i, r in enumerate(results):
        if progress is not None:
            progress((i + 1) / n, text=f"Forecast {r['symbol']} ({i + 1}/{n})…")
        try:
            fc = get_forecast(r)
        except Exception as exc:
            fc = {
                "state": "error",
                "next_in_label": str(exc)[:80],
                "quality_tier": "n/a",
                "blockers": [str(exc)[:80]],
                "regime_on": False,
            }
        d = forecast_display(fc)
        p7 = d["p7_pct"]
        p14 = d["p14_pct"]
        rows.append(
            {
                "asset": short_sym(r["symbol"]),
                "symbol": r["symbol"],
                "status": d["status"],
                "timing": d["timing"],
                "regime": d["regime_label"],
                "gap_bps": d["gap_bps_label"],
                "med_days": fc.get("next_in_days"),
                "p7": p7,
                "p7_label": f"{p7}%" if p7 is not None else "—",
                "p14": p14,
                "p14_label": f"{p14}%" if p14 is not None else "—",
                "quality": d["quality_score"],
                "tier": d["quality_tier"],
                "hist_win": round(fc["win_pct"], 0) if fc.get("win_pct") is not None else None,
                "hist_med_pct": round(fc["med_open_to_exit_pct"], 1)
                if fc.get("med_open_to_exit_pct") is not None
                else None,
                "since_exit_d": d["since_exit"],
                "blockers": d["blockers_short"],
                "_sort": forecast_sort_key(fc),
            }
        )
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.sort_values("_sort").drop(columns="_sort")
    return df


def build_performance_rows(results: list[dict[str, Any]], stats: pd.DataFrame) -> pd.DataFrame:
    stats_by_sym = stats.set_index("symbol")
    rows: list[dict[str, Any]] = []
    for r in results:
        sym = r["symbol"]
        latest = r["latest"]
        strat = r["strat_cfg"]
        curve = r["curve"]
        trades = r["trades"]
        op = r.get("open_position")
        st_row = stats_by_sym.loc[sym]
        row: dict[str, Any] = {
            "asset": short_sym(sym),
            "symbol": sym,
            "state": state_label(r),
            "pnl_2026": round(float(st_row["pnl_2026"]), 0),
            "ret_2026": round(float(st_row["ret_2026"]), 2),
            "trades_26": trades_2026_count(trades),
            "exposure": round(exposure_since(curve, VIEW_START), 1),
            "to_signal": bps_to_signal(latest, float(strat.buffer_bps)),
            "last_exit": last_trade_2026(trades),
        }
        if op:
            row["unreal_pct"] = round(op["unrealized_pct"], 1)
            row["exit_by"] = op["exit_target"]
        else:
            row["unreal_pct"] = None
            row["exit_by"] = None
        rows.append(row)
    return pd.DataFrame(rows).sort_values("ret_2026", ascending=False)


def forecast_p7_chart(forecast_df: pd.DataFrame) -> alt.Chart:
    plot = forecast_df.dropna(subset=["p7"]).copy()
    if plot.empty:
        plot = forecast_df.copy()
        plot["p7"] = 0.0
    plot = plot.sort_values("p7", ascending=True)
    status_colors = ["ENTER", "IN TRADE", "WATCH", "BLOCKED"]
    plot.loc[~plot["status"].isin(status_colors), "status"] = "WATCH"
    color_range = [ACCENT, UP, "#5b8def", DOWN]
    bars = (
        alt.Chart(plot)
        .mark_bar(size=18, cornerRadiusEnd=3)
        .encode(
            y=alt.Y("asset:N", sort=alt.EncodingSortField(field="p7", order="descending"), title=None),
            x=alt.X("p7:Q", title="P(signal within 7d) %", scale=alt.Scale(domain=[0, 100])),
            color=alt.Color(
                "status:N",
                scale=alt.Scale(domain=status_colors, range=color_range),
                legend=None,
            ),
            tooltip=[
                alt.Tooltip("symbol:N", title="sleeve"),
                alt.Tooltip("status:N"),
                alt.Tooltip("timing:N"),
                alt.Tooltip("gap_bps:N", title="bps to signal"),
                alt.Tooltip("med_days:Q", title="median days"),
                alt.Tooltip("p7:Q", title="P(7d) %", format=".0f"),
                alt.Tooltip("quality:Q", title="quality score"),
                alt.Tooltip("tier:N"),
            ],
        )
    )
    return (
        bars.properties(height=max(220, 36 * len(plot)))
        .configure(background=BG_CHART)
        .configure_view(strokeWidth=0)
        .configure_axis(gridColor=GRID, domainColor=GRID, tickColor=GRID, labelColor=AXIS, titleColor=TEXT_DIM)
    )


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
            div[data-testid="stMetric"] {{
                background: #18181f; border: 1px solid #2a2a34; border-radius: 8px;
                padding: 0.65rem 0.85rem;
            }}
            div[data-testid="stMetricLabel"] {{ color: #9ca3af !important; font-size: 0.78rem !important; }}
            div[data-testid="stMetricValue"] {{ color: #f4f4f8 !important; font-size: 1.35rem !important; }}
            [data-testid="stTabs"] button {{ font-weight: 550; }}
            div[data-testid="stExpander"] summary {{ color: #c8c8d0 !important; }}
            .forecast-legend {{
                background: #18181f; border: 1px solid #2a2a34; border-radius: 8px;
                padding: 0.75rem 1rem; margin-bottom: 0.75rem; color: #b8b8c4; font-size: 0.88rem;
                line-height: 1.45;
            }}
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
        if st.button("↻", key="refresh", help="Full replay (slow)", width="stretch"):
            st.session_state["full_replay"] = True
            load_symbol.clear()
            load_forecast.clear()
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
    last_bar = latest_bar_date(results)
    port_base_2026 = eq_at(port, VIEW_START)
    port_ret = ret_pct_since(port, VIEW_START)
    port_pnl = pnl_since(port, VIEW_START)
    port_dd = max_dd_since(port, VIEW_START)

    stats = sleeve_stats(results)
    perf_df = build_performance_rows(results, stats)
    n_long, n_enter, n_pending = count_book_states(results)

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Book 2026", f"{port_ret:+.2f}%", f"${port_pnl:+,.0f}")
    m2.metric("Max DD 2026", f"{port_dd:.2f}%")
    m3.metric("As of", last_bar)
    m4.metric("In trade", str(n_long))
    m5.metric("Enter soon", str(n_enter + n_pending), help="Signal fired — entry next UTC open")

    tab_overview, tab_forecast, tab_sleeves, tab_trades = st.tabs(
        ["Overview", "Entry forecast", "Sleeves 2026", "Trades"]
    )

    with tab_overview:
        view = port[port.index >= VIEW_START]
        if not view.empty and port_base_2026:
            pct = (view / port_base_2026 - 1.0) * 100.0
            st.altair_chart(portfolio_pct_chart(pct), width="stretch")
        chart_stats = stats.loc[stats["sign"] != "flat"].copy()
        if not chart_stats.empty:
            st.subheader("Sleeve returns since 2026")
            st.altair_chart(ret_bar_chart(chart_stats), width="stretch")

    with tab_forecast:
        load_fc = st.button("Load entry forecasts", type="primary", key="load_forecasts")
        if load_fc:
            load_forecast.clear()
            st.session_state.pop("forecast_df", None)

        if load_fc:
            fc_bar = st.progress(0.0, text="Preparing forecasts…")

            def _progress(pct: float, text: str = "") -> None:
                fc_bar.progress(min(max(pct, 0.0), 1.0), text=text)

            built = build_forecast_rows(results, progress=_progress)
            fc_bar.empty()
            st.session_state["forecast_df"] = built

        forecast_df = st.session_state.get("forecast_df")
        if forecast_df is None:
            st.info(
                "Overview and **Sleeves 2026** are ready. "
                "Click **Load entry forecasts** (~1–3 min first time, then cached)."
            )
            forecast_df = pd.DataFrame()

        n_blocked = int((forecast_df["status"] == "BLOCKED").sum()) if not forecast_df.empty else 0

        if forecast_df.empty and load_fc:
            st.warning("Forecast failed — check terminal for errors, or run daily bot / ↻ full replay.")
        elif not forecast_df.empty:
            st.markdown(
                """
                <div class="forecast-legend">
                <b>How to read this</b> — not a price prediction.<br>
                <b>Gap</b>: bps to signal · <b>Med days</b>: typical wait from similar setups ·
                <b>P(7d/14d)</b>: historical hit rate · <b>Quality</b>: similar past trades vs sleeve avg.
                </div>
                """,
                unsafe_allow_html=True,
            )
            c_chart, c_table = st.columns([1, 2], gap="large")
            with c_chart:
                st.markdown("**Urgency** (P within 7d)")
                st.altair_chart(forecast_p7_chart(forecast_df), width="stretch")
            with c_table:
                fc_show = forecast_df[
                    [
                        "asset",
                        "status",
                        "timing",
                        "regime",
                        "gap_bps",
                        "med_days",
                        "p7_label",
                        "p14_label",
                        "quality",
                        "tier",
                        "hist_win",
                        "hist_med_pct",
                        "since_exit_d",
                        "blockers",
                    ]
                ].copy()
                st.dataframe(
                    fc_show,
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "asset": st.column_config.TextColumn("Asset", width="small"),
                        "status": st.column_config.TextColumn("Status", width="small"),
                        "timing": st.column_config.TextColumn("Est. timing", width="medium"),
                        "regime": st.column_config.TextColumn("Regime", width="small"),
                        "gap_bps": st.column_config.TextColumn("Gap bps", width="small"),
                        "med_days": st.column_config.NumberColumn("Med days", format="%d"),
                        "p7_label": st.column_config.TextColumn("P(7d)", width="small"),
                        "p14_label": st.column_config.TextColumn("P(14d)", width="small"),
                        "quality": st.column_config.NumberColumn("Score", format="%d"),
                        "tier": st.column_config.TextColumn("Tier", width="small"),
                        "hist_win": st.column_config.NumberColumn("Hist win%", format="%.0f"),
                        "hist_med_pct": st.column_config.NumberColumn("Hist med%", format="%.1f"),
                        "since_exit_d": st.column_config.NumberColumn("Days flat", format="%d"),
                        "blockers": st.column_config.TextColumn("Blockers", width="medium"),
                    },
                )

            action = forecast_df[forecast_df["status"].isin(["ENTER", "WATCH"])].head(3)
            if not action.empty:
                st.subheader("Closest to action")
                for _, row in action.iterrows():
                    st.markdown(
                        f"**{row['asset']}** · {row['status']} · {row['timing']} · "
                        f"gap {row['gap_bps']} bps · P(7d) {row['p7_label']} · "
                        f"quality {row['quality'] or '—'} ({row['tier']})"
                    )

            with st.expander("Telegram-style gate (per sleeve)"):
                for r in results:
                    st.text(
                        f"{r['symbol']}: {signal_status(r['latest'], r['strat_cfg'], r.get('open_position'), r.get('pending_entry'))}"
                    )

    with tab_sleeves:
        st.caption("2026 performance and live position — entry estimates are on the **Entry forecast** tab.")
        perf_show = perf_df[
            [
                "asset",
                "state",
                "pnl_2026",
                "ret_2026",
                "trades_26",
                "exposure",
                "to_signal",
                "unreal_pct",
                "exit_by",
                "last_exit",
            ]
        ]
        perf_styled = perf_show.style.map(color_signed, subset=["pnl_2026", "ret_2026", "unreal_pct"])
        st.dataframe(
            perf_styled,
            width="stretch",
            hide_index=True,
            column_config={
                "asset": st.column_config.TextColumn("Asset"),
                "state": st.column_config.TextColumn("Now", width="small"),
                "pnl_2026": st.column_config.NumberColumn("PnL $", format="$%d"),
                "ret_2026": st.column_config.NumberColumn("Return %", format="%.2f"),
                "trades_26": st.column_config.NumberColumn("Trades", format="%d"),
                "exposure": st.column_config.NumberColumn("In mkt %", format="%.1f"),
                "to_signal": st.column_config.TextColumn("To signal"),
                "unreal_pct": st.column_config.NumberColumn("Unreal %", format="%.1f"),
                "exit_by": st.column_config.TextColumn("Exit ≤"),
                "last_exit": st.column_config.TextColumn("Last exit"),
            },
        )

    with tab_trades:
        parts = []
        for r in results:
            t = r["trades"].copy()
            if t.empty:
                continue
            t["exit_date"] = pd.to_datetime(t["exit_date"], utc=True)
            t = t[t["exit_date"] >= VIEW_START].sort_values("exit_date", ascending=False)
            if not t.empty:
                p = t[
                    ["entry_date", "exit_date", "hold_days", "net_pnl", "open_to_exit_pct", "size_frac"]
                ].copy()
                p.insert(0, "asset", short_sym(r["symbol"]))
                parts.append(p)
        if parts:
            all_t = pd.concat(parts, ignore_index=True).sort_values("exit_date", ascending=False)
            all_t["net_pnl"] = pd.to_numeric(all_t["net_pnl"]).round(0)
            all_t["open_to_exit_pct"] = pd.to_numeric(all_t["open_to_exit_pct"]).round(1)
            all_t["size_frac"] = (pd.to_numeric(all_t["size_frac"]) * 100).round(0)
            trade_styled = all_t.head(30).style.map(color_signed, subset=["net_pnl", "open_to_exit_pct"])
            st.dataframe(trade_styled, width="stretch", hide_index=True)
        else:
            st.write("No exits yet in 2026.")

    st.caption(
        f"Updated {pd.Timestamp.utcnow():%Y-%m-%d %H:%M UTC} · "
        f"{n_blocked} blocked · 2026 view · forecasts cached 1h"
    )


if __name__ == "__main__":
    main()
