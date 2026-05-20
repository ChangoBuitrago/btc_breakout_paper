#!/usr/bin/env bash
# Run daily paper bot (updates state.json + latest signals), then Streamlit dashboard.
# Needs Python >= 3.10 for dukascopy-python.
#
# Options (passed before any streamlit args):
#   --skip-daily       Skip run_binance_paper_daily.py (dashboard only)
#   --refresh-cache    Re-download Dukascopy H1 caches (slow)
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
  echo "Creating venv at btc_breakout_clean/.venv-dashboard with $PY"
  "$PY" -m venv "$VENV"
fi
# shellcheck source=/dev/null
source "$VENV/bin/activate"
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements-dashboard.txt

SKIP_DAILY=0
REFRESH_CACHE=0
STREAMLIT_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --skip-daily)
      SKIP_DAILY=1
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

if [ "$SKIP_DAILY" -eq 0 ]; then
  echo "Running daily paper bot (updates paper_portfolio/)…" >&2
  DAILY_CMD=(python run_binance_paper_daily.py --state-dir paper_portfolio --no-telegram --quiet)
  if [ "$REFRESH_CACHE" -eq 1 ]; then
    DAILY_CMD+=(--refresh-cache)
  fi
  if ! "${DAILY_CMD[@]}"; then
    echo "Warning: daily paper bot failed; starting dashboard with last saved state." >&2
  fi
else
  echo "Skipping daily paper bot (--skip-daily)." >&2
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
