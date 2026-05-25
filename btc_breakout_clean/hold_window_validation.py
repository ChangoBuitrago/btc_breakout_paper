#!/usr/bin/env python3
"""
Hold-window sweep on daily bars (live engine unchanged).

Phases:
  1. Book-wide scale on hold_min / hold_max (0.75× … 1.5×)
  2. Asset-class presets (crypto shorter/longer, commodities shorter/longer)
  3. Fixed calendar holds (5/10/15/20d, no dynamic fade)
  4. Asymmetric crypto window (low min, high max)

Run: python3 btc_breakout_clean/hold_window_validation.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from btc_breakout_binance_paper_bot import (  # noqa: E402
    LIVE_CRYPTO_SYMBOLS,
    LIVE_MAX_CONCURRENT_ENTRIES,
    LIVE_SYMBOLS,
    live_strategy_config,
)
from strategy_validation import (  # noqa: E402
    beats_baseline,
    portfolio_metrics,
    preload_raw,
    run_full_book_live,
)

OUT_PATH = HERE / "hold_window_validation_results.json"
METALS_OIL = ("XAUUSD", "XAGUSD", "BRENT")


def scale_holds(cfg: Any, mult: float, *, dynamic: bool = True) -> Any:
    hmin = max(1, int(round((cfg.hold_min or cfg.hold_days) * mult)))
    hmax = max(hmin + 1, int(round((cfg.hold_max or hmin) * mult)))
    return replace(
        cfg,
        hold_min=hmin,
        hold_max=hmax,
        hold_days=hmin,
        dynamic_hold=dynamic and hmax > hmin,
    )


def build_scaled(mult: float, *, dynamic: bool = True) -> dict[str, Any]:
    return {s: scale_holds(live_strategy_config(s), mult, dynamic=dynamic) for s in LIVE_SYMBOLS}


def build_per_symbol(overrides: dict[str, dict[str, int]], *, dynamic: bool = True) -> dict[str, Any]:
    strats: dict[str, Any] = {}
    for s in LIVE_SYMBOLS:
        base = live_strategy_config(s)
        if s not in overrides:
            strats[s] = base
            continue
        kw = overrides[s]
        hmin = int(kw["hold_min"])
        hmax = int(kw["hold_max"])
        strats[s] = replace(
            base,
            hold_min=hmin,
            hold_max=hmax,
            hold_days=hmin,
            dynamic_hold=dynamic and hmax > hmin,
        )
    return strats


def live_holds(symbol: str) -> tuple[int, int]:
    cfg = live_strategy_config(symbol)
    hmin = int(cfg.hold_min or cfg.hold_days)
    hmax = int(cfg.hold_max or hmin)
    return hmin, hmax


def crypto_preset(short: bool) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for s in LIVE_CRYPTO_SYMBOLS:
        hmin, hmax = live_holds(s)
        if short:
            out[s] = {"hold_min": max(2, hmin - 2), "hold_max": max(hmin, hmax - 3)}
        else:
            out[s] = {"hold_min": hmin + 2, "hold_max": hmax + 5}
    return out


def comm_preset(short: bool) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for s in METALS_OIL:
        hmin, hmax = live_holds(s)
        if short:
            out[s] = {"hold_min": max(3, hmin - 3), "hold_max": max(hmin + 1, hmax - 2)}
        else:
            out[s] = {"hold_min": hmin, "hold_max": hmax + 5}
    return out


def asymmetric_crypto() -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for s in LIVE_CRYPTO_SYMBOLS:
        hmin, hmax = live_holds(s)
        out[s] = {"hold_min": max(2, hmin - 2), "hold_max": hmax + 7}
    return out


def run_case(
    raw: dict,
    *,
    label: str,
    category: str,
    strats: dict[str, Any],
    note: str = "",
) -> dict[str, Any]:
    curves, _, trades, eq = run_full_book_live(
        raw, tuple(LIVE_SYMBOLS), strats, max_concurrent=LIVE_MAX_CONCURRENT_ENTRIES
    )
    initial = sum(eq.values())
    m = portfolio_metrics(curves, trades, initial)
    hold_stats: dict[str, Any] = {}
    if not trades.empty:
        t = trades.copy()
        t["entry_date"] = __import__("pandas").to_datetime(t["entry_date"], utc=True)
        t["exit_date"] = __import__("pandas").to_datetime(t["exit_date"], utc=True)
        t["hold_days"] = (t["exit_date"] - t["entry_date"]).dt.days
        hold_stats = {
            "median_hold_days": float(t["hold_days"].median()),
            "mean_hold_days": float(t["hold_days"].mean()),
        }
        if "exit_reason" in t.columns:
            hold_stats["exit_reasons"] = {
                str(k): int(v) for k, v in t["exit_reason"].value_counts().items()
            }
    return {
        "label": label,
        "category": category,
        "note": note,
        "metrics": m,
        "trades": int(m.get("trades", 0)),
        "hold_stats": hold_stats,
    }


def main() -> None:
    print("Hold-window validation (daily bars, live unchanged)", flush=True)
    raw = preload_raw(tuple(LIVE_SYMBOLS))

    variants: list[tuple[str, str, dict[str, Any], str]] = [
        ("live_baseline", "baseline", build_scaled(1.0), "Current per-sleeve min/max"),
        ("scale_0p75", "scale", build_scaled(0.75), "All sleeves 0.75× hold window"),
        ("scale_1p25", "scale", build_scaled(1.25), "All sleeves 1.25× hold window"),
        ("scale_1p5", "scale", build_scaled(1.5), "All sleeves 1.5× hold window"),
        ("crypto_shorter", "asset_class", build_per_symbol(crypto_preset(True)), "Crypto −2/−3 days"),
        ("crypto_longer", "asset_class", build_per_symbol(crypto_preset(False)), "Crypto +2/+5 days"),
        ("comm_shorter", "asset_class", build_per_symbol(comm_preset(True)), "Metals/oil shorter max"),
        ("comm_longer", "asset_class", build_per_symbol(comm_preset(False)), "Metals/oil +5d max"),
        ("crypto_asymmetric", "asymmetric", build_per_symbol(asymmetric_crypto()), "Crypto low min, +7d max"),
    ]
    for days in (5, 10, 15, 20):
        fixed = {
            s: replace(
                live_strategy_config(s),
                hold_min=days,
                hold_max=days,
                hold_days=days,
                dynamic_hold=False,
            )
            for s in LIVE_SYMBOLS
        }
        variants.append(
            (f"fixed_hold_{days}d", "fixed", fixed, f"Fixed {days}d exit, no momentum fade"),
        )

    rows: list[dict[str, Any]] = []
    for label, cat, strats, note in variants:
        row = run_case(raw, label=label, category=cat, strats=strats, note=note)
        if label == "live_baseline":
            row["passes_baseline"] = True
            bm = row["metrics"]
        else:
            row["passes_baseline"] = beats_baseline(bm, row["metrics"])
        rows.append(row)
        m = row["metrics"]
        hs = row.get("hold_stats", {})
        med = hs.get("median_hold_days", float("nan"))
        print(
            f"  {label:22} ret={m['return_pct']:6.1f}% DD={m['max_drawdown_pct']:6.2f}% "
            f"PF={m['profit_factor']:.2f} Sh={m['sharpe_ratio']:.2f} tr={row['trades']:3} "
            f"med_hold={med:4.0f} pass={row['passes_baseline']}",
            flush=True,
        )

    passing = [r["label"] for r in rows if r.get("passes_baseline") and r["label"] != "live_baseline"]
    payload = {
        "baseline_label": "live_baseline",
        "baseline_metrics": bm,
        "live_hold_ranges": {s: {"hold_min": live_holds(s)[0], "hold_max": live_holds(s)[1]} for s in LIVE_SYMBOLS},
        "variants": rows,
        "passing_labels": passing,
        "note": "Daily bar holds only; see timeframe_validation.py for 1h/4h pilot.",
    }
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nPassing: {passing or ['(none)']}")
    print(f"Wrote {OUT_PATH}", flush=True)


if __name__ == "__main__":
    main()
