#!/usr/bin/env bash
# 生产单端口入口：构建前端（如缺失）→ 由后端统一托管 UI + API，监听 0.0.0.0。
#
# 环境变量：
#   CODA_HOST  监听地址，默认 0.0.0.0
#   CODA_PORT  监听端口，默认 8010
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${CODA_HOST:-0.0.0.0}"
PORT="${CODA_PORT:-8010}"

venv_python="$ROOT/backend/.venv/bin/python"
if [ ! -x "$venv_python" ] || ! "$venv_python" -c '' >/dev/null 2>&1; then
  if [ -d "$ROOT/backend/.venv" ]; then
    echo "→ 后端虚拟环境不可用，重建 backend/.venv"
    python3 -m venv --clear "$ROOT/backend/.venv"
  else
    echo "→ 创建后端虚拟环境 backend/.venv"
    python3 -m venv "$ROOT/backend/.venv"
  fi
  "$ROOT/backend/.venv/bin/pip" -q install -U pip
  "$ROOT/backend/.venv/bin/pip" -q install -r "$ROOT/backend/requirements.txt"
fi

if [ ! -f "$ROOT/frontend/dist/index.html" ]; then
  if command -v npm >/dev/null 2>&1; then
    echo "→ frontend/dist 缺失，先构建前端"
    (
      cd "$ROOT/frontend"
      [ -d node_modules ] || npm install
      npm run build
    )
  else
    echo "⚠ 未找到 npm 且 frontend/dist 不存在：本次仅提供 API（文档见 /docs）" >&2
  fi
fi

echo "→ Coda 监听 http://$HOST:$PORT"
exec "$venv_python" -m uvicorn app.main:app \
  --app-dir "$ROOT/backend" --host "$HOST" --port "$PORT"
