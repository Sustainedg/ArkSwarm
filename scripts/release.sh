#!/usr/bin/env bash
# 打包发布：构建前端 → 打 backend 源码 + 预构建 dist 的 tar.gz → 用 gh 创建 GitHub Release。
#
# 产物名固定 arkswarm.tar.gz，随 Release 附 arkswarm.tar.gz.sha256；
# 「设置 → 关于 → 检测更新」就吃这两个文件。
#
# 用法：
#   bash scripts/release.sh            # 版本号取自 backend/app/__init__.py 的 __version__
#   bash scripts/release.sh v0.44       # 显式指定 tag（须与 __version__ 一致，V 前缀小写化）
#
# 前置：已安装 gh 并 gh auth login；工作区干净（避免把半成品发出去）。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# ---- 版本号：唯一来源是 backend/app/__init__.py ----
APP_VERSION="$(grep -oP '(?<=^__version__ = ")[^"]+' backend/app/__init__.py)"
if [ -z "$APP_VERSION" ]; then
  echo "✗ 读不到 __version__（backend/app/__init__.py）" >&2
  exit 1
fi
TAG="${1:-v${APP_VERSION#V}}"
NORMALIZED="V${TAG#v}"
if [ "$NORMALIZED" != "$APP_VERSION" ]; then
  echo "✗ tag $TAG 与 __version__ $APP_VERSION 不一致：先改 backend/app/__init__.py 再发版" >&2
  exit 1
fi

# ---- 桌面端与后端/网页同版本发布：安装包会追加到同一个 Release ----
DESKTOP_VERSION="$(node -p "require('./desktop/package.json').version" 2>/dev/null || true)"
if [ "$DESKTOP_VERSION" != "${APP_VERSION#V}" ]; then
  echo "✗ 桌面端版本 ${DESKTOP_VERSION:-<读取失败>} 与后端 $APP_VERSION 不一致：先改 desktop/package.json" >&2
  exit 1
fi

# ---- 工作区必须干净（已提交的内容才构成一个版本）----
if [ -n "$(git status --porcelain)" ]; then
  echo "✗ 工作区不干净：先提交再发版" >&2
  git status --short >&2
  exit 1
fi
if ! git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "→ 本地没有 tag $TAG，在 HEAD 上创建"
  git tag "$TAG"
fi
if ! git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "✗ tag 创建失败" >&2
  exit 1
fi

echo "→ 发布 $TAG（对应 __version__ $APP_VERSION）"

# ---- 构建前端 ----
echo "→ 构建前端"
(
  cd frontend
  [ -d node_modules ] || npm install
  npm run build
)

# ---- 打包（backend 源码 + 预构建 dist + requirements；不带 .venv / data / node_modules）----
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
OUT="$STAGE/arkswarm.tar.gz"
echo "→ 打包 → $OUT"
tar -czf "$OUT" \
  --exclude='backend/.venv' \
  --exclude='backend/data' \
  --exclude='backend/__pycache__' \
  --exclude='backend/.pytest_cache' \
  --exclude='frontend/node_modules' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  --exclude='*.pyo' \
  backend/app backend/requirements.txt backend/.env.example frontend/dist \
  scripts/serve.sh scripts/mcp
SHA256="$(sha256sum "$OUT" | cut -d' ' -f1)"
echo "$SHA256" > "$OUT.sha256"
echo "→ sha256: $SHA256"

# ---- 创建 Release（正文里的 sha256 行是更新器的第二校验来源）----
echo "→ 创建 GitHub Release $TAG"
NOTES="$(mktemp)"
{
  echo "ArkSwarm $APP_VERSION"
  echo ""
  echo "sha256: $SHA256"
  echo ""
  echo "（版本说明见仓库 VERSION_HISTORY / 关于页版本记录）"
} > "$NOTES"

if gh release view "$TAG" >/dev/null 2>&1; then
  echo "→ Release $TAG 已存在，追加 / 覆盖产物"
  gh release upload "$TAG" "$OUT" "$OUT.sha256" --clobber
else
  gh release create "$TAG" "$OUT" "$OUT.sha256" \
    --title "$APP_VERSION" \
    --notes-file "$NOTES" \
    --target "$(git rev-parse --abbrev-ref HEAD)"
fi

echo "✓ 发布完成：$TAG"
echo "  部署端在「设置 → 关于 → 检测更新」即可看到并一键更新。"
