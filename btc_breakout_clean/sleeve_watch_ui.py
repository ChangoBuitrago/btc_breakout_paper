#!/usr/bin/env python3
"""Streamlit UI for Sleeve Watch (trade-flow auditor tab)."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from btc_breakout_binance_paper_bot import LIVE_SYMBOLS, LIVE_SLEEVE_EQUITY
from run_binance_paper_daily import signal_status
from sleeve_flow import append_journal, audit_sleeve, book_snapshot, load_journal, sleeve_brief_state


def _step_icon(passed: bool) -> str:
    return "✅" if passed else "❌"


def render_sleeve_watch_tab(
    results: list[dict[str, Any]],
    *,
    blocked_by_sym: dict[str, frozenset[pd.Timestamp]],
) -> None:
    st.markdown(
        """
        <div class="forecast-legend">
        <b>Sleeve Watch</b> — parallel auditor for manual validation (no orders).<br>
        <b>Official</b> steps use last <b>closed daily bar</b> (same as daily paper bot).<br>
        <b>Provisional</b> (crypto) uses Binance developing 1d — <b>not</b> official SIG until UTC close.<br>
        Log <b>Actual</b> fills when you trade live to compare vs expected.
        </div>
        """,
        unsafe_allow_html=True,
    )

    book = book_snapshot(results)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Open sleeves", f"{book['open_count']}/{book['max_concurrent']}")
    c2.metric("Pending entry", book["pending_count"])
    c3.metric("Slots free", book["slots_free"])
    pending_txt = ", ".join(book["pending_symbols"]) if book["pending_symbols"] else "—"
    c4.metric("Enter soon", pending_txt[:24] + ("…" if len(pending_txt) > 24 else ""))

    sym_map = {r["symbol"]: r for r in results}
    missing = [s for s in LIVE_SYMBOLS if s not in sym_map]
    if missing:
        st.warning(f"No saved state for: {', '.join(missing)} — run daily bot or ↻ full replay.")

    st.markdown("#### All sleeves")
    row_a, row_b = st.columns(4), st.columns(4)
    for i, sym in enumerate(LIVE_SYMBOLS):
        col = (row_a if i < 4 else row_b)[i % 4]
        r = sym_map.get(sym)
        with col:
            if not r:
                st.metric(sym, "—", help="missing")
                continue
            latest = r.get("latest") or {}
            blocked = blocked_by_sym.get(sym.upper(), frozenset())
            brief = sleeve_brief_state(
                latest=latest,
                strat_cfg=r["strat_cfg"],
                pending_entry=r.get("pending_entry"),
                open_position=r.get("open_position"),
                blocked_dates=blocked,
            )
            sig = "SIG" if latest.get("signal") else "—"
            bps = latest.get("breakout_bps")
            bps_txt = f"{bps:.0f}bp" if bps is not None else ""
            st.metric(sym, brief[:22], delta=f"{sig} {bps_txt}".strip() or None)

    default_ix = 0
    for i, s in enumerate(LIVE_SYMBOLS):
        if sym_map.get(s, {}).get("pending_entry") or sym_map.get(s, {}).get("open_position"):
            default_ix = i
            break

    col_sel, col_ref = st.columns([2, 1])
    with col_sel:
        symbol = st.selectbox("Sleeve", LIVE_SYMBOLS, index=default_ix, key="watch_symbol")
    with col_ref:
        auto = st.checkbox("Auto-refresh 60s", value=False, key="watch_auto_refresh")
        if auto:
            st.caption("Refreshing…")

    if auto:

        @st.fragment(run_every=60)
        def _auto_body() -> None:
            _render_watch_body(symbol, sym_map, blocked_by_sym)

        _auto_body()
    else:
        _render_watch_body(symbol, sym_map, blocked_by_sym)


def _render_watch_body(
    symbol: str,
    sym_map: dict[str, dict[str, Any]],
    blocked_by_sym: dict[str, frozenset[pd.Timestamp]],
) -> None:
    r = sym_map.get(symbol)
    if not r:
        st.warning("No data for this sleeve — run ↻ or daily bot.")
        return

    strat_cfg = r["strat_cfg"]
    latest = r.get("latest") or {}
    pending = r.get("pending_entry")
    open_pos = r.get("open_position")

    blocked = blocked_by_sym.get(symbol.upper(), frozenset())
    audit = audit_sleeve(
        symbol,
        latest=latest,
        strat_cfg=strat_cfg,
        pending_entry=pending,
        open_position=open_pos,
        blocked_dates=blocked,
        include_provisional=True,
    )

    st.subheader(f"{symbol} — {audit['state']}")
    st.caption(
        f"{audit['as_of_utc']} · {audit['data_source']} · official bar {audit['official_bar_date']} · "
        f"TV: {audit['tv_chart']}"
    )

    if audit.get("expected_notional", 0) > 0:
        st.info(f"Expected deploy ≈ **${audit['expected_notional']:,.0f}** ({100*audit['expected_notional']/LIVE_SLEEVE_EQUITY:.0f}% of sleeve)")

    if audit.get("provisional_note"):
        prov_sig = audit.get("provisional_signal")
        prov_bps = audit.get("provisional_breakout_bps")
        sig_txt = "YES" if prov_sig else "NO"
        bps_txt = f"{prov_bps:.0f} bps" if prov_bps is not None else "n/a"
        st.warning(f"**Provisional:** {audit['provisional_note']} · signal={sig_txt} · breakout={bps_txt}")

    status_line = signal_status(latest, strat_cfg, open_pos, pending)
    st.markdown(f"**Daily bot status:** {status_line}")

    st.markdown("#### Rule checklist (official close)")
    for step in audit["steps"]:
        icon = _step_icon(step["pass"])
        detail = f" — {step['detail']}" if step.get("detail") else ""
        st.markdown(
            f"{icon} **{step['label']}** · expected: _{step['expected']}_ · actual: _{step['actual']}_{detail}"
        )

    st.markdown("#### Expected vs actual (your log)")
    journal = load_journal(symbol, limit=20)
    if journal:
        jdf = pd.DataFrame(journal)
        show_cols = [c for c in ("ts_utc", "event", "expected", "actual", "note") if c in jdf.columns]
        st.dataframe(jdf[show_cols], width="stretch", hide_index=True)
    else:
        st.caption("No journal entries yet — log fills and confirmations below.")

    with st.expander("Log actual event", expanded=False):
        ev = st.selectbox(
            "Event",
            ["ENTRY_FILL", "EXIT_FILL", "SIG_CONFIRMED", "CAP_BLOCK", "MANUAL_NOTE", "MISMATCH"],
            key=f"j_ev_{symbol}",
        )
        exp = st.text_input("Expected", key=f"j_exp_{symbol}", placeholder="e.g. enter 2026-05-27 open ~$12.5k")
        act = st.text_input("Actual", key=f"j_act_{symbol}", placeholder="e.g. filled $0.1423 on Binance")
        note = st.text_area("Note", key=f"j_note_{symbol}", height=68)
        if st.button("Save to journal", key=f"j_save_{symbol}"):
            append_journal(symbol, event=ev, note=note, expected=exp, actual=act)
            st.success("Saved.")
            st.rerun()

    st.markdown("#### Timeline")
    if open_pos:
        st.write(
            f"**LONG** from {open_pos.get('entry_date')} @ {open_pos.get('entry_px', 0):,.4g} · "
            f"hold {open_pos.get('hold_day')}/{open_pos.get('hold_max')} · "
            f"exit target ≤ {open_pos.get('exit_target')}"
        )
    elif pending:
        st.write(f"**ARMED** — SIG {pending.get('signal_date')} → enter next session open")
    elif audit.get("gap_to_buffer_bps") is not None:
        st.write(f"**FLAT** — {audit['gap_to_buffer_bps']:.0f} bps below buffer (regime must stay on)")
