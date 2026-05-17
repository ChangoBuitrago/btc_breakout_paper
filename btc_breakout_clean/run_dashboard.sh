#!/usr/bin/env bash
# Run the paper portfolio dashboard (needs Python >= 3.10 for dukascopy-python).
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

DASHBOARD_PORT=8501
if lsof -ti:"${DASHBOARD_PORT}" >/dev/null 2>&1; then
  echo "Stopping existing process on port ${DASHBOARD_PORT}..." >&2
  lsof -ti:"${DASHBOARD_PORT}" | xargs kill -9 2>/dev/null || true
  sleep 1
fi

exec python -m streamlit run paper_dashboard.py --server.port "${DASHBOARD_PORT}" "$@"
