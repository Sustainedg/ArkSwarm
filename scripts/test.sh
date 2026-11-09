#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/backend"
[ -d .venv ] || (python3 -m venv .venv && ./.venv/bin/pip -q install -r requirements-dev.txt)
./.venv/bin/python -m pytest tests "$@" -q
if [ -d "$ROOT/frontend/node_modules" ]; then
  echo "→ frontend typecheck"
  (cd "$ROOT/frontend" && npm run typecheck)
fi
