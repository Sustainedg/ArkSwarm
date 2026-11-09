#!/usr/bin/env bash
# 构建前端并由后端统一托管（等价于 serve.sh，但强制重新构建前端）。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/frontend"
[ -d node_modules ] || npm install
npm run build
exec "$ROOT/scripts/serve.sh"
