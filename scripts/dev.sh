#!/usr/bin/env bash
# Run the API (reload) and the Vite dev server together.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${MAGENTA_PORT:-8000}"

if [ ! -d "$ROOT/backend/.venv" ]; then
  echo "→ creating python venv"
  python3 -m venv "$ROOT/backend/.venv"
  "$ROOT/backend/.venv/bin/pip" -q install -U pip
  "$ROOT/backend/.venv/bin/pip" -q install -r "$ROOT/backend/requirements-dev.txt"
fi
if [ ! -d "$ROOT/frontend/node_modules" ]; then
  echo "→ installing frontend deps"
  (cd "$ROOT/frontend" && npm install)
fi

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "→ API      http://127.0.0.1:$PORT"
(cd "$ROOT/backend" && exec ./.venv/bin/python -m uvicorn app.main:app --reload --host 0.0.0.0 --port "$PORT")

if [ "${1:-}" = "--api-only" ]; then exit 0; fi

echo "→ Web UI   http://127.0.0.1:5173  (proxying /api → :$PORT)"
export MAGENTA_BACKEND="http://127.0.0.1:$PORT"
cd "$ROOT/frontend" && exec npm run dev
