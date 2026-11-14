#!/usr/bin/env bash
# ONE COMMAND TO START. Double-click on Mac, or run:  ./run.sh
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "First time setup (about 1 minute)..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

echo "Verifying multi-agent pipeline and contracts..."
(cd backend && ../.venv/bin/python smoke.py) || { echo "SMOKE TEST FAILED - restore your backup, do not present."; exit 1; }

echo "Starting server on http://localhost:8000 (press Ctrl-C to stop)"
(sleep 1 && (open http://localhost:8000 || xdg-open http://localhost:8000)) >/dev/null 2>&1 &
cd backend && exec ../.venv/bin/python -m uvicorn main:app --port 8000
