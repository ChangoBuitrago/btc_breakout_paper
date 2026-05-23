#!/usr/bin/env python3
"""Print concise summary from validation_results.json."""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATH = HERE / "validation_results.json"


def main() -> None:
    if not PATH.exists():
        print(f"Missing {PATH}. Run validation.py first.", file=sys.stderr)
        sys.exit(1)
    data = json.loads(PATH.read_text(encoding="utf-8"))
    print(f"Regime-off v{data.get('version', 1)} — {data.get('variants_passing_discovery', 0)} passing / {data.get('variants_run', 0)} variants\n")
    for line in data.get("discovery_lines", []):
        print(line)
    print("\n--- Passing only (best per symbol) ---")
    best = data.get("best_by_symbol_mechanism", {})
    for sym, mechs in best.items():
        for mech, row in mechs.items():
            if row.get("passes_discovery"):
                print(
                    f"  {sym} {mech}: ret={row['full']['return_pct']:.1f}% "
                    f"PF={row['full']['profit_factor']:.2f} ex={row['ex_2024']['trades']}t "
                    f"exPF={row['ex_2024'].get('profit_factor', float('nan')):.2f} "
                    f"DD={row['full']['max_drawdown_pct']:.1f}%"
                )


if __name__ == "__main__":
    main()
