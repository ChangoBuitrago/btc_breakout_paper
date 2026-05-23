#!/usr/bin/env bash
# Streamlit paper portfolio dashboard (Python >= 3.10).
#
# First-time setup (once):
#   python3.12 -m venv btc_breakout_clean/.venv-dashboard
#   source btc_breakout_clean/.venv-dashboard/bin/activate
#   pip install -r btc_breakout_clean/requirements-dashboard.txt
#
# Options (before any streamlit args):
#   --daily            Run run_binance_paper_daily.py before dashboard
#   --telegram         With --daily: send Telegram (needs TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)
#   --refresh-cache    With --daily: re-download Dukascopy H1 caches (slow)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HERE="$ROOT/btc_breakout_clean"
cd "$HERE"
VENV="$HERE/.venv-dashboard"

PY=""
for candidate in python3.12 python3.11 python3.10 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    ver="$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
    major="${ver%%.*}"
    minor="${ver#*.}"
    if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
      PY="$candidate"
      break
    fi
  fi
done

if [ -z "$PY" ]; then
  echo "No Python >= 3.10 found. Install one, e.g.: brew install python@3.12" >&2
  exit 1
fi

if [ ! -d "$VENV" ]; then
  echo "No venv at btc_breakout_clean/.venv-dashboard" >&2
  echo "Create it: $PY -m venv $VENV && source $VENV/bin/activate && pip install -r requirements-dashboard.txt" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$VENV/bin/activate"
if ! python -c "import streamlit, pandas, altair" 2>/dev/null; then
  echo "Missing dashboard deps. Run: pip install -r requirements-dashboard.txt" >&2
  exit 1
fi

RUN_DAILY=0
SEND_TELEGRAM=0
REFRESH_CACHE=0
STREAMLIT_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --daily)
      RUN_DAILY=1
      shift
      ;;
    --telegram)
      SEND_TELEGRAM=1
      shift
      ;;
    --refresh-cache)
      REFRESH_CACHE=1
      shift
      ;;
    *)
      STREAMLIT_ARGS+=("$1")
      shift
      ;;
  esac
done

if [ "$RUN_DAILY" -eq 1 ]; then
  echo "Running daily paper bot (updates paper_portfolio/)…" >&2
  DAILY_CMD=(python run_binance_paper_daily.py --state-dir paper_portfolio --quiet)
  if [ "$SEND_TELEGRAM" -eq 0 ]; then
    DAILY_CMD+=(--no-telegram)
  fi
  if [ "$REFRESH_CACHE" -eq 1 ]; then
    DAILY_CMD+=(--refresh-cache)
  fi
  if ! "${DAILY_CMD[@]}"; then
    echo "Warning: daily paper bot failed; starting dashboard with last saved state." >&2
  fi
fi

DASHBOARD_PORT=8501
if lsof -ti:"${DASHBOARD_PORT}" >/dev/null 2>&1; then
  echo "Stopping existing process on port ${DASHBOARD_PORT}..." >&2
  # TERM first (graceful); -9 only if still bound (avoids killing your own shell mid-exec)
  lsof -ti:"${DASHBOARD_PORT}" | xargs kill -15 2>/dev/null || true
  sleep 2
  if lsof -ti:"${DASHBOARD_PORT}" >/dev/null 2>&1; then
    lsof -ti:"${DASHBOARD_PORT}" | xargs kill -9 2>/dev/null || true
    sleep 1
  fi
fi

if [ "${#STREAMLIT_ARGS[@]}" -gt 0 ]; then
  exec python -m streamlit run paper_dashboard.py --server.port "${DASHBOARD_PORT}" "${STREAMLIT_ARGS[@]}"
else
  exec python -m streamlit run paper_dashboard.py --server.port "${DASHBOARD_PORT}"
fi
