#!/usr/bin/env bash
# 同时启动 API（自动重载）与 Vite 开发服务器。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${ARKSWARM_PORT:-8000}"

venv_python="$ROOT/backend/.venv/bin/python"
if [ ! -x "$venv_python" ] || ! "$venv_python" -c '' >/dev/null 2>&1; then
  if [ -d "$ROOT/backend/.venv" ]; then
    echo "→ 后端虚拟环境不可用，重建 backend/.venv"
    python3 -m venv --clear "$ROOT/backend/.venv"
  else
    echo "→ 创建 Python 虚拟环境"
    python3 -m venv "$ROOT/backend/.venv"
  fi
  "$ROOT/backend/.venv/bin/pip" -q install -U pip
  "$ROOT/backend/.venv/bin/pip" -q install -r "$ROOT/backend/requirements-dev.txt"
fi
if [ ! -d "$ROOT/frontend/node_modules" ]; then
  echo "→ 安装前端依赖"
  (cd "$ROOT/frontend" && npm install)
fi

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

echo "→ API      http://127.0.0.1:$PORT"
(cd "$ROOT/backend" && exec ./.venv/bin/python -m uvicorn app.main:app --reload --host 0.0.0.0 --port "$PORT")

if [ "${1:-}" = "--api-only" ]; then exit 0; fi

echo "→ Web UI   http://127.0.0.1:5173  （将 /api 反向代理到 :$PORT）"
export ARKSWARM_BACKEND="http://127.0.0.1:$PORT"
cd "$ROOT/frontend" && exec npm run dev
