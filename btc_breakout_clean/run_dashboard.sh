#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# Simona paper / live runner
#
# Usage:
#   ./run_dashboard.sh                  normal: update paper portfolio → open dashboard
#   ./run_dashboard.sh --no-update      skip daily run, open dashboard with last state
#   ./run_dashboard.sh --orders         also dry-run order log (TWS must be open)
#   ./run_dashboard.sh --live           LIVE mode: real orders via IBKR TWS live port
#   ./run_dashboard.sh --telegram       send Telegram summary after daily run
#   ./run_dashboard.sh --refresh        force re-download all data caches
#   ./run_dashboard.sh --health         print IBKR sleeve table, then continue
#
# Standalone:  python ibkr_health_check.py
#
# Paper → live requires exactly one change:  --live flag (or IBKR_MODE=live env var).
# Everything else — signal logic, sizing, data feed — is identical.
#
# IBKR_MODE / port reference:
#   paper  TWS port 7497  (default)
#   live   TWS port 7496
#   Set IBKR_HOST / IBKR_PORT to override if using IB Gateway (4002/4001).
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"
VENV="$HERE/.venv-dashboard"

# ── Find Python ≥ 3.10 ───────────────────────────────────────────────────────
PY=""
for c in python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    minor="$("$c" -c 'import sys; print(sys.version_info.minor)')"
    maj="$("$c"   -c 'import sys; print(sys.version_info.major)')"
    if [ "$maj" -ge 3 ] && [ "$minor" -ge 10 ]; then
      PY="$c"; break
    fi
  fi
done
[ -n "$PY" ] || { echo "Need Python >= 3.10  (brew install python@3.12)"; exit 1; }

# ── Auto-create / repair venv ─────────────────────────────────────────────────
if [ ! -d "$VENV" ]; then
  echo "Creating venv…"
  "$PY" -m venv "$VENV"
  source "$VENV/bin/activate"
  pip install -q -r requirements-dashboard.txt
else
  source "$VENV/bin/activate"
  python -c "import streamlit, pandas, altair, ib_async" 2>/dev/null \
    || pip install -q -r requirements-dashboard.txt
fi

# ── Parse flags ───────────────────────────────────────────────────────────────
UPDATE=1 TELEGRAM=0 REFRESH=0 ORDERS=0 LIVE=0 HEALTH=1
for arg in "$@"; do
  case "$arg" in
    --no-update)  UPDATE=0   ;;
    --no-health)  HEALTH=0   ;;
    --telegram)   TELEGRAM=1 ;;
    --refresh)    REFRESH=1  ;;
    --orders)     ORDERS=1   ;;
    --live)       LIVE=1     ;;
    --health)     HEALTH=1   ;;
  esac
done

# Live mode: export env var so all sub-processes (data fetch + order execution) see it.
if [ "$LIVE" -eq 1 ]; then
  export IBKR_MODE=live
  echo "⚡ LIVE MODE — real orders will be placed via IBKR TWS (port 7496)"
else
  export IBKR_MODE=paper
fi

# ── IBKR health (one connection, ~10s) ───────────────────────────────────────
if [ "$HEALTH" -eq 1 ]; then
  python ibkr_health_check.py || true
fi

# ── Daily update ─────────────────────────────────────────────────────────────
if [ "$UPDATE" -eq 1 ]; then
  CMD=(python run_binance_paper_daily.py --state-dir paper_portfolio --quiet)
  [ "$TELEGRAM" -eq 0 ] && CMD+=(--no-telegram)
  [ "$REFRESH"  -eq 1 ] && CMD+=(--refresh-cache)
  [ "$ORDERS"   -eq 1 ] && CMD+=(--execute-orders)
  # In paper mode with --orders, force dry-run so nothing real fires accidentally.
  [ "$LIVE"     -eq 0 ] && [ "$ORDERS" -eq 1 ] && CMD+=(--dry-run-orders)

  echo "Updating paper portfolio  [IBKR_MODE=$IBKR_MODE]…"
  "${CMD[@]}" || echo "Warning: daily update failed — showing last saved state."
fi

# ── Kill any stale dashboard process ─────────────────────────────────────────
PORT=8501
if lsof -ti:"$PORT" >/dev/null 2>&1; then
  lsof -ti:"$PORT" | xargs kill -15 2>/dev/null || true
  sleep 1
  lsof -ti:"$PORT" >/dev/null 2>&1 && lsof -ti:"$PORT" | xargs kill -9 2>/dev/null || true
fi

# ── Launch dashboard ──────────────────────────────────────────────────────────
exec python -m streamlit run paper_dashboard.py --server.port "$PORT"
