#!/usr/bin/env python3
"""Print ranked summary from candidate_backtest_results.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "candidate_backtest_results.json"
DEEP = HERE / "candidate_assets_deep_screen_results.json"


def main() -> None:
    if not RESULTS.exists():
        print("No candidate_backtest_results.json", file=sys.stderr)
        sys.exit(1)
    d = json.loads(RESULTS.read_text(encoding="utf-8"))
    base = d["baseline"]
    print(f"\nBASELINE: ret={base['return_pct']:.2f}% profit=${base['net_profit']:,.0f} DD={base['max_drawdown_pct']:.2f}%\n")

    rows: list[dict] = []
    for sym, pack in d.get("book", {}).items():
        solo = next((s for s in d["solo"] if s["symbol"] == sym), {})
        ex = solo.get("windows", {}).get("ex_2024", {})
        fair = next((s for s in pack["scenarios"] if "fair" in s["label"]), None)
        if not fair:
            continue
        rows.append(
            {
                "symbol": sym,
                "ex_pf": ex.get("profit_factor"),
                "ex_trades": ex.get("trades", 0),
                "ex_pnl": ex.get("net_pnl", 0),
                "solo_dd": solo.get("max_drawdown_pct"),
                "fair_ret": fair["return_pct"],
                "fair_dprofit": fair["delta_profit_usd"],
                "fair_dd": fair["max_drawdown_pct"],
            }
        )

    rows.sort(key=lambda r: (r["fair_dprofit"], r["ex_pnl"] or 0), reverse=True)
    print(f"{'symbol':10} {'exPF':>7} {'exT':>4} {'exPnL':>9} {'soloDD':>7} {'fairΔ$':>10} {'fairRet':>7}")
    for r in rows:
        pf = r["ex_pf"]
        pfs = f"{pf:.2f}" if pf == pf else "  —"
        print(
            f"{r['symbol']:10} {pfs:>7} {r['ex_trades']:4d} ${r['ex_pnl']:8,.0f} "
            f"{r['solo_dd']:7.2f}% ${r['fair_dprofit']:+9,.0f} {r['fair_ret']:7.2f}%"
        )

    winners = [r for r in rows if r["fair_dprofit"] >= 0]
    print(f"\nFair BTC swap >= baseline profit: {len(winners)} / {len(rows)}")
    for r in winners[:10]:
        print(f"  {r['symbol']}: +${r['fair_dprofit']:,.0f}")

    if DEEP.exists():
        deep = json.loads(DEEP.read_text(encoding="utf-8"))
        print(f"\nDeep screen ranked (ex-2024 score, top 15):")
        for row in deep.get("deep_optimized_top", [])[:15]:
            ex = row["solo"]["ex_2024"]
            print(
                f"  {row['symbol']:10} exPF={ex['pf']:.2f} exPnL=${ex['pnl']:,.0f} "
                f"corr={row.get('corr_ex_2024_vs_book', float('nan')):.2f} "
                f"pass={row.get('pass_count', 0)}"
            )


if __name__ == "__main__":
    main()
