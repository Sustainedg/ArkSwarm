#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/backend"

# 测试必须只碰临时库：容器里继承了 ARKSWARM_DATA_DIR=/app/data（正式库），
# 这里先切断它，再交给 backend/conftest.py 做最终强制覆盖与断言。
TEST_DATA_DIR="$(mktemp -d "${TMPDIR:-/tmp}/arkswarm-test-shell-XXXXXX")"
export ARKSWARM_DATA_DIR="$TEST_DATA_DIR"
export ARKSWARM_DATABASE_URL="sqlite+aiosqlite:///$TEST_DATA_DIR/arkswarm.db"
export ARKSWARM_WORKSPACE_DIR="$TEST_DATA_DIR/workspaces"
trap 'rm -rf -- "$TEST_DATA_DIR"' EXIT

if [ ! -x .venv/bin/python ] || ! ./.venv/bin/python -c '' >/dev/null 2>&1; then
  if [ -d .venv ]; then
    echo "→ 后端虚拟环境不可用，重建 backend/.venv"
    python3 -m venv --clear .venv
  else
    python3 -m venv .venv
  fi
  ./.venv/bin/pip -q install -r requirements-dev.txt
fi
./.venv/bin/python -m pytest tests "$@" -q
if [ -d "$ROOT/frontend/node_modules" ]; then
  echo "→ 前端类型检查"
  (cd "$ROOT/frontend" && npm run typecheck)
fi
