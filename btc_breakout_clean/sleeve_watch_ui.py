#!/usr/bin/env python3
"""Sleeve Watch — guided trade-flow walkthrough (not a metrics dashboard)."""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from btc_breakout_binance_paper_bot import LIVE_SYMBOLS, LIVE_SLEEVE_EQUITY
from sleeve_flow import (
    append_journal,
    audit_sleeve,
    book_flow_context,
    book_snapshot,
    build_action_queue,
    enrich_pipeline,
    load_journal,
    primary_instruction,
)


def _inject_flow_styles() -> None:
    st.markdown(
        """
        <style>
            .flow-hero {
                background: linear-gradient(135deg, #1a1a24 0%, #12121a 100%);
                border: 1px solid #3d3520;
                border-left: 4px solid #e8b84a;
                border-radius: 10px;
                padding: 1rem 1.25rem;
                margin-bottom: 1rem;
            }
            .flow-hero h3 {
                margin: 0 0 0.35rem 0;
                color: #f4f4f8;
                font-size: 1.15rem;
                letter-spacing: 0.04em;
            }
            .flow-hero p { margin: 0; color: #b8b8c4; font-size: 0.92rem; line-height: 1.5; }
            .flow-slots {
                display: flex; gap: 0.5rem; margin-top: 0.75rem; flex-wrap: wrap;
            }
            .flow-slot {
                width: 2.4rem; height: 2.4rem; border-radius: 6px;
                border: 1px solid #2a2a34; background: #0e0e12;
                display: flex; align-items: center; justify-content: center;
                font-size: 0.7rem; color: #6b7280;
            }
            .flow-slot.on {
                background: #1a2e1a; border-color: #3ecf8e; color: #3ecf8e;
                font-weight: 600;
            }
            .flow-step {
                display: flex; gap: 0.85rem; margin: 0.35rem 0;
                padding: 0.55rem 0.75rem; border-radius: 8px;
                border: 1px solid #22222c; background: #14141a;
            }
            .flow-step.done { border-color: #1e3a2a; opacity: 0.85; }
            .flow-step.current { border-color: #e8b84a; background: #1c1a14; }
            .flow-step.pending { opacity: 0.45; }
            .flow-num {
                flex-shrink: 0; width: 1.6rem; height: 1.6rem; border-radius: 50%;
                background: #2a2a34; color: #9ca3af; font-size: 0.75rem;
                display: flex; align-items: center; justify-content: center; font-weight: 600;
            }
            .flow-step.done .flow-num { background: #1a3d2a; color: #3ecf8e; }
            .flow-step.current .flow-num { background: #3d3520; color: #e8b84a; }
            .flow-body { flex: 1; min-width: 0; }
            .flow-body b { color: #ececf1; font-size: 0.92rem; }
            .flow-body .meta { color: #9ca3af; font-size: 0.82rem; margin-top: 0.15rem; }
            .flow-action-tag {
                display: inline-block; padding: 0.12rem 0.45rem; border-radius: 4px;
                font-size: 0.68rem; font-weight: 650; letter-spacing: 0.03em; margin-right: 0.35rem;
            }
            .tag-ENTER { background: #1a3d2a; color: #3ecf8e; }
            .tag-EXIT { background: #3d1a1a; color: #ef6b6b; }
            .tag-CAP_BLOCK { background: #3d2a1a; color: #e8b84a; }
            .tag-APPROACH { background: #1a2a3d; color: #5b8def; }
            .tag-FLAT { background: #22222c; color: #6b7280; }
            .tag-MISSING, .tag-ERROR { background: #3d1a2a; color: #f472b6; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _slot_html(book: dict[str, Any]) -> str:
    open_list = book.get("open_symbols") or []
    cells = []
    for i in range(book["max_concurrent"]):
        if i < len(open_list):
            sym = open_list[i]
            cells.append(f'<div class="flow-slot on" title="{sym}">{sym[:3]}</div>')
        else:
            cells.append('<div class="flow-slot">·</div>')
    return "".join(cells)


def render_sleeve_watch_tab(
    results: list[dict[str, Any]],
    *,
    blocked_by_sym: dict[str, frozenset[pd.Timestamp]],
) -> None:
    _inject_flow_styles()
    sym_map = {r["symbol"]: r for r in results}
    book = book_snapshot(results)
    queue = build_action_queue(results, blocked_by_sym)
    ctx = book_flow_context(book, queue)

    # --- 1. Book gate (always first) ---
    st.markdown(
        f"""
        <div class="flow-hero">
            <h3>① BOOK · {ctx["phase"]}</h3>
            <p>{ctx["instruction"]}</p>
            <div class="flow-slots">{_slot_html(book)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.caption(
        f"Max {book['max_concurrent']} concurrent · {book['open_count']} open · "
        f"{book['slots_free']} slot(s) free · pending entry: {book['pending_count']}"
    )

    # --- 2. Action queue (what needs you today) ---
    st.markdown("## ② Today's queue")
    st.caption("Work top → bottom. Select a row to walk the rule pipeline for that sleeve.")

    actionable = [q for q in queue if q["action"] not in ("FLAT", "MISSING", "ERROR")]
    if not actionable:
        st.info("Nothing requires action right now. Expand a flat sleeve below to verify rules.")
    else:
        for q in actionable:
            tag = q["action"]
            label = f"{q['symbol']} — {q['headline']}"
            if st.button(label, key=f"flow_pick_{q['symbol']}", width="stretch"):
                st.session_state["flow_symbol"] = q["symbol"]

    with st.expander("All sleeves (flat / scan)", expanded=False):
        for q in queue:
            if q["action"] in ("FLAT", "MISSING", "ERROR"):
                st.markdown(
                    f'<span class="flow-action-tag tag-{q["action"]}">{q["action"]}</span> '
                    f"**{q['symbol']}** — {q['headline']}",
                    unsafe_allow_html=True,
                )
                if st.button(f"Inspect {q['symbol']}", key=f"flow_flat_{q['symbol']}"):
                    st.session_state["flow_symbol"] = q["symbol"]

    # Default focus: highest-priority actionable, else first symbol
    if "flow_symbol" not in st.session_state:
        st.session_state["flow_symbol"] = (
            actionable[0]["symbol"] if actionable else LIVE_SYMBOLS[0]
        )

    # --- 3. Sleeve walkthrough ---
    st.markdown("## ③ Sleeve walkthrough")
    symbol = st.radio(
        "Focus",
        LIVE_SYMBOLS,
        index=LIVE_SYMBOLS.index(st.session_state.get("flow_symbol", LIVE_SYMBOLS[0])),
        horizontal=True,
        key="flow_symbol_radio",
        label_visibility="collapsed",
    )
    st.session_state["flow_symbol"] = symbol

    _render_walkthrough(symbol, sym_map, blocked_by_sym, queue)


def _render_walkthrough(
    symbol: str,
    sym_map: dict[str, dict[str, Any]],
    blocked_by_sym: dict[str, frozenset[pd.Timestamp]],
    queue: list[dict[str, Any]],
) -> None:
    r = sym_map.get(symbol)
    action = next((q for q in queue if q["symbol"] == symbol.upper()), None)
    if not r:
        st.error("No paper state for this sleeve. Run `./btc_breakout_clean/run_dashboard.sh --daily`.")
        return

    blocked = blocked_by_sym.get(symbol.upper(), frozenset())
    audit = audit_sleeve(
        symbol,
        latest=r.get("latest") or {},
        strat_cfg=r["strat_cfg"],
        pending_entry=r.get("pending_entry"),
        open_position=r.get("open_position"),
        blocked_dates=blocked,
        include_provisional=(symbol.upper() in {"BTCUSD", "ETHUSDT", "BNBUSDT", "SOLUSDT", "DOGEUSDT"}),
    )
    act = action or {"action": "FLAT", "headline": audit["state"]}
    instruction = primary_instruction(audit, act)

    st.markdown(f"### {symbol}")
    st.markdown(
        f'<span class="flow-action-tag tag-{act["action"]}">{act["action"]}</span> '
        f"{act.get('headline', audit['state'])}",
        unsafe_allow_html=True,
    )
    st.success(instruction)

    if audit.get("expected_notional", 0) > 0:
        st.caption(
            f"Deploy ≈ ${audit['expected_notional']:,.0f} "
            f"({100 * audit['expected_notional'] / LIVE_SLEEVE_EQUITY:.0f}% of ${LIVE_SLEEVE_EQUITY:,.0f} sleeve)"
        )

    if audit.get("provisional_note"):
        prov_sig = audit.get("provisional_signal")
        bps = audit.get("provisional_breakout_bps")
        st.warning(
            f"Intraday preview only (not official): {audit['provisional_note']} · "
            f"signal={'YES' if prov_sig else 'NO'} · "
            f"breakout={f'{bps:.0f} bps' if bps is not None else 'n/a'}"
        )

    st.caption(
        f"Official bar **{audit['official_bar_date']}** · {audit['data_source']} · "
        f"as of {audit['as_of_utc']} · chart: {audit['tv_chart']}"
    )

    st.markdown("#### Rule pipeline")
    st.caption("Top to bottom = decision order. **Gold** = current gate.")

    pipeline = enrich_pipeline(audit["steps"])
    for i, step in enumerate(pipeline, start=1):
        status = step.get("status", "pending")
        icon = "✓" if status == "done" else ("→" if status == "current" else "·")
        pass_txt = "pass" if step["pass"] else "fail"
        detail = f" · {step['detail']}" if step.get("detail") else ""
        st.markdown(
            f"""
            <div class="flow-step {status}">
                <div class="flow-num">{icon}</div>
                <div class="flow-body">
                    <b>{i}. {step['label']}</b>
                    <div class="meta">{pass_txt} · expected <i>{step['expected']}</i> · actual <i>{step['actual']}</i>{detail}</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    if r.get("open_position"):
        op = r["open_position"]
        st.markdown(
            f"**Position:** LONG from {op.get('entry_date')} · "
            f"hold {op.get('hold_day')}/{op.get('hold_max', op.get('hold_days'))} · "
            f"exit ≤ {op.get('exit_target', '—')}"
        )
    elif r.get("pending_entry"):
        pe = r["pending_entry"]
        st.markdown(f"**Armed:** SIG {pe.get('signal_date')} → enter next open")

    # --- 4. Log (only when trading) ---
    if act["action"] in ("ENTER", "EXIT", "CAP_BLOCK", "ARMED"):
        st.markdown("#### ④ Confirm & log")
        ev_default = "ENTRY_FILL" if act["action"] == "ENTER" else (
            "EXIT_FILL" if "fade" in act["headline"].lower() or act["action"] == "EXIT" else "SIG_CONFIRMED"
        )
        ev = st.selectbox(
            "Event",
            ["ENTRY_FILL", "EXIT_FILL", "SIG_CONFIRMED", "CAP_BLOCK", "MISMATCH", "MANUAL_NOTE"],
            index=["ENTRY_FILL", "EXIT_FILL", "SIG_CONFIRMED", "CAP_BLOCK", "MISMATCH", "MANUAL_NOTE"].index(
                ev_default
            ),
            key=f"flow_ev_{symbol}",
        )
        c1, c2 = st.columns(2)
        with c1:
            exp = st.text_input("Expected", value=instruction[:80], key=f"flow_exp_{symbol}")
        with c2:
            act_txt = st.text_input("Actual fill / note", key=f"flow_act_{symbol}")
        note = st.text_input("Note (optional)", key=f"flow_note_{symbol}")
        if st.button("Save", key=f"flow_save_{symbol}", type="primary"):
            append_journal(symbol, event=ev, note=note, expected=exp, actual=act_txt)
            st.toast("Logged.")
            st.rerun()

        journal = load_journal(symbol, limit=8)
        if journal:
            st.dataframe(
                pd.DataFrame(journal)[["ts_utc", "event", "expected", "actual"]],
                hide_index=True,
                width="stretch",
            )
